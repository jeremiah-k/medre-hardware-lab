#!/usr/bin/env bash
# =============================================================================
# Scenario: medre-replies
#
# Cross-implementation reply correlation on one mesh (AlphaLab), two concerns:
#
# (a) Radio reply targeting an mmrelay-bridged packet, consumed by the medre
#     bridge:
#       1. A human Matrix text is bridged by BOTH bridges (mmrelay packet P_a,
#          medre packet P_m). The medre bridge also hears mmrelay's P_a packet
#          over the air and maps it (inbound native ref).
#       2. A radio reply injected from the ALPHA PEER with reply_id=P_a must
#          produce a medre replied event whose bridge_context relation hint
#          targets the RADIO-NATIVE packet id P_a (numeric, channel 0).
#       3. mmrelay (trusting the medre bot) must NOT re-emit the medre copy to
#          radio: the same-meshnet peer guard makes the peer copy inert.
#          Evidence is BOTH state (no mmrelay message_map row for the medre
#          replied event: rows exist only after confirmed radio sends) AND
#          radio (the a-peer collector captures zero packets carrying the
#          reply text in its window).
#          Observation-window note: the one-client-per-node rule forbids
#          arming a collector on the SAME peer node during the injection
#          (the injector evicts the collector; the collector's reconnect
#          fights the injector handshake). The collector is therefore armed
#          immediately after the blocking injection returns - any loop echo
#          is additionally caught by the map-state assertion, which is
#          race-free (rows are written only after confirmed sends).
#
# (b) Human Matrix reply (m.in_reply_to) to a MEDRE-authored event:
#       1. A radio text injected from the ALPHA PEER produces the medre bot's
#          canonical event (packet P mapped as medre's inbound native ref).
#          mmrelay is inert to that Matrix copy (same-meshnet guard), so
#          ONLY the medre bridge can resolve a reply to it: no mmrelay map
#          row exists for the medre event.
#       2. alice replies (m.in_reply_to) to the medre event; the medre bridge
#          must resolve the target through its OWN map and transmit a
#          structured radio reply whose reply_id == P (the original packet).
#       3. The a-peer collector must capture exactly that packet (text carries
#          the human body, reply_id == P, within the radio byte limit).
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=lib/medre_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/medre_lib.sh"

require_rig
require_medre_bridge
resolve_rooms

SCENARIO="medre-replies"
ALICE_TOKEN="$(lib_py mx_user.py token "${HUMAN_ALICE_LOCALPART}" | tr -d '"')"
[[ -n ${ALICE_TOKEN} && ${ALICE_TOKEN} != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_ALICE_LOCALPART}" \
		"the rig must provision @alice and publish LAB_TOKEN_ALICE (scenarios/README.md)"

# medre radio packet id from an events-for-native JSON payload: the outbound
# radio native ref of the canonical event(s) owning the looked-up native id.
medre_packet_from_events() { # EVENTS_JSON -> packet id (or "")
	printf '%s' "$1" | tools_exec python3 -c '
import json, re, sys
data = json.load(sys.stdin)
packets = []
for event in data.get("events", []):
    for ref in event.get("native_refs", []):
        if ref.get("direction") == "outbound" and re.fullmatch(
            r"[0-9]+", str(ref.get("native_message_id") or "")
        ):
            packets.append(str(ref["native_message_id"]))
print(packets[0] if packets else "")
'
}

# =============================================================================
# (a) radio reply targeting an mmrelay-bridged packet -> medre replied event,
#     mmrelay peer copy inert (no radio echo)
# =============================================================================
MARKER_A="MEDRE-RPL-A-$(SC_UID)"
SINCE_A="$(date +%s%3N)"
capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ALPHA_CH0_ID}" "${MARKER_A}"
((SC_STATUS == 0)) || SC_FAIL "(a) human seed text failed" "${out}"
ALICE_EVENT_A="$(jq_get "${out}" event_id | tr -d '"')"

capture out lib_py maps.py rows-for-event "${MAP_DB_A}" "${ALICE_EVENT_A}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "(a) mmrelay did not bridge the seed text (no P_a)" "${out}"
PACKET_A="$(printf '%s' "${out}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')"
SC_PASS "(a) mmrelay bridged the seed text" "matrix_event=${ALICE_EVENT_A} mmrelay_packet=${PACKET_A}"

capture out medre_state_py events-for-native "${MEDRE_DB_A}" "${ALICE_EVENT_A}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "(a) medre state has no canonical event for the seed text" "${out}"
MEDRE_OUT_PACKET_A="$(medre_packet_from_events "${out}")"
SC_PASS "(a) medre bridged the seed text to radio" \
	"db=${MEDRE_DB_A} medre_outbound_packet=${MEDRE_OUT_PACKET_A:-none}"

# The medre bridge must also hold mmrelay's packet (heard over the air) so the
# reply can resolve; wait for that inbound ref before injecting the reply.
capture out medre_state_py events-for-native "${MEDRE_DB_A}" "${PACKET_A}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "(a) medre did not map mmrelay's bridged packet ${PACKET_A} (reply target would be unresolvable)" "${out}"
SC_PASS "(a) medre mapped mmrelay's bridged packet (inbound native ref)" \
	"packet=${PACKET_A} db=${MEDRE_DB_A}"

REPLY_A="MEDRE-RPLY-$(SC_UID)"
SINCE_REPLY_A="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${REPLY_A}" --reply-id "${PACKET_A}"
((SC_STATUS == 0)) || SC_FAIL "(a) radio reply injection failed" "${out}"

# The collector is armed AFTER the blocking injection (one client per node:
# the injector and a collector cannot share the peer's API slot). Any loop
# echo fired in the arming gap is caught race-free by the map-state assertion
# below (map rows exist only after confirmed radio sends).
start_collector COLLECT "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	--timeout "${SNIFF_WAIT_SECONDS}" --contains "${REPLY_A}"
trap 'stop_collector "${COLLECT_PID}" "${COLLECT}"' EXIT

capture reply_json lib_py mx_events.py wait "${ROOM_ALPHA_CH0_ID}" \
	--sender "${MEDRE_BOT_MXID}" --body-contains "${REPLY_A}" \
	--require-envelope --envelope-kind message.created --relation-info reply \
	--source-channel "0" --native-ref-is-numeric \
	--hint-target-msg-id "${PACKET_A}" \
	--since-ms "${SINCE_REPLY_A}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "(a) no medre replied event with a radio-native hint targeting packet ${PACKET_A}" "${reply_json}"
MEDRE_REPLY_EVENT_A="$(jq_get "${reply_json}" event.event_id | tr -d '"')"
medre_assert_valid "${reply_json}" "(a) medre replied event"
SC_PASS "(a) medre bridged the radio reply with a radio-native hint" \
	"event=${MEDRE_REPLY_EVENT_A} hint_target=${PACKET_A}"

capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${MEDRE_REPLY_EVENT_A}"
rows_count=$(jq_get "${rows}" 'rows' | grep -c '"meshtastic_id"' || true)
((rows_count == 0)) ||
	SC_FAIL "(a) mmrelay re-bridged the medre peer copy (map row written after a confirmed radio send)" \
		"rows=${rows} medre_event=${MEDRE_REPLY_EVENT_A}"
SC_PASS "(a) mmrelay peer copy inert: no map row for the medre replied event" \
	"db=${MAP_DB_A} medre_event=${MEDRE_REPLY_EVENT_A}"

wait_until "$((SNIFF_WAIT_SECONDS + 10))" grep -q '"packets"' "${COLLECT}" ||
	SC_FAIL "(a) collector window ended without a result" "collector log: $(cat "${COLLECT}")"
COLLECT_RESULT="$(collector_result "${COLLECT}")"
echo_count="$(printf '%s' "${COLLECT_RESULT}" | tools_exec python3 -c "
import json, sys
data = json.load(sys.stdin)
print(data.get('count', -1))
")"
[[ ${echo_count} == "0" ]] ||
	SC_FAIL "(a) radio echo detected on Alpha: a bridge re-emitted the reply text" \
		"collector: ${COLLECT_RESULT}" $'\n'"medre_event=${MEDRE_REPLY_EVENT_A}"
SC_PASS "(a) no radio echo on Alpha for the medre peer copy (collector window clean)" \
	"window=${SNIFF_WAIT_SECONDS}s result=${COLLECT_RESULT}"
stop_collector "${COLLECT_PID}" "${COLLECT}"
trap - EXIT

# =============================================================================
# (b) human Matrix reply to a medre event -> medre structured radio reply
#     via its own map
# =============================================================================
MARKER_B="MEDRE-RPL-B-$(SC_UID)"
SINCE_B="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${MARKER_B}"
((SC_STATUS == 0)) || SC_FAIL "(b) radio seed injection failed" "${out}"
PACKET_B="$(jq_get "${out}" packet_id | tr -d '"')"

capture medre_json lib_py mx_events.py wait "${ROOM_ALPHA_CH0_ID}" \
	--sender "${MEDRE_BOT_MXID}" --body-contains "${MARKER_B}" \
	--require-envelope --envelope-kind message.created \
	--source-channel "0" --native-ref-is-numeric --native-ref-msg-id "${PACKET_B}" \
	--since-ms "${SINCE_B}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "(b) no medre canonical event for the radio seed (packet ${PACKET_B})" "${medre_json}"
MEDRE_EVENT_B="$(jq_get "${medre_json}" event.event_id | tr -d '"')"
medre_assert_valid "${medre_json}" "(b) medre seed event"

capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${MEDRE_EVENT_B}"
rows_count=$(jq_get "${rows}" 'rows' | grep -c '"meshtastic_id"' || true)
((rows_count == 0)) ||
	SC_FAIL "(b) test precondition broken: mmrelay mapped the medre Matrix copy (it must be inert)" \
		"rows=${rows}"
SC_PASS "(b) attribution precondition: mmrelay holds no map row for the medre event" \
	"medre_event=${MEDRE_EVENT_B} packet=${PACKET_B}"

capture out medre_state_py events-for-native "${MEDRE_DB_A}" "${PACKET_B}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "(b) medre state lacks the inbound native ref for its own seed packet ${PACKET_B}" "${out}"
SC_PASS "(b) medre map holds the original packet via its own state" \
	"packet=${PACKET_B} db=${MEDRE_DB_A}"

HUMAN_REPLY_B="MEDRE-HRPLY-$(SC_UID)"
# Tagged sniffer (first-match with reply-id filter): mmrelay may relay the
# human reply body as ordinary text (unmapped-target fallback, reply_id
# absent); only the medre bridge can emit a structured reply carrying
# reply_id == PACKET_B, so the filter attributes the transmission cleanly.
start_sniffer SNIFF "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "${HUMAN_REPLY_B}" --expect-reply-id "${PACKET_B}"
trap 'stop_sniffer "${SNIFF_PID}" "${SNIFF}"' EXIT

SINCE_REPLY_B="$(date +%s%3N)"
capture out lib_py mx_user.py reply "${HUMAN_ALICE_LOCALPART}" "${ROOM_ALPHA_CH0_ID}" \
	"${MEDRE_EVENT_B}" "${HUMAN_REPLY_B}"
((SC_STATUS == 0)) || SC_FAIL "(b) alice reply send failed" "${out}"
HUMAN_REPLY_EVENT_B="$(jq_get "${out}" event_id | tr -d '"')"

if wait_until "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" grep -q '"match"' "${SNIFF}"; then
	MATCH_LINE="$(grep -o '{"match":.*}' "${SNIFF}" | head -1)"
else
	MATCH_LINE=""
fi
[[ -n ${MATCH_LINE} ]] ||
	SC_FAIL "(b) medre did not transmit a structured radio reply for the human Matrix reply" \
		"sniffer log: $(cat "${SNIFF}")" $'\n'"human_reply_event=${HUMAN_REPLY_EVENT_B} expected_target=${PACKET_B}"

reply_check="$(printf '%s' "${MATCH_LINE}" | tools_exec python3 -c "
import json, sys
payload = json.load(sys.stdin)
view = payload.get('match') or {}
text = view.get('text') or ''
reply_id = view.get('reply_id')
problems = []
if str(reply_id) != '${PACKET_B}':
    problems.append(f'reply_id={reply_id} != original packet ${PACKET_B}')
n = len(text.encode('utf-8'))
if n > ${MEDRE_TRUNCATE_BYTES}:
    problems.append(f'{n} UTF-8 bytes exceeds the radio limit ${MEDRE_TRUNCATE_BYTES}')
if '${HUMAN_REPLY_B}' not in text:
    problems.append('human reply body missing from the radio text')
print(json.dumps({'problems': problems, 'view': view}))
")"
printf '%s' "${reply_check}" | grep -q '"problems": \[\]' ||
	SC_FAIL "(b) medre structured reply is wrong (target/body/limit)" \
		"check: ${reply_check}" $'\n'"match: ${MATCH_LINE}"
# Prefix-freeness (the r15 no-re-prefix pin, 2026-11-11 review): the
# transmitted reply body must not gain a second prefix token.
REPLY_TEXT_B="$(printf '%s' "${MATCH_LINE}" | tools_exec python3 -c '
import json, sys
print((json.load(sys.stdin).get("match") or {}).get("text") or "")')"
REPLY_NESTED_B="$(medre_lib_py normal_checks.py nested-info "${REPLY_TEXT_B}")"
printf '%s' "${REPLY_NESTED_B}" | grep -q '"nested": false' ||
	SC_FAIL "(b) the transmitted reply body gained a second prefix token (r15 no-re-prefix regression on the reply path)" \
		"nested-info: ${REPLY_NESTED_B}" $'\n'"captured body: ${REPLY_TEXT_B}" $'\n'"match: ${MATCH_LINE}"
SC_PASS "(b) medre resolved the human reply through its own map and targeted the original packet (prefix-free body)" \
	"target_packet=${PACKET_B} reply_event=${HUMAN_REPLY_EVENT_B} nested-info=${REPLY_NESTED_B} match=${MATCH_LINE}"
stop_sniffer "${SNIFF_PID}" "${SNIFF}"
trap - EXIT

SC_LOG "[${SCENARIO}] complete"
