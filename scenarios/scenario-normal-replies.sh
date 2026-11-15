#!/usr/bin/env bash
# =============================================================================
# Scenario: normal-replies
#
# Cross-implementation reply correlation in the normal shape (MeshA/B =
# mmrelay-a/b, MeshC = medre-normal, one shared Matrix room):
#
# (a) MeshA text -> medre re-air on MeshC -> c-peer radio REPLY -> medre
#     replied event -> relay-a re-bridge:
#       1. alice's seed text in #lab-bridge is bridged by relay-a (packet P_a,
#          map row) AND re-aired by medre-normal onto MeshC (outbound radio
#          ref P_c in medre state, polled asynchronously).
#       2. The c-peer radio REPLY targets the re-aired packet P_c. medre
#          resolves it through its own map and emits a replied canonical
#          event carrying a relation to the original text.
#       3. relay-a (trusting @medre, remote meshnet claim) must re-bridge
#         that replied event onto MeshA as a STRUCTURED radio reply targeting
#         the ORIGINAL MeshA packet id P_a (assert via the a-peer sniffer:
#         reply_id == P_a, plus relay-a's map row for the medre event).
#       The medre event's relation surfaces (m.in_reply_to + hint target
#       form) are persisted as evidence so a failure classifies cleanly:
#       no resolvable target on the medre event = PRODUCT finding; a
#       misjudged expected form = SCENARIO finding.
#
# (b) Human Matrix reply (m.in_reply_to) to a MEDRE-origin event:
#       1. A c-peer radio seed produces medre's canonical event (packet P_c).
#       2. alice replies (m.in_reply_to) to that event; medre resolves the
#          target through its OWN state and transmits a structured radio
#          reply on MeshC with reply_id == P_c (c-peer sniffer capture,
#          armed before the Matrix send - the human reply travels via
#          Matrix, so no radio contention on c-peer during it).
#
# (c) mmrelay<->mmrelay reply path re-proven in the normal shape:
#       1. alice's seed text is bridged by BOTH relays (P_a on MeshA, P_b on
#          MeshB, one map row each for the same Matrix event).
#       2. An a-peer radio reply targets P_a; relay-a emits the replied
#          event with the native relation to the original (m.in_reply_to +
#          hint target).
#       3. relay-b must resolve that relation through ITS OWN map and send a
#          structured radio reply on MeshB with reply_id == P_b (b-peer
#          sniffer armed before the injection + relay-b's map row).
#
# Every canonical event is corpus-validated under the medre installed schema
# authority. Observation-window discipline: sniffers are armed before the
# triggering action and never share a node with an injection (one client per
# node; the a-peer sniffer holds the far slot while c-peer injects, etc.).
#
# Env keys: ROOM_BRIDGE_CH0_ALIAS, MEDRE_NORMAL_DB (or MEDRE_NORMAL_MAP_DB),
# NODE_{A,B,C}_PEER_HOST/PORT, MAP_DB_A/B, MEDRE_TRUNCATE_BYTES (227).
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=lib/medre_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/medre_lib.sh"
# shellcheck source=lib/normal_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/normal_lib.sh"

require_normal_rig
resolve_bridge_rooms

SCENARIO="normal-replies"
ALICE_TOKEN="$(lib_py mx_user.py token "${HUMAN_ALICE_LOCALPART}" | tr -d '"')"
[[ -n "${ALICE_TOKEN}" && "${ALICE_TOKEN}" != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_ALICE_LOCALPART}" \
		"the rig must provision @alice and publish LAB_TOKEN_ALICE (scenarios/README.md)"

ROOM_ID="${ROOM_BRIDGE_CH0_ID}"

# Radio-ingress readiness gates (mmrelay startup drain drops inbound radio
# packets by design): mesh C before the medre legs, mesh A before the
# mmrelay leg.
radio_ingest_ready "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${MEDRE_NORMAL_BOT_MXID}" "${ROOM_ID}" "medre-normal"
radio_ingest_ready "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${BOT_A_MXID}" "${ROOM_ID}" "relay-a"

# medre outbound radio packet id for an origin event (polled; medre writes
# outbound refs asynchronously after the canonical row lands).
medre_outbound_packet() { # ORIGIN_EVENT_ID -> packet id ("" when none)
	capture _mo_out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "$1" \
		--wait "${RADIO_WAIT_SECONDS}" --require-outbound-radio
	if ((SC_STATUS == 0)); then
		printf '%s' "${_mo_out}" | tools_exec python3 -c '
import json, re, sys
data = json.load(sys.stdin)
for event in data.get("events", []):
    for ref in event.get("native_refs", []):
        if ref.get("direction") == "outbound" and re.fullmatch(
            r"[0-9]+", str(ref.get("native_message_id") or "")
        ):
            print(ref["native_message_id"])
            raise SystemExit
'
	else
		SC_MEDRE_OUT_LAST="${_mo_out}"
		printf ''
	fi
}

# sniff_match VAR TMPFILE: extract the {"match":...} line or empty.
sniff_match() {
	local __var="$1" tmp="$2" line=""
	if grep -q '"match"' "${tmp}"; then
		line="$(grep -o '{"match":.*}' "${tmp}" | head -1)"
	fi
	printf -v "${__var}" '%s' "${line}"
}

# assert_reply_capture MATCH_JSON EXPECTED_REPLY_ID MARKER LABEL
#   Hard assertions on the captured radio reply: target id, authored body,
#   byte limit - and PREFIX-FREENESS (the r15 no-re-prefix pin, 2026-11-11
#   review): the re-bridged reply body must not gain a SECOND prefix token
#   (the nested-info verdict flags repeated prefix tokens). A failure here
#   is a PRODUCT finding (captured body in the evidence), never weakened.
assert_reply_capture() {
	local match_json="$1" expected_id="$2" marker="$3" label="$4"
	local verdict text nested
	verdict="$(printf '%s' "${match_json}" | tools_exec python3 -c "
import json, sys
payload = json.load(sys.stdin)
view = payload.get('match') or {}
text = view.get('text') or ''
reply_id = view.get('reply_id')
problems = []
if str(reply_id) != '${expected_id}':
    problems.append(f'reply_id={reply_id} != expected target ${expected_id}')
n = len(text.encode('utf-8'))
if n > ${MEDRE_TRUNCATE_BYTES}:
    problems.append(f'{n} UTF-8 bytes exceeds the radio limit ${MEDRE_TRUNCATE_BYTES}')
if '${marker}' not in text:
    problems.append('reply body missing from the radio text')
print(json.dumps({'problems': problems, 'view': view}))
")"
	printf '%s' "${verdict}" | grep -q '"problems": \[\]' ||
		SC_FAIL "${label}: structured radio reply is wrong (target/body/limit)" \
			"check: ${verdict}" $'\n'"match: ${match_json}"
	text="$(printf '%s' "${match_json}" | tools_exec python3 -c '
import json, sys
print((json.load(sys.stdin).get("match") or {}).get("text") or "")')"
	nested="$(medre_lib_py normal_checks.py nested-info "${text}")"
	printf '%s' "${nested}" | grep -q '"nested": false' ||
		SC_FAIL "${label}: the re-bridged reply body gained a second prefix token (r15 no-re-prefix regression on the reply path)" \
			"nested-info: ${nested}" $'\n'"captured body: ${text}" $'\n'"match: ${match_json}" $'\n'"PRODUCT CANDIDATE: report with the captured body if the current build reproduces this"
	SC_PASS "${label}: structured radio reply targets the expected packet (prefix-free body)" \
		"expected_target=${expected_id} nested-info=${nested} match=${match_json}"
}

# =============================================================================
# (a) MeshA text -> medre re-air on MeshC -> c-peer reply -> relay-a re-bridge
# =============================================================================
MARKER_A="NRPL-A-$(SC_UID)"
SINCE_A="$(date +%s%3N)"
capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" "${MARKER_A}"
((SC_STATUS == 0)) || SC_FAIL "(a) human seed send failed" "${out}"
SEED_EVENT_A="$(jq_get "${out}" event_id | tr -d '"')"

capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${SEED_EVENT_A}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "(a) mmrelay-a did not bridge the seed text (no P_a map row)" "${rows}"
PACKET_A="$(printf '%s' "${rows}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')"
SC_PASS "(a) relay-a bridged the seed text" "matrix_event=${SEED_EVENT_A} relay_a_packet=${PACKET_A}"

PACKET_C="$(medre_outbound_packet "${SEED_EVENT_A}")"
if [[ -z "${PACKET_C}" ]]; then
	capture _mo_out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${SEED_EVENT_A}"
	SC_FAIL "(a) medre-normal never re-aired the MeshA origin text onto MeshC (no outbound radio ref)" \
		"db=${MEDRE_NORMAL_DB} origin_event=${SEED_EVENT_A} waited=${RADIO_WAIT_SECONDS}s" $'\n'"state output: ${_mo_out}"
fi
SC_PASS "(a) medre re-aired the seed onto MeshC" \
	"origin_event=${SEED_EVENT_A} medre_packet=${PACKET_C} db=${MEDRE_NORMAL_DB}"

REPLY_A="NRPLY-A-$(SC_UID)"
# a-peer sniffer armed BEFORE the c-peer injection (one client per node).
start_sniffer_checked SNIFF_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "${REPLY_A}" --expect-reply-id "${PACKET_A}"
trap 'stop_sniffer "${SNIFF_A_PID:-}" "${SNIFF_A:-}"' EXIT

SINCE_REPLY_A="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${REPLY_A}" --reply-id "${PACKET_C}"
((SC_STATUS == 0)) || SC_FAIL "(a) c-peer radio reply injection failed" "${out}"
SC_PASS "(a) c-peer radio reply injected against the re-aired packet" \
	"reply_marker=${REPLY_A} target_packet=${PACKET_C}"

capture reply_json lib_py mx_events.py wait "${ROOM_ID}" \
	--sender "${MEDRE_NORMAL_BOT_MXID}" --body-contains "${REPLY_A}" \
	--require-envelope --envelope-kind message.created --relation-info reply \
	--since-ms "${SINCE_REPLY_A}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "(a) no medre replied event for the c-peer radio reply targeting packet ${PACKET_C}" "${reply_json}"
MEDRE_REPLY_EVENT_A="$(jq_get "${reply_json}" event.event_id | tr -d '"')"
medre_assert_valid "${reply_json}" "(a) medre replied event"
relation_summary="$(printf '%s' "${reply_json}" | medre_lib_py normal_checks.py hint-summary -)"
SC_PASS "(a) medre emitted the replied event for the MeshC radio reply" \
	"event=${MEDRE_REPLY_EVENT_A} relations=${relation_summary}"

sniff_match MATCH_A "${SNIFF_A}"
if [[ -z "${MATCH_A}" ]]; then
	SC_FAIL "(a) relay-a did not re-bridge the medre replied event onto MeshA as a structured reply targeting the ORIGINAL packet ${PACKET_A}" \
		"sniffer log: $(cat "${SNIFF_A}")" $'\n'"medre event=${MEDRE_REPLY_EVENT_A} relation surfaces=${relation_summary}" $'\n'"classification hint: a medre event whose hint target is radio-form (medre MeshC adapter) or absent gives relay-a no resolvable target - PRODUCT (cross-implementation correlation); if the medre event carries a Matrix-native target the resolution must go through relay-a's own map"
fi
assert_reply_capture "${MATCH_A}" "${PACKET_A}" "${REPLY_A}" "(a) relay-a re-bridge"

capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${MEDRE_REPLY_EVENT_A}" --wait "${RADIO_WAIT_SECONDS}"
printf '%s' "${rows}" | grep -q '"meshtastic_id"' ||
	SC_FAIL "(a) relay-a has no map row for the medre replied event (rows exist only after confirmed radio sends)" \
		"db=${MAP_DB_A} medre_event=${MEDRE_REPLY_EVENT_A} rows=${rows}"
SC_PASS "(a) relay-a map row confirms the re-bridged radio reply" \
	"db=${MAP_DB_A} medre_event=${MEDRE_REPLY_EVENT_A}"

stop_sniffer "${SNIFF_A_PID}" "${SNIFF_A}"
trap - EXIT

# =============================================================================
# (b) human Matrix reply to a medre-origin event -> medre structured radio
#     reply on MeshC via its own state
# =============================================================================
MARKER_B="NRPL-B-$(SC_UID)"
SINCE_B="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" "${MARKER_B}"
((SC_STATUS == 0)) || SC_FAIL "(b) c-peer radio seed injection failed" "${out}"
PACKET_B="$(jq_get "${out}" packet_id | tr -d '"')"

capture medre_json lib_py mx_events.py wait "${ROOM_ID}" \
	--sender "${MEDRE_NORMAL_BOT_MXID}" --body-contains "${MARKER_B}" \
	--require-envelope --envelope-kind message.created \
	--source-channel "0" --native-ref-is-numeric --native-ref-msg-id "${PACKET_B}" \
	--since-ms "${SINCE_B}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "(b) no medre canonical event for the MeshC radio seed (packet ${PACKET_B})" "${medre_json}"
MEDRE_EVENT_B="$(jq_get "${medre_json}" event.event_id | tr -d '"')"
medre_assert_valid "${medre_json}" "(b) medre seed event"

HUMAN_REPLY_B="NHRPLY-B-$(SC_UID)"
# c-peer sniffer armed BEFORE the Matrix send: the reply travels via Matrix,
# so the peer's client slot is free while alice sends.
start_sniffer_checked SNIFF_B "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "${HUMAN_REPLY_B}" --expect-reply-id "${PACKET_B}"
trap 'stop_sniffer "${SNIFF_B_PID:-}" "${SNIFF_B:-}"' EXIT

SINCE_REPLY_B="$(date +%s%3N)"
capture out lib_py mx_user.py reply "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" \
	"${MEDRE_EVENT_B}" "${HUMAN_REPLY_B}"
((SC_STATUS == 0)) || SC_FAIL "(b) alice reply send failed" "${out}"
HUMAN_REPLY_EVENT_B="$(jq_get "${out}" event_id | tr -d '"')"

if wait_until "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" grep -q '"match"' "${SNIFF_B}"; then
	sniff_match MATCH_B "${SNIFF_B}"
else
	MATCH_B=""
fi
if [[ -z "${MATCH_B}" ]]; then
	capture out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${HUMAN_REPLY_EVENT_B}"
	SC_FAIL "(b) medre did not transmit a structured radio reply on MeshC for the human Matrix reply" \
		"sniffer log: $(cat "${SNIFF_B}")" $'\n'"human_reply_event=${HUMAN_REPLY_EVENT_B} expected_target=${PACKET_B}" $'\n'"medre state for the reply event: ${out}"
fi
assert_reply_capture "${MATCH_B}" "${PACKET_B}" "${HUMAN_REPLY_B}" "(b) medre radio reply"

capture out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${HUMAN_REPLY_EVENT_B}" \
	--wait "${RADIO_WAIT_SECONDS}" --require-outbound-radio
((SC_STATUS == 0)) ||
	SC_FAIL "(b) medre state has no confirmed outbound radio send for the human reply event" "${out}"
SC_PASS "(b) medre resolved the human reply through its own state and targeted the original packet" \
	"target_packet=${PACKET_B} reply_event=${HUMAN_REPLY_EVENT_B}"

stop_sniffer "${SNIFF_B_PID}" "${SNIFF_B}"
trap - EXIT

# =============================================================================
# (c) mmrelay<->mmrelay reply path in the normal shape
# =============================================================================
MARKER_C="NRPL-C-$(SC_UID)"
SINCE_C="$(date +%s%3N)"
capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" "${MARKER_C}"
((SC_STATUS == 0)) || SC_FAIL "(c) human seed send failed" "${out}"
SEED_EVENT_C="$(jq_get "${out}" event_id | tr -d '"')"

capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${SEED_EVENT_C}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "(c) relay-a did not bridge the seed (no P_a)" "${rows}"
PACKET_A2="$(printf '%s' "${rows}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')"
capture rows lib_py maps.py rows-for-event "${MAP_DB_B}" "${SEED_EVENT_C}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "(c) relay-b did not bridge the seed (no P_b)" "${rows}"
PACKET_B2="$(printf '%s' "${rows}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')"
SC_PASS "(c) both relays bridged the seed (one map row each)" \
	"seed_event=${SEED_EVENT_C} relay_a_packet=${PACKET_A2} relay_b_packet=${PACKET_B2}"

REPLY_C="NRPLY-C-$(SC_UID)"
# b-peer sniffer armed BEFORE the a-peer injection.
start_sniffer_checked SNIFF_C "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "${REPLY_C}" --expect-reply-id "${PACKET_B2}"
trap 'stop_sniffer "${SNIFF_C_PID:-}" "${SNIFF_C:-}"' EXIT

SINCE_REPLY_C="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${REPLY_C}" --reply-id "${PACKET_A2}"
((SC_STATUS == 0)) || SC_FAIL "(c) a-peer radio reply injection failed" "${out}"

capture reply_json lib_py mx_events.py wait "${ROOM_ID}" \
	--sender "${BOT_A_MXID}" --body-contains "${REPLY_C}" \
	--require-envelope --relation-info reply \
	--relates-to "${SEED_EVENT_C}" --hint-target-msg-id "${SEED_EVENT_C}" \
	--since-ms "${SINCE_REPLY_C}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "(c) relay-a did not emit the replied event with a native relation to the original" "${reply_json}"
RELAY_REPLY_EVENT_C="$(jq_get "${reply_json}" event.event_id | tr -d '"')"
medre_assert_valid "${reply_json}" "(c) relay-a replied event"

sniff_match MATCH_C "${SNIFF_C}"
[[ -n "${MATCH_C}" ]] ||
	SC_FAIL "(c) relay-b did not send a structured radio reply on MeshB targeting its own packet ${PACKET_B2}" \
		"sniffer log: $(cat "${SNIFF_C}")" $'\n'"relay_a_reply_event=${RELAY_REPLY_EVENT_C}"
assert_reply_capture "${MATCH_C}" "${PACKET_B2}" "${REPLY_C}" "(c) relay-b structured reply"

capture rows lib_py maps.py rows-for-event "${MAP_DB_B}" "${RELAY_REPLY_EVENT_C}"
printf '%s' "${rows}" | grep -q '"meshtastic_id"' ||
	SC_FAIL "(c) relay-b has no map row for the replied event (no confirmed send)" \
		"db=${MAP_DB_B} replied_event=${RELAY_REPLY_EVENT_C} rows=${rows}"
SC_PASS "(c) relay-b map row confirms the structured reply send" \
	"db=${MAP_DB_B} replied_event=${RELAY_REPLY_EVENT_C}"

stop_sniffer "${SNIFF_C_PID}" "${SNIFF_C}"
trap - EXIT

SC_LOG "[${SCENARIO}] complete"
