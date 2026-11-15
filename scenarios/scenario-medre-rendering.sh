#!/usr/bin/env bash
# =============================================================================
# Scenario: medre-rendering
#
# Prefixes and formatting across BOTH bridges, both directions:
#
# Forward (radio -> room): one radio text per mesh, rendered by @relay-a AND
#   the medre bot. Each rendering must:
#     * carry the authored text exactly once (no doubled body, no
#       duplicate-prefix re-render of the same text);
#     * leak no internal fields in the body (no native node id !hex, no
#       "TEXT_MESSAGE_APP", no raw packet id; corpus validation covers the
#       envelope/context side, including meshnet-label isolation to
#       native_facts);
#     * keep formatted_body consistent with the plain body (marker present).
#
# Reverse (room -> radio): one human Matrix text per mesh, bridged to radio
#   by BOTH bridges. Every radio transmission captured on the mesh's PEER
#   node must:
#     * carry the authored text exactly once;
#     * stay within the radio UTF-8 byte limit (<= mmrelay's
#       DEFAULT_MESSAGE_TRUNCATE_BYTES == medre max_text_bytes == 227);
#     * carry at most ONE meshnet-label occurrence (a double prefix -
#       e.g. "[X/AlphaLab]: [X/AlphaLab]: text" - is the regression);
#     * come from at least two distinct sender nodes: both bridges actually
#       rendered and transmitted the room text.
#
# Human tokens are required for the reverse direction (mx_user.py); the
# scenario SKIPs with the rig requirement when the rig has not provisioned
# the simulated humans.
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=lib/medre_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/medre_lib.sh"

require_rig
require_medre_bridge
resolve_rooms

SCENARIO="medre-rendering"

ALICE_TOKEN="$(lib_py mx_user.py token "${HUMAN_ALICE_LOCALPART}" | tr -d '"')"
BOB_TOKEN="$(lib_py mx_user.py token "${HUMAN_BOB_LOCALPART}" | tr -d '"')"
[[ -n ${ALICE_TOKEN} && ${ALICE_TOKEN} != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_ALICE_LOCALPART}" \
		"the rig must provision @alice and publish LAB_TOKEN_ALICE (scenarios/README.md)"
[[ -n ${BOB_TOKEN} && ${BOB_TOKEN} != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_BOB_LOCALPART}" \
		"the rig must provision @bob and publish LAB_TOKEN_BOB (scenarios/README.md)"

check_body() { # LABEL JSON_PATH_IN_EVENT MARKER EXPECT_BAD_SUBSTRINGS...
	local label="$1" event_json="$2" marker="$3"
	shift 3
	local findings
	findings="$(printf '%s' "${event_json}" | tools_exec python3 -c "
import json, sys
payload = json.load(sys.stdin)
current = payload
for segment in sys.argv[1].split('.'):
    if isinstance(current, list):
        current = current[int(segment)]
    elif isinstance(current, dict):
        current = current.get(segment)
    else:
        current = None
        break
body = current if isinstance(current, str) else ''
marker = sys.argv[2]
banned = sys.argv[3:]
problems = []
count = body.count(marker)
if count != 1:
    problems.append(f'authored text occurrences={count} (expected 1)')
for needle in banned:
    if needle and needle in body:
        problems.append(f'leaked internal field in body: {needle!r}')
if not body:
    problems.append('body missing')
print('|'.join(problems))
" "event.content.body" "${marker}" "$@")"
	[[ -z ${findings} ]] ||
		SC_FAIL "${label}: body rendering violations" "${findings}" $'\n'"event: ${event_json}"
}

run_forward() { # VARIANT ROOM INJECT_HOST INJECT_PORT ORIGIN_BOT OTHER_BOT MESHNET
	local variant="$1" room_id="$2" inject_host="$3" inject_port="$4"
	local origin_bot="$5" other_bot="$6" meshnet="$7"
	local marker since_ms out ev count

	marker="RENDER-F-${variant}-$(SC_UID)"
	since_ms="$(date +%s%3N)"
	capture out lib_py radio.py send-text "${inject_host}" "${inject_port}" "${marker}"
	((SC_STATUS == 0)) || SC_FAIL "[forward ${variant}] radio injection failed" "${out}"

	for bot in "${origin_bot}" "${other_bot}"; do
		capture out lib_py mx_events.py wait "${room_id}" \
			--sender "${bot}" --body-contains "${marker}" \
			--require-envelope --envelope-kind message.created \
			--native-ref-is-numeric --since-ms "${since_ms}" \
			--timeout "${MATRIX_WAIT_SECONDS}"
		((SC_STATUS == 0)) ||
			SC_FAIL "[forward ${variant}] no canonical event from ${bot} carrying the marker" "${out}"
		medre_assert_valid "${out}" "[forward ${variant}] ${bot} event"
		check_body "[forward ${variant}] ${bot} body" "${out}" "${marker}" \
			"TEXT_MESSAGE_APP"
		# formatted_body must carry the same authored text (HTML rendering).
		count="$(printf '%s' "${out}" | tools_exec python3 -c "
import json, sys
payload = json.load(sys.stdin)
body = payload.get('event', {}).get('content', {}).get('formatted_body') or ''
print(body.count(sys.argv[1]))
" "${marker}")"
		[[ ${count} -ge 1 ]] ||
			SC_FAIL "[forward ${variant}] ${bot} formatted_body lost the authored text" "${out}"
		SC_PASS "[forward ${variant}] ${bot} renders the authored text once, no leaked internal fields" \
			"event=$(jq_get "${out}" event.event_id | tr -d '"') meshnet_label_isolated_in_native_facts=true (corpus)"
	done
}

run_reverse() { # VARIANT ROOM SENDER_TOKEN SENDER_LOCALPART PEER_HOST PEER_PORT MESHNET
	local variant="$1" room_id="$2" token="$3" localpart="$4"
	local peer_host="$5" peer_port="$6" meshnet="$7"
	local marker event_json out analysis result

	marker="RENDER-R-${variant}-$(SC_UID)"
	# The collector prints its result line only when its window closes; the
	# window must span the human send plus BOTH bridges' radio sends.
	start_collector COLLECT "${peer_host}" "${peer_port}" \
		--timeout "${SNIFF_WAIT_SECONDS}" --contains "${marker}"
	trap 'stop_collector "${COLLECT_PID}" "${COLLECT}"' EXIT

	capture event_json medre_lib_py mx_user.py send "${localpart}" "${room_id}" "${marker}"
	((SC_STATUS == 0)) || SC_FAIL "[reverse ${variant}] human send failed" "${event_json}"
	SC_PASS "[reverse ${variant}] human text sent into ${room_id}" \
		"event=$(jq_get "${event_json}" event_id | tr -d '"') marker=${marker}"

	wait_until "$((SNIFF_WAIT_SECONDS + 10))" grep -q '"packets"' "${COLLECT}" ||
		SC_FAIL "[reverse ${variant}] collector window ended without a result" \
			"collector log: $(cat "${COLLECT}")"
	result="$(collector_result "${COLLECT}")"
	[[ -n ${result} ]] || SC_FAIL "[reverse ${variant}] collector produced no result" \
		"collector log: $(cat "${COLLECT}")"

	analysis="$(printf '%s' "${result}" | tools_exec python3 -c "
import json, sys
data = json.load(sys.stdin)
marker = sys.argv[1]
meshnet = sys.argv[2]
limit = int(sys.argv[3])
packets = data.get('packets', [])
senders = {str(p.get('from_id')) for p in packets}
problems = []
if len(senders) < 2:
    problems.append(f'expected BOTH bridges to transmit (distinct senders={len(senders)}: {sorted(senders)})')
for p in packets:
    text = p.get('text') or ''
    label = f\"packet {p.get('packet_id')} from {p.get('from_id')}\"
    if text.count(marker) != 1:
        problems.append(f'{label}: authored text occurrences={text.count(marker)} (expected 1)')
    n = len(text.encode('utf-8'))
    if n > limit:
        problems.append(f'{label}: {n} UTF-8 bytes exceeds the radio limit {limit}')
    if meshnet and text.count(meshnet) > 1:
        problems.append(f'{label}: meshnet label {meshnet!r} appears {text.count(meshnet)}x (double prefix?)')
print(json.dumps({'problems': problems, 'packets': packets, 'senders': sorted(senders)}))
" "${marker}" "${meshnet}" "${MEDRE_TRUNCATE_BYTES}")"

	printf '%s' "${analysis}" | grep -q '"problems": \[\]' ||
		SC_FAIL "[reverse ${variant}] radio rendering violations" \
			"analysis: ${analysis}" $'\n'"collector: ${result}"
	SC_PASS "[reverse ${variant}] both bridges rendered the room text for radio, each within the byte limit, no double prefix" \
		"senders=$(jq_get "${analysis}" senders) packets=$(jq_get "${analysis}" packets)"
	stop_collector "${COLLECT_PID}" "${COLLECT}"
	trap - EXIT
}

run_forward "alpha-ch0" "${ROOM_ALPHA_CH0_ID}" "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${BOT_A_MXID}" "${MEDRE_BOT_MXID}" "${MESHNET_A}"
run_forward "beta-ch0" "${ROOM_BETA_CH0_ID}" "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	"${BOT_B_MXID}" "${MEDRE_BOT_MXID}" "${MESHNET_B}"

run_reverse "alpha-ch0" "${ROOM_ALPHA_CH0_ID}" "${ALICE_TOKEN}" "${HUMAN_ALICE_LOCALPART}" \
	"${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${MESHNET_A}"
run_reverse "beta-ch0" "${ROOM_BETA_CH0_ID}" "${BOB_TOKEN}" "${HUMAN_BOB_LOCALPART}" \
	"${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" "${MESHNET_B}"

SC_LOG "[${SCENARIO}] complete"
