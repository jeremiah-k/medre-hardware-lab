#!/usr/bin/env bash
# =============================================================================
# Scenario: medre-loop-inertness
#
# Both bridges see the same radio text; each bridge's room copy of it must
# NOT be re-bridged to radio by the other. One radio text injected from the
# ALPHA PEER produces exactly ONE Alpha radio transmission - the injection
# itself. Not two (a bridge re-bridging the other's copy), not four (a
# cascade).
#
# Assertions:
#   1. BOTH bridges emit a corpus-valid canonical event for the packet
#      (@relay-a and the medre bot in #lab-alpha-ch0).
#   2. Same-mesh inertness, state evidence (race-free: rows exist only after
#      confirmed radio sends):
#        * no mmrelay message_map row for the medre bot's Matrix copy
#          (mmrelay same-meshnet peer guard; @medre is in trusted_relays per
#          the rig contract);
#        * no outbound radio native ref for @relay-a's Matrix copy in the
#          medre bridge's state (the candidate medre-adapter-matrix 75d961d8
#          has NO operator-facing MXID allowlist - inbound peer claims are
#          unverified source observations per core 93f43311 - so medre-side
#          inertness rests on its route/suppression model, exactly what this
#          assertion probes; ingestion without re-delivery is reported).
#   3. Radio evidence: the a-peer collector (armed immediately after the
#      blocking injection - the one-client-per-node rule forbids holding the
#      peer slot during the injection) captures ZERO packets carrying the
#      marker during its window; any capture would be a re-bridged echo.
#      The echo trigger (peer-copy processing) can only start after the
#      Matrix copies exist, which the collector window spans. The window is
#      also POISON-gated: if the collector's transport dropped queued
#      packets mid-window (client evicted/reconnected), the clean zero is
#      not evidence and the scenario FAILS instead of passing it.
#   4. No map growth storms: marker-referencing rows in relay A's map, relay
#      B's map and the medre state are stable across a settle window (a
#      re-bridge cascade grows them continuously).
#
# Observation-window caveat (triage 2026-11-11, "cannot fully observe while
# queues are saturated"): mmrelay map rows are written only after CONFIRMED
# radio sends, so an echo starved inside a saturated send queue is INVISIBLE
# in this window - its row lands (and its packet airs) only after the
# window closed. The starvation bound (assertion 5) guards that class: the
# relays' send queues must stay shallow in the current container generation,
# or the verdict is not a clean inertness proof.
#
#   5. Starvation bound: the mmrelay send queues' max queue position
#      ("Queued message (N/500)" log lines, current container generation) is
#      bounded - a saturated queue means starved echoes may exist outside
#      the observation window.
#
# Observation (not asserted): the BETA-side transmission count is REPORTED.
# Relay B bridges Matrix copies from the Alpha rooms to BetaLab radio - that
# is the documented cross-mesh delivery path. Because B receives BOTH bots'
# copies of the same packet, the far mesh may legitimately see two
# transmissions of one authored text (one per peer copy). Cross-peer dedup
# is future loop-hardening work (scenarios/deferred/ threenode-same-mesh);
# flip this scenario's assertion together with that product change.
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=lib/medre_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/medre_lib.sh"

require_rig
require_medre_bridge
resolve_rooms

SCENARIO="medre-loop-inertness"
MARKER="LOOP-$(SC_UID)"
WINDOW=$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS + 60))

# Peer node identity for from_id attribution (must run BEFORE any collector
# holds the peer's single API client slot).
capture info_out medre_lib_py medre_radio.py info "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}"
((SC_STATUS == 0)) || SC_FAIL "cannot read peer node identity" "${info_out}"
PEER_NODE_NUM="$(jq_get "${info_out}" my_node_num)"
[[ ${PEER_NODE_NUM} =~ ^[0-9]+$ ]] ||
	SC_FAIL "peer node identity unusable" "${info_out}"

# --- Act: inject the radio text (no collector armed yet), then arm ------------
SINCE_MS="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${MARKER}"
((SC_STATUS == 0)) || SC_FAIL "radio injection failed" "${out}"
PACKET_ID="$(jq_get "${out}" packet_id | tr -d '"')"
SC_PASS "radio text injected from the Alpha peer node" \
	"packet_id=${PACKET_ID} marker=${MARKER}"

start_collector COLLECT_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" --timeout "${WINDOW}"
start_collector COLLECT_B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" --timeout "${WINDOW}"
trap 'stop_collector "${COLLECT_A_PID:-}" "${COLLECT_A:-}"; stop_collector "${COLLECT_B_PID:-}" "${COLLECT_B:-}"' EXIT

# --- Assertion 1: both bridges' room copies, corpus-valid ----------------------
capture relay_json lib_py mx_events.py wait "${ROOM_ALPHA_CH0_ID}" \
	--sender "${BOT_A_MXID}" --body-contains "${MARKER}" \
	--require-envelope --envelope-kind message.created \
	--source-channel "0" --native-ref-is-numeric --native-ref-msg-id "${PACKET_ID}" \
	--since-ms "${SINCE_MS}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "no canonical @relay-a copy of the radio text" "${relay_json}"
RELAY_EVENT_ID="$(jq_get "${relay_json}" event.event_id | tr -d '"')"
medre_assert_valid "${relay_json}" "@relay-a copy"

capture medre_json lib_py mx_events.py wait "${ROOM_ALPHA_CH0_ID}" \
	--sender "${MEDRE_BOT_MXID}" --body-contains "${MARKER}" \
	--require-envelope --envelope-kind message.created \
	--source-channel "0" --native-ref-is-numeric --native-ref-msg-id "${PACKET_ID}" \
	--since-ms "${SINCE_MS}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "no canonical medre-bot copy of the radio text (medre did not bridge the packet, or its envelope disagrees)" "${medre_json}"
MEDRE_EVENT_ID="$(jq_get "${medre_json}" event.event_id | tr -d '"')"
medre_assert_valid "${medre_json}" "medre-bot copy"
SC_PASS "both bridges emitted corpus-valid copies of the same packet" \
	"packet=${PACKET_ID} relay=${RELAY_EVENT_ID} medre=${MEDRE_EVENT_ID}"

# --- Assertion 2: same-mesh inertness, state evidence --------------------------
capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${MEDRE_EVENT_ID}"
rows_count=$(jq_get "${rows}" 'rows' | grep -c '"meshtastic_id"' || true)
((rows_count == 0)) ||
	SC_FAIL "mmrelay re-bridged the medre bot's room copy (map row written after a confirmed radio send)" \
		"db=${MAP_DB_A} rows=${rows} medre_event=${MEDRE_EVENT_ID}"
SC_PASS "mmrelay holds no map row for the medre copy (same-meshnet peer guard)" \
	"medre_event=${MEDRE_EVENT_ID} db=${MAP_DB_A}"

capture out medre_state_py events-for-native "${MEDRE_DB_A}" "${RELAY_EVENT_ID}"
medre_state_verdict="$(printf '%s' "${out}" | tools_exec python3 -c '
import json, re, sys
data = json.load(sys.stdin)
events = data.get("events", [])
outbound = []
for event in events:
    for ref in event.get("native_refs", []):
        if ref.get("direction") == "outbound" and re.fullmatch(
            r"[0-9]+", str(ref.get("native_message_id") or "")
        ):
            outbound.append(ref.get("native_message_id"))
print(json.dumps({"events": len(events), "outbound_radio_refs": outbound}))
')"
printf '%s' "${medre_state_verdict}" | grep -q '"outbound_radio_refs": \[\]' ||
	SC_FAIL "medre re-delivered @relay-a's Matrix copy to radio (outbound radio ref exists)" \
		"verdict: ${medre_state_verdict}" $'\n'"relay_event=${RELAY_EVENT_ID} db=${MEDRE_DB_A}"
SC_PASS "medre state holds no outbound radio ref for the @relay-a copy (no re-delivery)" \
	"verdict: ${medre_state_verdict}"

# --- Assertion 4 (snapshot): marker-referencing rows, pre-settle ---------------
marker_counts() {
	capture c_a lib_py maps.py rows-for-text "${MAP_DB_A}" "${MARKER}"
	local a b m
	a=$(jq_get "${c_a}" rows | grep -c '"meshtastic_id"' || true)
	capture c_b lib_py maps.py rows-for-text "${MAP_DB_B}" "${MARKER}"
	b=$(jq_get "${c_b}" rows | grep -c '"meshtastic_id"' || true)
	capture c_m medre_state_py events-for-text "${MEDRE_DB_A}" "${MARKER}"
	m=$(jq_get "${c_m}" events | grep -c '"event_id"' || true)
	printf 'A=%s B=%s MEDRE=%s' "${a}" "${b}" "${m}"
}
BEFORE_COUNTS="$(marker_counts)"
SC_LOG "[${SCENARIO}] marker row snapshot (pre-settle): ${BEFORE_COUNTS}"

# --- Settle, then re-snapshot (storm detection) --------------------------------
sleep "${SETTLE_SECONDS}"
sleep "${SETTLE_SECONDS}"
AFTER_COUNTS="$(marker_counts)"
[[ ${BEFORE_COUNTS} == "${AFTER_COUNTS}" ]] ||
	SC_FAIL "map growth storm: marker-referencing rows grew after settle" \
		"before: ${BEFORE_COUNTS}" $'\n'"after: ${AFTER_COUNTS}" $'\n'"a re-bridge cascade keeps writing rows"
SC_PASS "no map growth storm across the settle window" \
	"before: ${BEFORE_COUNTS}" $'\n'"after: ${AFTER_COUNTS}"

# --- Assertion 5: send-queue starvation bound ----------------------------------
# queue_state LOG CONTAINER -> {"count","max_pos"} of the relay's
# "Queued message (N/500)" lines in the CURRENT container generation
# (same generation-bounded parsing as normal_lib.sh's queue_snapshot,
# c9acb3c; every tools_exec stage || true'd so a zero in-generation count
# is a valid result under pipefail).
queue_state() { # LOG CONTAINER -> JSON
	local log="$1" container="$2" cutoff out
	if tools_exec test -f "${log}" >/dev/null 2>&1; then
		cutoff="$(docker inspect --format '{{.State.StartedAt}}' "${container}" 2>/dev/null |
			cut -c1-19 | tr 'T' ' ')"
		if [[ -n ${cutoff} ]]; then
			# shellcheck disable=SC2016 # Child-language syntax or named output, not a host-shell read.
			out="$({ tools_exec awk -v cutoff="${cutoff}" '$1 " " $2 >= cutoff' "${log}" 2>/dev/null || true; } |
				{ tools_exec grep "Queued message (" 2>/dev/null || true; } |
				medre_lib_py normal_checks.py queue-parse)"
		else
			out="$({ tools_exec grep "Queued message (" "${log}" 2>/dev/null || true; } |
				medre_lib_py normal_checks.py queue-parse)"
		fi
	else
		out='{"count": 0, "max_pos": 0}'
	fi
	printf '%s' "${out}"
}
QUEUE_MAX_POS=5 # same backlog tolerance as normal-fanout's queue_assert_flat
queue_a="$(queue_state "${MEDRE_QUEUE_LOG_A:-/state/mmrelay-a/logs/mmrelay.log}" "${MEDRE_QUEUE_CONTAINER_A:-medre-lab-mmrelay-a}")"
queue_b="$(queue_state "${MEDRE_QUEUE_LOG_B:-/state/mmrelay-b/logs/mmrelay.log}" "${MEDRE_QUEUE_CONTAINER_B:-medre-lab-mmrelay-b}")"
pos_a="$(jq_get "${queue_a}" max_pos | tr -d '"')"
pos_b="$(jq_get "${queue_b}" max_pos | tr -d '"')"
count_a="$(jq_get "${queue_a}" count | tr -d '"')"
count_b="$(jq_get "${queue_b}" count | tr -d '"')"
((pos_a <= QUEUE_MAX_POS && pos_b <= QUEUE_MAX_POS)) ||
	SC_FAIL "mmrelay send queues saturated (starvation bound ${QUEUE_MAX_POS} exceeded): starved echoes are invisible inside this window" \
		"relay-a max_pos=${pos_a} (queued lines=${count_a}), relay-b max_pos=${pos_b} (queued lines=${count_b})" $'\n'"map rows are written only after CONFIRMED sends, so a saturated queue can hold unobserved re-bridges - the inertness verdict is not a clean proof on a saturated queue"
SC_PASS "starvation bound holds: send queues shallow in the current generation" \
	"relay-a max_pos=${pos_a}/${QUEUE_MAX_POS} (queued lines=${count_a}); relay-b max_pos=${pos_b}/${QUEUE_MAX_POS} (queued lines=${count_b})"

# --- Assertion 3: radio evidence on Alpha --------------------------------------
wait_collector() {
	local file="$1"
	wait_until "$((WINDOW + 15))" grep -q '"packets"' "${file}" ||
		SC_FAIL "collector window ended without a result" "collector log: $(cat "${file}")"
}
wait_collector "${COLLECT_A}"
alpha_result="$(collector_result "${COLLECT_A}")"
alpha_verdict="$(printf '%s' "${alpha_result}" | tools_exec python3 -c "
import json, sys
data = json.load(sys.stdin)
marker = sys.argv[1]
peer_num = int(sys.argv[2])
packets = data.get('packets', [])
foreign = [p for p in packets if marker in (p.get('text') or '')]
drops = data.get('transport_drops') or []
print(json.dumps({'marker_packets': foreign, 'count': len(foreign), 'seen_total': data.get('seen_total'), 'poisoned': bool(drops), 'transport_drops': drops}))
" "${MARKER}" "${PEER_NODE_NUM}")"
printf '%s' "${alpha_verdict}" | grep -q '"count": 0' ||
	SC_FAIL "Alpha radio re-bridged the text: a room copy was sent back to the mesh" \
		"verdict: ${alpha_verdict}" $'\n'"collector: ${alpha_result}" $'\n'"injected_packet=${PACKET_ID} (its own transmission predates the collector arming; any capture here is an echo)"
printf '%s' "${alpha_verdict}" | grep -q '"poisoned": false' ||
	SC_FAIL "Alpha zero-capture window POISONED (the collector's transport dropped queued packets mid-window): the clean zero is not inertness evidence" \
		"verdict: ${alpha_verdict}" $'\n'"a dropped-queued-packet window cannot distinguish radio silence from packet loss; rerun on a quiet rig"
SC_PASS "exactly one Alpha radio transmission per injected text (the injection itself; collector window clean, not poisoned)" \
	"injected_packet=${PACKET_ID} verdict: ${alpha_verdict}"

# --- Observation (REPORT ONLY): Beta-side peer-copy deliveries -----------------
wait_collector "${COLLECT_B}"
beta_result="$(collector_result "${COLLECT_B}")"
beta_count="$(printf '%s' "${beta_result}" | tools_exec python3 -c "
import json, sys
data = json.load(sys.stdin)
marker = sys.argv[1]
hits = [p for p in data.get('packets', []) if marker in (p.get('text') or '')]
print(len(hits))
" "${MARKER}")"
SC_LOG "NOTE: Beta-side marker transmissions observed: ${beta_count} (relay B delivering the Alpha rooms' peer copies to BetaLab radio; cross-peer dedup is deferred loop-hardening work - see scenario header)"
SC_PASS "loop-inertness observed on the origin mesh; Beta-side delivery count reported" \
	"beta_marker_transmissions=${beta_count}"

stop_collector "${COLLECT_A_PID}" "${COLLECT_A}"
stop_collector "${COLLECT_B_PID}" "${COLLECT_B}"
trap - EXIT

SC_LOG "[${SCENARIO}] complete"
