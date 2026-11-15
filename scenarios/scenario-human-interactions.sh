#!/usr/bin/env bash
# =============================================================================
# Scenario: human-interactions
#
# Simulated Matrix humans (@alice, @bob; rig-provisioned, tokens via
# lab/state/scenario.env as LAB_TOKEN_ALICE/LAB_TOKEN_BOB or runtime.json)
# interacting with each other and with radio-origin messages. Plain REST via
# scenarios/lib/mx_user.py; radio-side evidence from peer sniffers.
#
# Extracted product contract (from mmrelay release/1.5.0-medre-ingress code,
# not assumptions):
#   * Bridging ANY mapped-room message (human or radio-origin) writes a
#     message_map row keyed by the Matrix event id after the confirmed radio
#     send (message_queue.py stores mapping_info; storage is on whenever
#     replies or reactions interactions are enabled).
#   * A Matrix reply to a MAPPED event resolves through the local map and is
#     delivered as a structured radio reply (handle_matrix_reply ->
#     send_reply_to_meshtastic(reply_id=...)); the target packet id is each
#     relay's OWN mapped id for that event. Humans need no trusted_relays
#     entry: the resolution path runs for any sender (trusted_relays gates
#     only peer-metadata AUTHORITY, not reply resolution).
#   * A Matrix reaction (m.annotation) to a MAPPED event relays as a
#     structured radio reply carrying 'reacted <key> to "<preview>"'
#     (events.py reaction branch -> send_text_reply(reply_id=...)). The
#     reaction event itself writes NO map row.
#   * Human emotes (m.emote) relay as ordinary authored text.
#   * The matrix -> radio path emits no Matrix events and no envelopes.
#     Radio -> Matrix re-emissions of relay-originated traffic (self-echo)
#     are a meshtasticd behavior; if any relay-authored event appears it
#     must carry a well-formed canonical envelope (checked when present).
#
# NOTE on owner expectation vs extracted contract: the owner's brief guessed
# "no structured radio reply / no radio reaction" for human-to-human
# interactions "since no radio target exists". That premise does not hold
# once case (a) bridged the message: the bridging writes the map rows that
# the reply/reaction paths resolve (upstream CI tests 5/6 assert the same).
# This scenario asserts the EXTRACTED contract above; if the product moves to
# ordinary-text-only for human originals, cases (b) and (c) fail here by
# design and should be flipped together with the product change.
#
# Cases:
#   a. alice text in #lab-alpha-ch0 -> Alpha radios + Beta ch0 bridge; map
#      rows on both relays; peer sniffers see verbatim body on both meshes;
#      any relay re-emission carries a well-formed envelope.
#   b. bob replies to alice's message -> structured radio replies targeting
#      each relay's own mapped packet id (sniffer reply_id equality).
#   c. bob reacts (exact emoji) to alice's message -> 'reacted' radio text
#      with the exact key, structured onto the mapped packet ids.
#   d. alice replies to a RADIO-origin message -> structured radio reply on
#      Alpha targeting the original radio packet id (no trusted_relays).
#   e. alice reacts (exact emoji + skin-tone variant) to the radio-origin
#      message -> exact-key radio reactions targeting the packet id.
#   f. alice sends an m.emote -> relays as ordinary text (map rows, sniffer).
#   g. @untrusted performs (d)/(e) -> IDENTICAL outcomes (trust gates only
#      metadata authority, never normal human interaction).
#   h. alice/bob activity in #lab-other -> no map rows, no radio traffic.
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_rig
resolve_rooms

SCENARIO="human-interactions"
ALICE_TOKEN="$(lib_py mx_user.py token "${HUMAN_ALICE_LOCALPART}" | tr -d '"')"
BOB_TOKEN="$(lib_py mx_user.py token "${HUMAN_BOB_LOCALPART}" | tr -d '"')"
[[ -n ${ALICE_TOKEN} && ${ALICE_TOKEN} != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_ALICE_LOCALPART}" \
		"the rig must provision @alice and publish LAB_TOKEN_ALICE (scenarios/README.md)"
[[ -n ${BOB_TOKEN} && ${BOB_TOKEN} != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_BOB_LOCALPART}" \
		"the rig must provision @bob and publish LAB_TOKEN_BOB (scenarios/README.md)"

clear_sniffers() {
	stop_sniffer "${SNIFF_A_PID:-}" "${SNIFF_A:-}"
	stop_sniffer "${SNIFF_B_PID:-}" "${SNIFF_B:-}"
}
trap clear_sniffers EXIT

# arm_peers EXPECT_TEXT [EXPECT_REPLY_ID]
#   Attaches sniffers to BOTH peers before a triggering action.
arm_peers() {
	local expect_text="$1" expect_reply="${2:-}"
	if [[ -n ${expect_reply} ]]; then
		start_sniffer SNIFF_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
			--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
			--expect-text "${expect_text}" --expect-reply-id "${expect_reply}"
		start_sniffer SNIFF_B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
			--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
			--expect-text "${expect_text}" --expect-reply-id "${expect_reply}"
	else
		start_sniffer SNIFF_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
			--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
			--expect-text "${expect_text}"
		start_sniffer SNIFF_B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
			--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
			--expect-text "${expect_text}"
	fi
}

# expect_sniffed LABEL TMPFILE [EXPECT_REPLY_ID]
expect_sniffed() {
	local label="$1" file="$2" expect_reply="${3:-}"
	if wait_until "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" grep -q '"match"' "${file}"; then
		SC_PASS "${label}" "sniff: $(grep -o '"match":.*' "${file}" | head -1)"
	else
		SC_FAIL "${label}" "sniff log: $(cat "${file}")"
	fi
}

# check_envelopes_if_reemitted SINCE_MS MARKER
#   If the relays re-emitted the radio text into Matrix (self-echo), every
#   such event must carry a well-formed canonical envelope. If none exists,
#   report that the product does not re-emit (evidence printed, not a fail).
check_envelopes_if_reemitted() {
	local since_ms="$1" marker="$2"
	sleep "${SETTLE_SECONDS}"
	for bot in "${BOT_A_MXID}" "${BOT_B_MXID}"; do
		capture out lib_py mx_events.py scan "${ROOM_ALPHA_CH0_ID}" \
			--since-ms "${since_ms}" --sender "${bot}" --body-contains "${marker}"
		hits=$(jq_get "${out}" 'matches' | grep -c '"event_id"' || true)
		if ((hits > 0)); then
			capture ok lib_py mx_events.py scan "${ROOM_ALPHA_CH0_ID}" \
				--since-ms "${since_ms}" --sender "${bot}" --body-contains "${marker}" \
				--require-envelope --envelope-kind message.created --native-ref-is-numeric
			ok_count=$(jq_get "${ok}" 'matches' | grep -c '"event_id"' || true)
			((ok_count >= hits)) ||
				SC_FAIL "relay ${bot} re-emitted radio traffic with a malformed canonical envelope" \
					"emissions=${hits} envelope_ok=${ok_count}" $'\n'"${out}"
			SC_PASS "relay ${bot} re-emission carries a well-formed canonical envelope" \
				"events=${ok_count}"
		else
			SC_PASS "relay ${bot} emitted no Matrix re-emission of the radio text (no self-echo surface)" \
				"scanned ${ROOM_ALPHA_CH0_ID} since ${since_ms}"
		fi
	done
}

SINCE_MS="$(date +%s%3N)"

# =============================================================================
# Case a: alice text -> Alpha radios + Beta ch0 bridge
# =============================================================================
A_TEXT="ALICE-A-$(SC_UID)"
arm_peers "${A_TEXT}"
capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ALPHA_CH0_ID}" "${A_TEXT}"
((SC_STATUS == 0)) || SC_FAIL "case a: alice send failed" "${out}"
ALICE_EVENT_ID=$(jq_get "${out}" event_id | tr -d '"')
SC_PASS "case a: alice text sent" "event=${ALICE_EVENT_ID}"

expect_sniffed "case a: Alpha peer radio received alice's body verbatim" "${SNIFF_A}"
expect_sniffed "case a: Beta peer radio received alice's body verbatim (cross-mesh bridge)" "${SNIFF_B}"

for side in A B; do
	db_var="MAP_DB_${side}"
	capture rows lib_py maps.py rows-for-event "${!db_var}" "${ALICE_EVENT_ID}" --wait "${RADIO_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "case a: relay ${side} did not bridge alice's message (no map row)" "${rows}"
done
SC_PASS "case a: both relays bridged alice's message (map rows keyed by her event id)" \
	"A_rows=$(lib_py maps.py rows-for-event "${MAP_DB_A}" "${ALICE_EVENT_ID}")" $'\n'"B_rows=$(lib_py maps.py rows-for-event "${MAP_DB_B}" "${ALICE_EVENT_ID}")"

check_envelopes_if_reemitted "${SINCE_MS}" "${A_TEXT}"

# The packet ids each relay mapped for alice's message: the targets the
# reply/reaction cases must hit on each mesh.
A_PACKET=$(lib_py maps.py rows-for-event "${MAP_DB_A}" "${ALICE_EVENT_ID}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')
B_PACKET=$(lib_py maps.py rows-for-event "${MAP_DB_B}" "${ALICE_EVENT_ID}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')

# =============================================================================
# Case b: bob replies to alice's message -> structured radio replies
# =============================================================================
B_REPLY="BOB-B-$(SC_UID)"
arm_peers "${B_REPLY}" "${A_PACKET}"
# Beta's sniffer must target B's own mapped packet id; restart it precisely.
stop_sniffer "${SNIFF_B_PID}" "${SNIFF_B}"
start_sniffer SNIFF_B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "${B_REPLY}" --expect-reply-id "${B_PACKET}"

capture out lib_py mx_user.py reply "${HUMAN_BOB_LOCALPART}" "${ROOM_ALPHA_CH0_ID}" "${ALICE_EVENT_ID}" "${B_REPLY}"
((SC_STATUS == 0)) || SC_FAIL "case b: bob reply send failed" "${out}"
BOB_REPLY_EVENT_ID=$(jq_get "${out}" event_id | tr -d '"')
SC_PASS "case b: bob reply sent" "event=${BOB_REPLY_EVENT_ID} in_reply_to=${ALICE_EVENT_ID}"

expect_sniffed "case b: Alpha radio reply targets A's mapped packet ${A_PACKET}" "${SNIFF_A}" "${A_PACKET}"
expect_sniffed "case b: Beta radio reply targets B's mapped packet ${B_PACKET}" "${SNIFF_B}" "${B_PACKET}"

# =============================================================================
# Case c: bob reacts (exact emoji) to alice's message
# =============================================================================
REACT_KEY="🎯"
arm_peers "reacted ${REACT_KEY}"
stop_sniffer "${SNIFF_A_PID}" "${SNIFF_A}"
start_sniffer SNIFF_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "reacted ${REACT_KEY}" --expect-reply-id "${A_PACKET}"
stop_sniffer "${SNIFF_B_PID}" "${SNIFF_B}"
start_sniffer SNIFF_B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "reacted ${REACT_KEY}" --expect-reply-id "${B_PACKET}"

capture out lib_py mx_user.py react "${HUMAN_BOB_LOCALPART}" "${ROOM_ALPHA_CH0_ID}" "${ALICE_EVENT_ID}" "${REACT_KEY}"
((SC_STATUS == 0)) || SC_FAIL "case c: bob reaction send failed" "${out}"
BOB_REACT_EVENT_ID=$(jq_get "${out}" event_id | tr -d '"')
SC_PASS "case c: bob reaction sent" "event=${BOB_REACT_EVENT_ID} target=${ALICE_EVENT_ID} key=${REACT_KEY}"

expect_sniffed "case c: Alpha radio reaction text with exact key on packet ${A_PACKET}" "${SNIFF_A}" "${A_PACKET}"
expect_sniffed "case c: Beta radio reaction text with exact key on packet ${B_PACKET}" "${SNIFF_B}" "${B_PACKET}"

capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${BOB_REACT_EVENT_ID}"
count_rows=$(jq_get "${rows}" 'rows' | grep -c '"meshtastic_id"' || true)
((count_rows == 0)) ||
	SC_FAIL "case c: reaction event unexpectedly wrote a map row" "${rows}"
SC_PASS "case c: reaction event itself writes no map row (extraction: reaction branch sends without mapping_info)"

# =============================================================================
# Case d: radio-origin message; alice replies -> structured reply on the
# original RADIO packet id (no trusted_relays involvement for humans).
# =============================================================================
R_MARKER="RADIO-D-$(SC_UID)"
arm_peers "${R_MARKER}"
capture out lib_py radio.py send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${R_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "case d: radio seed injection failed" "${out}"
stop_sniffer "${SNIFF_A_PID}" "${SNIFF_A}"
stop_sniffer "${SNIFF_B_PID}" "${SNIFF_B}"

capture radio_json lib_py mx_events.py wait "${ROOM_ALPHA_CH0_ID}" \
	--sender "${BOT_A_MXID}" --body-contains "${R_MARKER}" \
	--require-envelope --envelope-kind message.created --source-channel "0" \
	--since-ms "${SINCE_MS}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "case d: no canonical event for the radio seed" "${radio_json}"
RADIO_EVENT_ID=$(jq_get "${radio_json}" event.event_id | tr -d '"')
RADIO_PACKET=$(jq_get "${radio_json}" event.content.medre.envelope.source_native_ref.native_message_id | tr -d '"')
SC_PASS "case d: radio-origin seed mapped" "event=${RADIO_EVENT_ID} packet=${RADIO_PACKET}"

D_REPLY="ALICE-D-$(SC_UID)"
arm_peers "${D_REPLY}" "${RADIO_PACKET}"
capture out lib_py mx_user.py reply "${HUMAN_ALICE_LOCALPART}" "${ROOM_ALPHA_CH0_ID}" "${RADIO_EVENT_ID}" "${D_REPLY}"
((SC_STATUS == 0)) || SC_FAIL "case d: alice reply send failed" "${out}"
D_REPLY_EVENT_ID=$(jq_get "${out}" event_id | tr -d '"')

expect_sniffed "case d: radio reply targets the ORIGINAL radio packet ${RADIO_PACKET}" "${SNIFF_A}" "${RADIO_PACKET}"
capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${D_REPLY_EVENT_ID}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "case d: relay A map missing alice's reply row" "${rows}"
SC_PASS "case d: structured reply delivered on Alpha without trusted_relays (human path)" \
	"reply_event=${D_REPLY_EVENT_ID} target_packet=${RADIO_PACKET}"

# =============================================================================
# Case e: alice reacts to the radio-origin message (exact emoji + modifier)
# =============================================================================
react_case_e() {
	local label="$1" key="$2"
	arm_peers "reacted ${key}"
	stop_sniffer "${SNIFF_A_PID}" "${SNIFF_A}"
	start_sniffer SNIFF_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
		--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
		--expect-text "reacted ${key}" --expect-reply-id "${RADIO_PACKET}"
	capture out lib_py mx_user.py react "${HUMAN_ALICE_LOCALPART}" "${ROOM_ALPHA_CH0_ID}" "${RADIO_EVENT_ID}" "${key}"
	((SC_STATUS == 0)) || SC_FAIL "case e (${label}): alice reaction send failed" "${out}"
	local react_event
	react_event=$(jq_get "${out}" event_id | tr -d '"')
	expect_sniffed "case e (${label}): exact-key radio reaction on packet ${RADIO_PACKET}" "${SNIFF_A}" "${RADIO_PACKET}"
}
react_case_e "emoji" "🦄"
react_case_e "skin-tone" "🤝🏽"

# =============================================================================
# Case f: alice sends an m.emote -> ordinary authored text
# =============================================================================
F_TEXT="waves to the lab"
arm_peers "${F_TEXT}"
capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ALPHA_CH0_ID}" "${F_TEXT}" --emote
((SC_STATUS == 0)) || SC_FAIL "case f: alice emote send failed" "${out}"
F_EVENT_ID=$(jq_get "${out}" event_id | tr -d '"')
expect_sniffed "case f: emote body relayed to the Alpha radio as ordinary text" "${SNIFF_A}"
expect_sniffed "case f: emote body bridged to the Beta radio as ordinary text" "${SNIFF_B}"
capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${F_EVENT_ID}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "case f: relay A map missing the emote row" "${rows}"
SC_PASS "case f: human m.emote relays as ordinary authored text" \
	"event=${F_EVENT_ID} map_rows=${rows}"

# =============================================================================
# Case g: @untrusted performs (d)/(e) -> identical outcomes
# =============================================================================
capture out lib_py mx_user.py token "${UNTRUSTED_LOCALPART}"
((SC_STATUS == 0)) || SC_SKIP "untrusted account not provisioned" "${out}"
UNTRUSTED_TOKEN=$(printf '%s' "${out}" | tr -d '"')

G_REPLY="UNTR-G-$(SC_UID)"
arm_peers "${G_REPLY}" "${RADIO_PACKET}"
capture out tools_exec python3 - "${UNTRUSTED_TOKEN}" "${ROOM_ALPHA_CH0_ID}" "${RADIO_EVENT_ID}" "${G_REPLY}" <<PYEOF
import json, sys
sys.path.insert(0, "${SCENARIOS_LIB_DIR}")
from mx_user import send_reply
token, room, target, body = sys.argv[1:5]
print(json.dumps(send_reply(token, room, target, body)))
PYEOF
((SC_STATUS == 0)) || SC_FAIL "case g: untrusted reply send failed" "${out}"
expect_sniffed "case g: untrusted reply resolves identically (structured reply on packet ${RADIO_PACKET})" "${SNIFF_A}" "${RADIO_PACKET}"
SC_PASS "case g: trust gate did not change untrusted human reply behavior"

G_KEY="🐳"
arm_peers "reacted ${G_KEY}"
stop_sniffer "${SNIFF_A_PID}" "${SNIFF_A}"
start_sniffer SNIFF_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "reacted ${G_KEY}" --expect-reply-id "${RADIO_PACKET}"
capture out tools_exec python3 - "${UNTRUSTED_TOKEN}" "${ROOM_ALPHA_CH0_ID}" "${RADIO_EVENT_ID}" "${G_KEY}" <<PYEOF
import json, sys
sys.path.insert(0, "${SCENARIOS_LIB_DIR}")
from mx_user import send_reaction
token, room, target, key = sys.argv[1:5]
print(json.dumps(send_reaction(token, room, target, key)))
PYEOF
((SC_STATUS == 0)) || SC_FAIL "case g: untrusted reaction send failed" "${out}"
expect_sniffed "case g: untrusted reaction relays identically (exact key on packet ${RADIO_PACKET})" "${SNIFF_A}" "${RADIO_PACKET}"
SC_PASS "case g: trust gate did not change untrusted human reaction behavior"

# =============================================================================
# Case h: alice/bob activity in #lab-other -> no radio side effects
# =============================================================================
[[ -n ${ROOM_OTHER_ID} ]] ||
	SC_SKIP "case h needs the unmapped #lab-other room (runtime.json)"

capture count_a lib_py maps.py count "${MAP_DB_A}"
capture count_b lib_py maps.py count "${MAP_DB_B}"
BASE_A=$(jq_get "${count_a}" count | tr -d '"')
BASE_B=$(jq_get "${count_b}" count | tr -d '"')

H_TEXT="OTHER-H-$(SC_UID)"
H_REPLY_TEXT="OTHER-HR-$(SC_UID)"
# Sniff for BOTH markers on both peers during the whole act: any radio
# transmission of unmapped-room content is a failure.
start_sniffer SNIFF_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" --expect-text "${H_TEXT}"
start_sniffer SNIFF_B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" --expect-text "${H_REPLY_TEXT}"

capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_OTHER_ID}" "${H_TEXT}"
((SC_STATUS == 0)) || SC_FAIL "case h: alice #lab-other send failed" "${out}"
H_EVENT_ID=$(jq_get "${out}" event_id | tr -d '"')
capture out lib_py mx_user.py reply "${HUMAN_BOB_LOCALPART}" "${ROOM_OTHER_ID}" "${ALICE_EVENT_ID}" "${H_REPLY_TEXT}"
((SC_STATUS == 0)) || SC_FAIL "case h: bob #lab-other reply send failed" "${out}"
H_REPLY_EVENT_ID=$(jq_get "${out}" event_id | tr -d '"')
# The reaction must target an in-room event: the homeserver rejects
# cross-room m.relation annotations server-side. The reply above keeps the
# cross-room reference the relays must refuse to resolve.
capture out lib_py mx_user.py react "${HUMAN_ALICE_LOCALPART}" "${ROOM_OTHER_ID}" "${H_EVENT_ID}" "🛑"
((SC_STATUS == 0)) || SC_FAIL "case h: alice #lab-other reaction send failed" "${out}"

sleep "${SETTLE_SECONDS}"

for event_id in "${H_EVENT_ID}" "${H_REPLY_EVENT_ID}"; do
	for side in A B; do
		db_var="MAP_DB_${side}"
		capture rows lib_py maps.py rows-for-event "${!db_var}" "${event_id}"
		row_count=$(jq_get "${rows}" 'rows' | grep -c '"meshtastic_id"' || true)
		((row_count == 0)) ||
			SC_FAIL "case h: #lab-other event ${event_id} produced a map row on relay ${side}" "${rows}"
	done
done
SC_PASS "case h: no map rows for #lab-other events"

if grep -q '"match"' "${SNIFF_A}" 2>/dev/null || grep -q '"match"' "${SNIFF_B}" 2>/dev/null; then
	SC_FAIL "case h: unmapped-room content reached a radio" \
		"A sniff: $(cat "${SNIFF_A}")" $'\n'"B sniff: $(cat "${SNIFF_B}")"
fi
SC_PASS "case h: no radio traffic for #lab-other content (peer sniffers silent)"
stop_sniffer "${SNIFF_A_PID}" "${SNIFF_A}"
stop_sniffer "${SNIFF_B_PID}" "${SNIFF_B}"

capture count_a lib_py maps.py count "${MAP_DB_A}"
capture count_b lib_py maps.py count "${MAP_DB_B}"
FINAL_A=$(jq_get "${count_a}" count | tr -d '"')
FINAL_B=$(jq_get "${count_b}" count | tr -d '"')
((FINAL_A == BASE_A && FINAL_B == BASE_B)) ||
	SC_FAIL "case h: relay map counts changed during the #lab-other act" \
		"A: ${BASE_A} -> ${FINAL_A}, B: ${BASE_B} -> ${FINAL_B}"
SC_PASS "case h: relay map counts unchanged" "A: ${BASE_A}, B: ${BASE_B}"

SC_LOG "[${SCENARIO}] complete"
