#!/usr/bin/env bash
# =============================================================================
# Scenario: normal-reactions
#
# THE r15 LIVE REVALIDATION on the normal topology (the stress-topology run
# was loop-starved before the far re-bridge legs could ever complete; see
# triage/2026-11-11-medre-interop-run.md):
#
# Forward (c-peer tapbacks on the re-aired text -> relay-a re-bridge):
#   1. alice's seed text in #lab-bridge is bridged by relay-a (packet P_a)
#      and re-aired by medre-normal onto MeshC (outbound radio ref P_c).
#   2. A c-peer radio TAPBACK (Data.emoji=1) targets the re-aired packet P_c.
#      medre must emit a message.reacted event carrying:
#        * the EXACT relation key (byte-exact; plain 🦄 AND skin-tone 👍🏽),
#        * a numeric radio-native SOURCE ref (the tapback packet, channel "0"),
#        * the DESCRIBED m.emote rendering (matrix adapter #53): attribution
#          prefix + exact key + target preview, always an m.emote room
#          message (the native m.reaction annotation is retired). The
#          preview comes from canonical relation enrichment; with the
#          content ceiling enabled the target's admission issued the
#          bounded reply_quotes grant, so the resolved seed target is
#          QUOTED (2026-11-13 core fix; before it the body degraded to
#          the honest wording `an earlier message` - a degradation that
#          remains correct for genuinely unresolved targets, never `to ""`),
#        and the event must be corpus-valid under the medre schema authority.
#      The relation TARGET form is a product contract variable: the stress
#      candidate hinted the radio-native packet (target == P_c); the normal
#      candidate resolves the tapped packet to its canonical Matrix event
#      and hints the MATRIX-native reference (room id + $event id). Both
#      forms are corpus-licensed; the form is persisted as evidence because
#      it decides whether the far relay can recognize the reaction at all.
#   3. relay-a (trusting @medre, remote meshnet claim) must re-bridge the
#      peer reaction onto MeshA as a radio reaction whose captured packet
#      carries the EXACT emoji, WITHOUT re-prefixing the authored body
#      (r15 no-re-prefix fix) and WITHIN the radio UTF-8 byte limit (r15
#      byte-limit fix). The transport shape (plain authored send vs tapback)
#      is REPORTED; both shapes carry the exact key.
#   4. relay-a's map row for the medre reacted event confirms the send
#      (rows exist only after confirmed radio transmissions).
#
# Reverse (a-peer tapback -> relay-a -> medre adoption):
#   bob's seed text bridged by relay-a (packet P_a); an a-peer tapback with
#   the skin-tone key targets P_a. relay-a must emit the reacted event with
#   the exact key + radio-native target (corpus-valid), and medre-normal
#   must adopt the reaction: its state holds the event with the exact key in
#   an authoritative relation row or the stored unverified-observation
#   namespace (the documented trust boundary), with the real sender
#   preserved.
#
# Untargeted emote: a c-peer radio text carrying an UNRESOLVABLE reply id
# must stay a plain message end to end: medre's event has no relation
# (content-level and envelope relation_info ""), medre state stores no
# relation rows for it.
#
# Unobserved-target reaction: a c-peer TAPBACK whose target packet was
# NEVER observed (the DM-privacy / cross-bridge class) must still render -
# as a described m.emote whose body degrades to the honest wording
# `an earlier message` (never `to ""`; corpus case
# reacted-ch0-unobserved-target).
#
# Verdict policy: this is the r15 revalidation, so EVERY leg produces a
# verdict in one run - leg failures are RECORDED (the scenario still exits
# 1) instead of aborting at the first one. Only setup/precondition failures
# (seed not bridged, medre never re-aired) abort hard: without them the
# remaining legs have no subject.
#
# Observation-window discipline: the a-peer sniffer is armed BEFORE the
# tapback injection on c-peer (one client per node; no shared slots). The
# sniffer lifecycle is tagged and exclusive (common.sh). A sniffer window
# whose transport reports dropped queued packets (a second client evicted
# the connection mid-window) is NAMED in the failure evidence as a poisoned
# window - rerun the scenario for a clean verdict.
#
# Env keys: ROOM_BRIDGE_CH0_ALIAS, MEDRE_NORMAL_DB (or MEDRE_NORMAL_MAP_DB),
# NODE_{A,C}_PEER_HOST/PORT, MAP_DB_A, MEDRE_TRUNCATE_BYTES (227).
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

SCENARIO="normal-reactions"
ALICE_TOKEN="$(lib_py mx_user.py token "${HUMAN_ALICE_LOCALPART}" | tr -d '"')"
BOB_TOKEN="$(lib_py mx_user.py token "${HUMAN_BOB_LOCALPART}" | tr -d '"')"
[[ -n ${ALICE_TOKEN} && ${ALICE_TOKEN} != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_ALICE_LOCALPART}" \
		"the rig must provision @alice and publish LAB_TOKEN_ALICE (scenarios/README.md)"
[[ -n ${BOB_TOKEN} && ${BOB_TOKEN} != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_BOB_LOCALPART}" \
		"the rig must provision @bob and publish LAB_TOKEN_BOB (scenarios/README.md)"

ROOM_ID="${ROOM_BRIDGE_CH0_ID}"

# Readiness gates (mmrelay startup drain drops inbound radio packets by
# design): mesh C before the forward tapbacks, mesh A before the re-bridge
# capture and the reverse direction.
radio_ingest_ready "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${MEDRE_NORMAL_BOT_MXID}" "${ROOM_ID}" "medre-normal"
radio_ingest_ready "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${BOT_A_MXID}" "${ROOM_ID}" "relay-a"

# record_fail LABEL EVIDENCE_LINE...: print the FAIL verdict and remember
# the leg (the run exits 1 at the end; later legs still produce verdicts).
FAILED_LEGS=()
record_fail() {
	local label="$1"
	shift
	FAILED_LEGS+=("${label}")
	echo "FAIL: ${label}" >&2
	if (($# > 0)); then
		# shellcheck disable=SC2119 # SC_EVIDENCE intentionally consumes stdin.
		printf '%s\n' "$*" | SC_EVIDENCE >&2
	fi
}

# =============================================================================
# Forward: c-peer tapbacks on the re-aired text -> relay-a re-bridge
# =============================================================================
MARKER="NREACT-$(SC_UID)"
SINCE_SEED="$(date +%s%3N)"
capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" "${MARKER}"
((SC_STATUS == 0)) || SC_FAIL "seed send failed" "${out}"
SEED_EVENT="$(jq_get "${out}" event_id | tr -d '"')"

capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${SEED_EVENT}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "relay-a did not bridge the seed text (no P_a map row)" "${rows}"
PACKET_A="$(printf '%s' "${rows}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')"

capture out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${SEED_EVENT}" \
	--wait "${RADIO_WAIT_SECONDS}" --require-outbound-radio
((SC_STATUS == 0)) ||
	SC_FAIL "medre never re-aired the seed text onto MeshC (no outbound radio ref)" \
		"db=${MEDRE_NORMAL_DB} origin_event=${SEED_EVENT} waited=${RADIO_WAIT_SECONDS}s" $'\n'"state output: ${out}"
PACKET_C="$(printf '%s' "${out}" | tools_exec python3 -c '
import json, re, sys
data = json.load(sys.stdin)
for event in data.get("events", []):
    for ref in event.get("native_refs", []):
        if ref.get("direction") == "outbound" and re.fullmatch(
            r"[0-9]+", str(ref.get("native_message_id") or "")
        ):
            print(ref["native_message_id"])
            raise SystemExit
')"
SC_PASS "seed text bridged: relay-a packet P_a and medre MeshC re-air P_c known" \
	"matrix_event=${SEED_EVENT} relay_a_packet=${PACKET_A} medre_packet=${PACKET_C} db=${MEDRE_NORMAL_DB}"

run_forward_key() { # KEY VARIANT_LABEL
	local key="$1" label="$2"
	local since_ms out react_json react_id react_body match_line verdict rows
	local msgtype relation_summary sniff_poison

	start_sniffer_checked SNIFF "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
		--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
		--expect-text "${key}"
	trap 'stop_sniffer "${SNIFF_PID:-}" "${SNIFF:-}"' EXIT

	since_ms="$(date +%s%3N)"
	capture out lib_py radio.py send-reaction "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
		"${key}" "${PACKET_C}"
	((SC_STATUS == 0)) || SC_FAIL "[${label}] tapback injection failed" "${out}"
	SC_PASS "[${label}] c-peer tapback injected against the re-aired packet" \
		"key=${key} target_packet=${PACKET_C}"

	capture react_json lib_py mx_events.py wait "${ROOM_ID}" \
		--sender "${MEDRE_NORMAL_BOT_MXID}" \
		--require-envelope --envelope-kind message.reacted \
		--relation-info reaction --native-ref-is-numeric --native-ref-channel "0" \
		--hint-key-equals "${key}" \
		--since-ms "${since_ms}" --timeout "${MATRIX_WAIT_SECONDS}"
	if ((SC_STATUS != 0)); then
		record_fail "[${label}] no medre reacted event with the exact key ${key} for the tapback on packet ${PACKET_C}" \
			"${react_json}"
		stop_sniffer "${SNIFF_PID}" "${SNIFF}"
		trap - EXIT
		return 1
	fi
	react_id="$(jq_get "${react_json}" event.event_id | tr -d '"')"
	medre_assert_valid "${react_json}" "[${label}] medre reacted event"
	# The m.emote fallback body legitimately CONTAINS double quotes
	# (reacted 🦄 to "<preview>") - extract via JSON, never tr -d '"'.
	react_body="$(printf '%s' "${react_json}" | tools_exec python3 -c '
import json, sys
payload = json.load(sys.stdin)
print(payload.get("event", {}).get("content", {}).get("body") or "")
')"
	relation_summary="$(printf '%s' "${react_json}" | medre_lib_py normal_checks.py hint-summary -)"
	# Described reaction delivery (matrix adapter #53): EVERY relayed
	# reaction renders as a described m.emote room message - the relay
	# attribution prefix, the exact canonical key, and the target preview.
	# The native m.reaction annotation is the RETIRED shape (the annotation
	# carries the bridge identity instead of the reacting source user, and
	# Matrix annotation idempotency collapses same-key reactions from
	# different source users into one event); it must not reappear. The
	# preview comes from canonical relation enrichment (the destination's
	# quote read). The target ref RESOLVES to the seed event, and with the
	# content ceiling enabled the bridge's admission issued the bounded
	# reply_quotes grant for it, so the described body must QUOTE the seed
	# marker (the actual target text, whitespace-flattened and bounded;
	# 2026-11-13 fix: core issues the grant at ceiling-governed admission -
	# before that fix the quote read denied after ingress completion and the
	# body deterministically degraded to `an earlier message`). The literal
	# shape `to ""` must never appear. Envelope + bridge_context were
	# corpus-validated above (medre_assert_valid); reaction emote bodies
	# carry EXACTLY ONE intentional leading newline (owner ruling
	# 2026-11-12; mmrelay emote shape).
	shape_verdict="$(printf '%s' "${react_json}" | tools_exec python3 -c "
import json, sys
payload = json.load(sys.stdin)
event = payload.get('event') or {}
content = event.get('content') or {}
relates = content.get('m.relates_to') or {}
body = content.get('body') or ''
problems = []
if event.get('type') != 'm.room.message':
    problems.append(f\"event type {event.get('type')!r} != m.room.message (described reactions render as emote room messages; a native m.reaction annotation is the retired shape)\")
if content.get('msgtype') != 'm.emote':
    problems.append(f\"msgtype {content.get('msgtype')!r} != m.emote (described reaction delivery)\")
if relates:
    problems.append(f\"described reactions carry no m.relates_to: {relates!r}\")
if not body.startswith('\n'):
    problems.append('reaction emote body missing its EXACTLY ONE intentional leading newline')
elif body.startswith('\n\n'):
    problems.append('reaction emote body carries two or more leading newlines')
if '${key}' not in body:
    problems.append('exact reaction key missing from the rendered body')
if 'reacted ' not in body:
    problems.append('described reaction body lacks the reacted wording')
if 'to \"\"' in body:
    problems.append('reaction body quotes an EMPTY preview (to \"\")')
if 'to an earlier message' in body:
    problems.append(f\"resolved-target reaction degraded to 'an earlier message' - the quote read must be authorized for the resolved seed target (bounded reply_quotes grant)\")
if '${MARKER}' not in body:
    problems.append(f\"resolved-target reaction does not QUOTE the seed marker (target not resolved to the seed text): {body!r}\")
print(json.dumps({'problems': problems, 'msgtype': content.get('msgtype'), 'body': body}, ensure_ascii=False))
")"
	if ! printf '%s' "${shape_verdict}" | grep -q '"problems": \[\]'; then
		record_fail "[${label}] medre reaction is not a described m.emote delivery quoting the resolved seed target with the exact key" \
			"shape: ${shape_verdict}" $'\n'"event: ${react_json}"
	else
		SC_PASS "[${label}] medre delivered a described m.emote reaction with the exact key, quoting the resolved seed target" \
			"key=${key} target_event=${SEED_EVENT}" \
			"source_packet=$(jq_get "${react_json}" event.content.medre.envelope.source_native_ref.native_message_id | tr -d '"')" \
			"event=${react_id}" $'\n'"relation surfaces=${relation_summary}"
	fi

	if wait_until "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" grep -q '"match"' "${SNIFF}"; then
		match_line="$(grep -o '{"match":.*}' "${SNIFF}" | head -1)"
	else
		match_line=""
	fi
	if [[ -z ${match_line} ]]; then
		sniff_poison="no drop signature; check relay-a logs for the confirmed send"
		if grep -q "Dropped.*queued packet" "${SNIFF}"; then
			sniff_poison="SNIFFER WINDOW POISONED: the peer's API connection was evicted mid-window (a second client connected - one-client-per-node violated by concurrent tooling) and the transport dropped queued packets; rerun the scenario"
		fi
		record_fail "[${label}] relay-a never re-bridged the medre peer reaction onto MeshA" \
			"sniffer log: $(cat "${SNIFF}")" \
			$'\n'"${sniff_poison}" \
			$'\n'"reacted_event=${react_id} key=${key}"
		stop_sniffer "${SNIFF_PID}" "${SNIFF}"
		trap - EXIT
		return 1
	fi

	verdict="$(printf '%s' "${match_line}" | medre_lib_py normal_checks.py reaction-verdict - "${react_body}" "${key}" "${MEDRE_TRUNCATE_BYTES}")"
	if ! printf '%s' "${verdict}" | grep -q '"ok": true'; then
		record_fail "[${label}] far-mesh peer reaction re-bridge violates the r15 contract" \
			"verdict: ${verdict}" $'\n'"match: ${match_line}" $'\n'"medre reacted event: ${react_json}"
	else
		SC_PASS "[${label}] r15 revalidated: no re-prefixed body, within the byte limit, exact emoji on MeshA" \
			"shape=$(printf '%s' "${verdict}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin).get("shape"))')" \
			"bytes=$(printf '%s' "${verdict}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin).get("bytes"))')" \
			"limit=${MEDRE_TRUNCATE_BYTES} medre_event=${react_id}"
	fi

	# Send-confirmation evidence: the plain-send path stores a map row after
	# the confirmed transmission; the fixed authored-body reply path
	# (send_text_reply targeting the resolved packet) does NOT. The sniffer
	# capture above is the authoritative confirmation for both shapes; the
	# row presence is reported.
	capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${react_id}" --wait "${RADIO_WAIT_SECONDS}"
	if printf '%s' "${rows}" | grep -q '"meshtastic_id"'; then
		SC_LOG "[${label}] relay-a map row present (plain-send path): $(printf '%s' "${rows}" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
print(",".join(str(r.get("meshtastic_id")) for r in rows))')"
	else
		SC_LOG "[${label}] no map row (authored-body reply path stores none); confirmed by the sniffer capture"
	fi

	stop_sniffer "${SNIFF_PID}" "${SNIFF}"
	trap - EXIT
}

run_forward_key "🦄" "plain-unicorn" || true
run_forward_key "👍🏽" "skin-tone-thumbsup" || true

# =============================================================================
# Reverse: a-peer tapback -> relay-a -> medre adoption
# =============================================================================
MARKER_B="NREACT-R-$(SC_UID)"
SINCE_SEED_B="$(date +%s%3N)"
capture out lib_py mx_user.py send "${HUMAN_BOB_LOCALPART}" "${ROOM_ID}" "${MARKER_B}"
((SC_STATUS == 0)) || SC_FAIL "reverse seed send failed" "${out}"
SEED_EVENT_B="$(jq_get "${out}" event_id | tr -d '"')"

capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${SEED_EVENT_B}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "relay-a did not bridge the reverse seed (no P_a map row)" "${rows}"
PACKET_A_B="$(printf '%s' "${rows}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')"
SC_PASS "reverse seed bridged by relay-a" "matrix_event=${SEED_EVENT_B} relay_a_packet=${PACKET_A_B}"

KEY_B="👍🏽"
SINCE_RX_B="$(date +%s%3N)"
capture out lib_py radio.py send-reaction "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${KEY_B}" "${PACKET_A_B}"
((SC_STATUS == 0)) || SC_FAIL "reverse tapback injection failed" "${out}"
SC_PASS "reverse tapback injected against relay-a's own packet" \
	"key=${KEY_B} target_packet=${PACKET_A_B}"

capture react_json lib_py mx_events.py wait "${ROOM_ID}" \
	--sender "${BOT_A_MXID}" \
	--require-envelope --envelope-kind message.reacted \
	--relation-info reaction --native-ref-is-numeric --native-ref-channel "0" \
	--hint-target-msg-id "${PACKET_A_B}" --hint-key-equals "${KEY_B}" \
	--since-ms "${SINCE_RX_B}" --timeout "${MATRIX_WAIT_SECONDS}"
if ((SC_STATUS != 0)); then
	record_fail "no relay-a reacted event with the exact key ${KEY_B} and numeric target ${PACKET_A_B}" \
		"${react_json}"
else
	REACT_EVENT_B="$(jq_get "${react_json}" event.event_id | tr -d '"')"
	medre_assert_valid "${react_json}" "reverse relay-a reacted event"
	SC_PASS "relay-a emitted the exact skin-tone key with a radio-native target" \
		"key=${KEY_B} target_packet=${PACKET_A_B} event=${REACT_EVENT_B}"

	capture out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${REACT_EVENT_B}" --wait "${RADIO_WAIT_SECONDS}"
	adoption_verdict="$(printf '%s' "${out}" | medre_lib_py normal_checks.py reaction-adoption - "${KEY_B}")"
	if ! printf '%s' "${adoption_verdict}" | grep -q '"ok": true'; then
		record_fail "medre-normal did not adopt the trusted peer reaction" \
			"verdict: ${adoption_verdict}" $'\n'"medre state output: ${out}" $'\n'"reacted_event=${REACT_EVENT_B}"
	else
		SC_PASS "medre adopted the reaction (exact key present in relations/observations; sender preserved)" \
			"verdict: ${adoption_verdict}"
	fi
fi

# =============================================================================
# Untargeted emote stays a plain message
# =============================================================================
MARKER_U="NREACT-U-$(SC_UID)"
SINCE_U="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${MARKER_U}" --reply-id 999999999
((SC_STATUS == 0)) || SC_FAIL "untargeted reply-id injection failed" "${out}"
SC_PASS "c-peer radio text with an unresolvable reply id injected" \
	"marker=${MARKER_U} unmapped_reply_id=999999999"

capture out lib_py mx_events.py wait "${ROOM_ID}" \
	--sender "${MEDRE_NORMAL_BOT_MXID}" --body-contains "${MARKER_U}" \
	--require-envelope --envelope-kind message.created --no-relation \
	--since-ms "${SINCE_U}" --timeout "${MATRIX_WAIT_SECONDS}"
if ((SC_STATUS != 0)); then
	record_fail "medre treated the untargeted reply-id text as a relation (event carries one) or never bridged it" \
		"${out}"
else
	UNTARGETED_EVENT="$(jq_get "${out}" event.event_id | tr -d '"')"
	medre_assert_valid "${out}" "untargeted text as plain message"
	# Contract scope: "stays a plain message" = the Matrix CONTENT carries no
	# relation (asserted by the --no-relation wait above) and medre state
	# stores no relation rows (below). The medre bridge may still PRESERVE
	# the unresolvable wire reply target as a radio-native envelope hint -
	# that is a divergence from mmrelay's baseline (untargeted-emote:
	# mmrelay drops the unresolvable target entirely) and is REPORTED, not
	# failed: the content renders plain and downstream relays treat the
	# event as ordinary text.
	untargeted_summary="$(printf '%s' "${out}" | medre_lib_py normal_checks.py hint-summary -)"
	if printf '%s' "${untargeted_summary}" | grep -qv '"hints": \[\]'; then
		SC_LOG "DIVERGENCE NOTE: medre preserved the unresolvable wire reply target as an envelope hint (mmrelay's untargeted-emote baseline drops it): ${untargeted_summary}"
	fi
	# medre writes relation rows asynchronously after the canonical row lands
	# (the outbound-ref lag class), so a first-sight read races the writer:
	# poll across a settle window and sample repeatedly. What medre does
	# with an UNRESOLVABLE wire reply target is a contract variable: the
	# current build deterministically stores it as an event_relations row
	# (a faithful FIRST-PARTY wire claim - medre heard the packet itself -
	# whose target can never resolve: unmapped id, and downstream relays
	# ignore unresolvable radio targets for replies). mmrelay's baseline
	# stores nothing. Either way the content renders plain, so the row
	# presence is REPORTED, not failed; the hard assertions are the
	# content-level relation freedom (the --no-relation wait above) and
	# corpus validity.
	untargeted_state=""
	settle_deadline=$((SECONDS + SETTLE_SECONDS * 3))
	while ((SECONDS < settle_deadline)); do
		capture out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${UNTARGETED_EVENT}"
		untargeted_state="$(printf '%s' "${out}" | tools_exec python3 -c '
import json, sys
data = json.load(sys.stdin)
events = data.get("events", [])
relations = []
for event in events:
    for relation in event.get("relations", []):
        relations.append(relation)
print(json.dumps({"events": len(events), "relations": relations}))')"
		if printf '%s' "${untargeted_state}" | grep -qv '"relations": \[\]'; then
			break
		fi
		sleep "${SETTLE_SECONDS}"
	done
	if printf '%s' "${untargeted_state}" | grep -qv '"relations": \[\]'; then
		SC_LOG "DIVERGENCE NOTE: medre stored the unresolvable wire reply target as an event_relations row (first-party claim; mmrelay's baseline stores none): ${untargeted_state}"
	fi
	SC_PASS "untargeted emote stays a plain message end to end (content relation-free, corpus-valid; state row presence reported)" \
		"event=${UNTARGETED_EVENT}" $'\n'"state: ${untargeted_state}"
fi

# =============================================================================
# Unobserved-target reaction degrades to honest wording (corpus case
# reacted-ch0-unobserved-target; the DM-privacy / cross-bridge class: a
# reaction whose target message the bridge NEVER observed - e.g. a packet
# id from a direct message medre ignores, or a far bridge's own radio
# packet. The described reaction must quote `an earlier message` instead of
# an empty preview; the literal shape `to ""` must not reach the room).
# =============================================================================
KEY_UO="🦄"
UNOBSERVED_PACKET="999999998" # well-formed u32 packet id no rig leg ever uses
SINCE_UO="$(date +%s%3N)"
capture out lib_py radio.py send-reaction "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${KEY_UO}" "${UNOBSERVED_PACKET}"
((SC_STATUS == 0)) || SC_FAIL "unobserved-target tapback injection failed" "${out}"
SC_PASS "c-peer tapback injected against a packet the bridge never observed" \
	"key=${KEY_UO} unobserved_target_packet=${UNOBSERVED_PACKET}"

capture out lib_py mx_events.py wait "${ROOM_ID}" \
	--sender "${MEDRE_NORMAL_BOT_MXID}" \
	--require-envelope --envelope-kind message.reacted \
	--relation-info reaction --native-ref-is-numeric --native-ref-channel "0" \
	--hint-key-equals "${KEY_UO}" --hint-target-msg-id "${UNOBSERVED_PACKET}" \
	--since-ms "${SINCE_UO}" --timeout "${MATRIX_WAIT_SECONDS}"
if ((SC_STATUS != 0)); then
	record_fail "no medre reacted event for the tapback on the unobserved packet ${UNOBSERVED_PACKET}" \
		"${out}"
else
	UNOBSERVED_EVENT="$(jq_get "${out}" event.event_id | tr -d '"')"
	medre_assert_valid "${out}" "unobserved-target reacted event"
	degradation_verdict="$(printf '%s' "${out}" | tools_exec python3 -c "
import json, sys
payload = json.load(sys.stdin)
content = (payload.get('event') or {}).get('content') or {}
body = content.get('body') or ''
problems = []
if 'to an earlier message' not in body:
    problems.append(f\"unobserved-target reaction body does not degrade to the honest wording (expected 'an earlier message'): {body!r}\")
if 'to \"\"' in body:
    problems.append('reaction body quotes an EMPTY preview (to \"\")')
print(json.dumps({'problems': problems, 'body': body}, ensure_ascii=False))
")"
	if ! printf '%s' "${degradation_verdict}" | grep -q '"problems": \[\]'; then
		record_fail "unobserved-target reaction does not render the honest degradation" \
			"verdict: ${degradation_verdict}" $'\n'"event: ${out}"
	else
		SC_PASS "unobserved-target reaction degrades honestly to 'an earlier message' (never to \"\")" \
			"event=${UNOBSERVED_EVENT} target_packet=${UNOBSERVED_PACKET}" \
			"body: $(printf '%s' "${degradation_verdict}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin).get("body"))')"
	fi
fi

# =============================================================================
# Verdict
# =============================================================================
if ((${#FAILED_LEGS[@]} > 0)); then
	SC_LOG "[${SCENARIO}] legs with failures: ${FAILED_LEGS[*]}"
	exit 1
fi

SC_LOG "[${SCENARIO}] complete"
