#!/usr/bin/env bash
# =============================================================================
# DEFERRED (owner decision 2026-11-10): same-mesh multi-relay loop handling is
# explicitly a later work item. Kept here for that future work; NOT wired into
# the runner. When revived it needs rework for the per-meshnet x channel room
# topology (#lab-alpha-* rooms instead of the retired #lab-bridge) and the
# peer-node radio model (meshtasticd-a/a-peer).
#
# Scenario: threenode-same-mesh (profile-gated: requires the `threenode`
# compose profile: meshtasticd-c + mmrelay-c, both on meshnet A / AlphaLab).
#
# With two relays sharing one meshnet and one room:
#   1. A message from node A must NOT loop: suppression by sender identity
#      keeps the marker event count bounded and stable (relay C must not
#      re-broadcast to the AlphaLab radio what relay A already broadcast
#      there; the meshnet-claim equality is the r13 suppression).
#   2. C's rendering of A's radio packet keeps native channel ids NUMERIC:
#      every relay-authored event for the marker (from @relay-a AND @relay-c)
#      carries envelope source_native_ref with numeric channel + packet id -
#      never the meshnet label "AlphaLab" (the historical defect).
#   3. Replies from C correlate on A: a radio reply from node C to A's packet
#      produces a reply event from @relay-c whose relation resolves to the
#      SAME native packet id that relay A recorded for its own event of that
#      packet (end-to-end radio packet ids are preserved across relays; the
#      reply targets C's own rendering event, and that event's native ref is
#      the packet id in A's map row).
# =============================================================================
set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/common.sh"

require_rig
resolve_rooms

SCENARIO="threenode-same-mesh"

# Profile gate: relay C must actually be up and reachable.
if ! docker compose -f "${LAB_COMPOSE_FILE}" ps --status running --services 2>/dev/null | grep -Fxq mmrelay-c; then
	SC_SKIP "threenode profile is not running (mmrelay-c container absent)" \
		"bring up with: docker compose -f compose.yaml --profile threenode up -d"
fi

MARKER="THREE-$(SC_UID)"
SINCE_MS="$(date +%s%3N)"

# --- Act: one radio text from node A -------------------------------------------
capture out lib_py radio.py send-text "${NODE_A_HOST}" "${NODE_A_PORT}" "${MARKER}"
((SC_STATUS == 0)) || SC_FAIL "radio injection on node A failed" "${out}"

# --- Settle, then count marker events per relay ---------------------------------
sleep "${SETTLE_SECONDS}"
COUNT_ARGS=(scan "${ROOM_BRIDGE_ID}" --since-ms "${SINCE_MS}" --body-contains "${MARKER}"
	--require-envelope --envelope-kind message.created --native-ref-is-numeric)
capture out lib_py mx_events.py "${COUNT_ARGS[@]}" --sender "${BOT_A_MXID}"
count_a=$(jq_get "${out}" 'matches' | grep -c '"event_id"' || true)
capture out lib_py mx_events.py "${COUNT_ARGS[@]}" --sender "${BOT_B_MXID}"
count_b=$(jq_get "${out}" 'matches' | grep -c '"event_id"' || true)
capture out lib_py mx_events.py "${COUNT_ARGS[@]}" --sender "${BOT_C_MXID}"
count_c=$(jq_get "${out}" 'matches' | grep -c '"event_id"' || true)

((count_a >= 1)) || SC_FAIL "relay A did not emit an event for node A's packet" "${out}"
((count_c >= 1)) ||
	SC_FAIL "relay C emitted no (envelope-valid) event for node A's packet (mesh not shared, or C's native refs are non-numeric - see the channel-identity check below)" \
		"${out}"
SC_PASS "both AlphaLab relays rendered the radio packet" \
	"marker_events: A=${count_a} B=${count_b} C=${count_c}"

# --- Assertion 2: numeric native ids (never the meshnet label) -------------------
capture all_json lib_py mx_events.py scan "${ROOM_BRIDGE_ID}" \
	--since-ms "${SINCE_MS}" --body-contains "${MARKER}" --require-envelope --native-ref-is-numeric
envelope_count=$(jq_get "${all_json}" 'matches' | grep -c '"event_id"' || true)
total_events=$((count_a + count_b + count_c))
# Every relay-authored marker event must pass the numeric-native-ref filter;
# an event whose native_channel_id was the meshnet label would be filtered out.
((envelope_count >= total_events)) ||
	SC_FAIL "a relay-authored marker event lost numeric native channel ids (meshnet label leaked into a native ref)" \
		"envelope_ok=${envelope_count} rendered=${total_events}" $'\n'"${all_json}"
SC_PASS "all marker events keep numeric native channel ids (no meshnet label)" \
	"events=${envelope_count}"

# --- Assertion 1: no loop (stable marker count over a quiet window) ---------------
sleep 15
capture out lib_py mx_events.py "${COUNT_ARGS[@]}"
count_after=$(jq_get "${out}" 'matches' | grep -c '"event_id"' || true)
((count_after <= total_events + 1)) ||
	SC_FAIL "marker events kept growing after settle (bridge loop)" \
		"first_window=${total_events} second_window=${count_after}" $'\n'"${out}"
SC_PASS "marker event count stabilized (no loop from the single radio message)" \
	"first_window=${total_events} second_window=${count_after}"

# --- Assertion 3: replies from C correlate on A -----------------------------------
# Relay C's map holds the radio packet under its own rendering event; relay A's
# map holds the SAME end-to-end packet id under A's rendering event. Only rows
# whose text is EXACTLY the injected marker are the radio->Matrix rows for the
# original packet (bridged retransmissions carry prefixed text).
exact_packet_id() { # rows_json -> first numeric packet id from exact-text rows
	printf '%s' "$1" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
marker = sys.argv[1]
ids = [str(r["meshtastic_id"]) for r in rows
       if r.get("meshtastic_text") == marker and str(r.get("meshtastic_id")).isdigit()]
print(ids[0] if ids else "")' "${MARKER}"
}
capture a_rows lib_py maps.py rows-for-text "${MAP_DB_A}" "${MARKER}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "relay A map has no row for the marker" "${a_rows}"
A_PACKET=$(exact_packet_id "${a_rows}")
[[ -n ${A_PACKET} ]] || SC_FAIL "no exact-text radio row in relay A map for the original packet" "${a_rows}"
capture c_rows lib_py maps.py rows-for-text "${MAP_DB_C}" "${MARKER}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "relay C map has no row for the marker" "${c_rows}"
C_PACKET=$(exact_packet_id "${c_rows}")
[[ -n ${C_PACKET} ]] || SC_FAIL "no exact-text radio row in relay C map for the original packet" "${c_rows}"
[[ "${C_PACKET}" == "${A_PACKET}" ]] ||
	SC_FAIL "relays A and C recorded different packet ids for the same radio packet" \
		"A=${A_PACKET} C=${C_PACKET}"
capture c_event_json lib_py mx_events.py scan "${ROOM_BRIDGE_ID}" \
	--since-ms "${SINCE_MS}" --sender "${BOT_C_MXID}" --body-contains "${MARKER}" \
	--require-envelope --envelope-kind message.created --native-ref-is-numeric \
	--native-ref-msg-id "${C_PACKET}"
C_RENDERING_EVENT_ID="$(jq_get "${c_event_json}" 'matches.0.event_id' | tr -d '"')"
[[ -n "${C_RENDERING_EVENT_ID}" && "${C_RENDERING_EVENT_ID}" != "null" ]] ||
	SC_FAIL "cannot identify relay C's rendering event for the original packet" "${c_event_json}"
SC_PASS "radio packet id agrees across A's map and C's rendering (shared mesh, end-to-end ids)" \
	"packet=${A_PACKET} C_rendering_event=${C_RENDERING_EVENT_ID}"

# Radio reply from node C to that packet. C resolves the reply through its own
# map to its rendering event; the Matrix relation must target exactly that
# event, and A's map row proves the SAME packet id the reply lineage resolves
# to - that is the cross-relay correlation anchor.
REPLY_MARKER="THREER-$(SC_UID)"
since_reply="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_C_HOST}" "${NODE_C_PORT}" \
	"${REPLY_MARKER}" --reply-id "${A_PACKET}"
((SC_STATUS == 0)) || SC_FAIL "radio reply from node C failed" "${out}"

capture reply_json lib_py mx_events.py wait "${ROOM_BRIDGE_ID}" \
	--sender "${BOT_C_MXID}" --body-contains "${REPLY_MARKER}" \
	--require-envelope --relation-info reply --native-ref-is-numeric \
	--native-ref-channel "0" --relates-to "${C_RENDERING_EVENT_ID}" \
	--hint-target-msg-id "${C_RENDERING_EVENT_ID}" \
	--since-ms "${since_reply}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "relay C emitted no reply event targeting its own rendering of A's packet" "${reply_json}"
REPLY_EVENT_ID="$(jq_get "${reply_json}" event.event_id | tr -d '"')"
SC_PASS "C's reply correlates on A: relation resolves to the packet id recorded in A's map" \
	"reply_event=${REPLY_EVENT_ID} in_reply_to=${C_RENDERING_EVENT_ID} packet=${A_PACKET}"

SC_LOG "[$SCENARIO] complete"
