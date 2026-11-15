#!/usr/bin/env bash
# =============================================================================
# Scenario: medre-reactions
#
# Radio tapbacks consumed by the medre bridge, and the cross-mesh peer
# reaction re-bridge that carries the r15 regression class:
#
# For each mesh (AlphaLab with the plain key, BetaLab with the skin-tone
# modifier key):
#   1. A human Matrix text is bridged to radio by BOTH bridges. The medre
#      bridge's own packet id (P_m) is read from the medre bridge's state
#      (outbound native ref). mmrelay cannot resolve P_m (it holds only its
#      own packet for the same text), so the tapback produces NO mmrelay
#      event: the medre reacted event is the only reaction in the room.
#   2. A radio tapback injected from the mesh's PEER node with reply_id=P_m
#      must produce a medre message.reacted event carrying:
#        * the EXACT relation key (byte-exact; skin-tone modifier preserved),
#        * a numeric radio-native target (hint target_native_ref
#          native_message_id == P_m, native_channel_id == "0"),
#        * the m.emote fallback rendering carrying the key verbatim.
#      The event must be corpus-valid under the medre installed schema
#      authority.
#   3. Same-mesh inertness: the local mmrelay (same meshnet as the medre bot)
#      must NOT re-bridge the medre reacted copy (no message_map row: rows
#      exist only after confirmed radio sends).
#   4. Cross-mesh re-bridge (r15 fixes): the FAR mmrelay, trusting the medre
#      bot via trusted_relays, bridges the peer reacted event back to ITS
#      mesh:
#        * WITHOUT re-prefixing: the transmitted body must equal the medre
#          event's authored body (stripped) - the far relay must not prepend
#          its own attribution prefix to an already-authored peer body;
#        * WITHIN the radio UTF-8 byte limit (<= mmrelay
#          DEFAULT_MESSAGE_TRUNCATE_BYTES == medre max_text_bytes == 227);
#        * as a plain authored send (no reply_id), exactly once.
#
# Radio observation is on the FAR mesh's PEER node, armed before the tapback
# injection: the injection contends only the NEAR peer's client slot, the
# sniffer holds the far one (one-client-per-node rule).
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=lib/medre_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/medre_lib.sh"

require_rig
require_medre_bridge
resolve_rooms

SCENARIO="medre-reactions"
ALICE_TOKEN="$(lib_py mx_user.py token "${HUMAN_ALICE_LOCALPART}" | tr -d '"')"
BOB_TOKEN="$(lib_py mx_user.py token "${HUMAN_BOB_LOCALPART}" | tr -d '"')"
[[ -n ${ALICE_TOKEN} && ${ALICE_TOKEN} != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_ALICE_LOCALPART}" \
		"the rig must provision @alice and publish LAB_TOKEN_ALICE (scenarios/README.md)"
[[ -n ${BOB_TOKEN} && ${BOB_TOKEN} != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_BOB_LOCALPART}" \
		"the rig must provision @bob and publish LAB_TOKEN_BOB (scenarios/README.md)"

# run_case VARIANT ROOM SEED_LOCALPART MEDRE_DB LOCAL_MAP_DB INJECT_HOST
#          INJECT_PORT FAR_PEER_HOST FAR_PEER_PORT KEY
run_case() {
	local variant="$1" room_id="$2" seed_localpart="$3" mesh_db="$4"
	local local_map_db="$5" inject_host="$6" inject_port="$7"
	local far_host="$8" far_port="$9" key="${10}"
	local marker since_ms out event_id medre_packet=""
	local react_json react_id react_body match_line sniffed_text byte_count rows_count

	marker="MEDRE-REACT-${variant}-$(SC_UID)"

	# --- Act 1: seed text bridged by both bridges -------------------------------
	capture out lib_py mx_user.py send "${seed_localpart}" "${room_id}" "${marker}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] human seed send failed" "${out}"
	event_id="$(jq_get "${out}" event_id | tr -d '"')"

	# medre's own radio packet for the seed (outbound native ref in its
	# state; medre writes that ref moments after ingest, so the poll must
	# REQUIRE it instead of returning at first sight of the event).
	capture out medre_state_py events-for-native "${mesh_db}" "${event_id}" \
		--wait "${RADIO_WAIT_SECONDS}" --require-outbound-radio
	if ((SC_STATUS == 0)); then
		medre_packet="$(printf '%s' "${out}" | tools_exec python3 -c '
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
')"
	fi
	[[ -n ${medre_packet} ]] ||
		SC_FAIL "[${variant}] medre state holds no outbound radio packet for the seed text (medre did not bridge it)" \
			"db=${mesh_db} matrix_event=${event_id} events=${out}"
	SC_PASS "[${variant}] seed text bridged; medre packet known from medre state" \
		"matrix_event=${event_id} medre_packet=${medre_packet} db=${mesh_db}"

	# --- Far-mesh sniffer armed BEFORE the tapback -------------------------------
	start_sniffer SNIFF "${far_host}" "${far_port}" \
		--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
		--expect-text "${key}"
	trap 'stop_sniffer "${SNIFF_PID}" "${SNIFF}"' EXIT

	# --- Act 2: radio tapback targeting the medre packet -------------------------
	since_ms="$(date +%s%3N)"
	capture out lib_py radio.py send-reaction "${inject_host}" "${inject_port}" \
		"${key}" "${medre_packet}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] tapback injection failed" "${out}"
	SC_PASS "[${variant}] tapback injected against the medre packet" \
		"key=${key} target_packet=${medre_packet}"

	# --- medre reacted event -------------------------------------------------------
	capture react_json lib_py mx_events.py wait "${room_id}" \
		--sender "${MEDRE_BOT_MXID}" \
		--require-envelope --envelope-kind message.reacted \
		--relation-info reaction --native-ref-is-numeric --native-ref-channel "0" \
		--hint-target-msg-id "${medre_packet}" --hint-key-equals "${key}" \
		--since-ms "${since_ms}" --timeout "${MATRIX_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] no medre reacted event with the exact key ${key} and numeric target ${medre_packet}" "${react_json}"
	react_id="$(jq_get "${react_json}" event.event_id | tr -d '"')"
	medre_assert_valid "${react_json}" "[${variant}] medre reacted event"
	local msgtype
	msgtype="$(jq_get "${react_json}" event.content.msgtype | tr -d '"')"
	[[ ${msgtype} == "m.emote" ]] ||
		SC_FAIL "[${variant}] medre radio-derived reaction must render as the m.emote fallback" "${react_json}"
	SC_PASS "[${variant}] medre emitted the exact reaction key with a numeric radio-native target" \
		"key=${key} target_packet=${medre_packet} event=${react_id}"

	# --- Same-mesh inertness --------------------------------------------------------
	capture rows lib_py maps.py rows-for-event "${local_map_db}" "${react_id}"
	rows_count=$(jq_get "${rows}" 'rows' | grep -c '"meshtastic_id"' || true)
	((rows_count == 0)) ||
		SC_FAIL "[${variant}] local mmrelay re-bridged the medre reacted copy (map row written after a confirmed radio send)" \
			"db=${local_map_db} rows=${rows} reacted_event=${react_id}"
	SC_PASS "[${variant}] mmrelay peer copy inert on the origin mesh (no map row)" \
		"reacted_event=${react_id} db=${local_map_db}"

	# --- Far-mesh re-bridge: no re-prefix, byte limit, plain send -------------------
	if wait_until "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" grep -q '"match"' "${SNIFF}"; then
		match_line="$(grep -o '{"match":.*}' "${SNIFF}" | head -1)"
	else
		match_line=""
	fi
	[[ -n ${match_line} ]] ||
		SC_FAIL "[${variant}] far relay never bridged the medre peer reaction to its mesh" \
			"sniffer log: $(cat "${SNIFF}")" $'\n'"reacted_event=${react_id} key=${key}"

	# The m.emote fallback body legitimately CONTAINS double quotes
	# (reacted 🦄 to "<preview>") - extract via JSON (83b6869), never tr -d '"'.
	react_body="$(printf '%s' "${react_json}" | tools_exec python3 -c '
import json, sys
payload = json.load(sys.stdin)
print(payload.get("event", {}).get("content", {}).get("body") or "")')"
	sniff_verdict="$(printf '%s' "${match_line}" | tools_exec python3 -c "
import json, sys
payload = json.load(sys.stdin)
view = payload.get('match') or {}
text = view.get('text') or ''
medre_body = sys.argv[1]
key = sys.argv[2]
limit = int(sys.argv[3])
problems = []
if view.get('reply_id') is not None:
    problems.append(
        f'peer reaction must relay as a plain authored send (reply_id={view.get(\"reply_id\")})'
    )
n = len(text.encode('utf-8'))
if n > limit:
    problems.append(f'{n} UTF-8 bytes exceeds the radio limit {limit} (r15 byte-limit regression)')
stripped = medre_body.strip()
# No-re-prefix with truncation tolerance: the transmitted body equals the
# authored body OR is its radio-truncation (a prefix of the authored body
# within the byte limit) - the strict inequality alone would misjudge a
# legitimate truncation as a re-prefix.
truncated_ok = len(text.encode('utf-8')) <= limit and text and stripped.startswith(text)
if text != stripped and not truncated_ok:
    problems.append(
        'far relay re-prefixed or altered the authored peer body (r15 no-re-prefix regression):'
        f' sniffed={text!r} medre_body={stripped!r}'
    )
if key not in text:
    problems.append(f'exact key {key!r} missing from the transmitted body')
print(json.dumps({'problems': problems, 'sniffed': text, 'medre_body': stripped}))
" "${react_body}" "${key}" "${MEDRE_TRUNCATE_BYTES}")"
	printf '%s' "${sniff_verdict}" | grep -q '"problems": \[\]' ||
		SC_FAIL "[${variant}] far-mesh peer reaction re-bridge violates the r15 contract" \
			"verdict: ${sniff_verdict}" $'\n'"match: ${match_line}" $'\n'"medre reacted event: ${react_json}"

	byte_count="$(printf '%s' "${match_line}" | tools_exec python3 -c '
import json, sys
view = (json.load(sys.stdin).get("match") or {})
print(len((view.get("text") or "").encode("utf-8")))
')"
	SC_PASS "[${variant}] far relay bridged the peer reaction without re-prefixing, within the byte limit" \
		"sniffed_bytes=${byte_count} limit=${MEDRE_TRUNCATE_BYTES} medre_event=${react_id}" $'\n'"verdict: ${sniff_verdict}"

	stop_sniffer "${SNIFF_PID}" "${SNIFF}"
	trap - EXIT
}

run_case "alpha" "${ROOM_ALPHA_CH0_ID}" "${HUMAN_ALICE_LOCALPART}" \
	"${MEDRE_DB_A}" "${MAP_DB_A}" \
	"${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	"🦄"

run_case "beta" "${ROOM_BETA_CH0_ID}" "${HUMAN_BOB_LOCALPART}" \
	"${MEDRE_DB_B}" "${MAP_DB_B}" \
	"${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	"${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"👍🏽"

SC_LOG "[${SCENARIO}] complete"
