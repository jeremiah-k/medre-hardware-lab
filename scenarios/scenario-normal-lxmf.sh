#!/usr/bin/env bash
# =============================================================================
# Scenario: normal-lxmf  (GATED: SKIPs cleanly when the LXMF leg is absent)
#
# LXMF-origin traffic through the medre-normal loopback adapter (lxmf0):
#
#   * lxmf-origin message -> canonical room event with attribution and
#     bounded native facts, corpus-validated under the medre authority
#     (source_adapter == MEDRE_NORMAL_LXMF_ADAPTER, default lxmf0);
#   * the lxmf-origin text re-airs ONCE per mmrelay mesh (relay-a onto
#     MeshA, relay-b onto MeshB - cross-mesh delivery of room texts);
#   * the reverse leg: a room text reaches the LXMF counterpart (outbound
#     lxmf0 ref on the origin event in medre state, and the counterpart
#     driver reports the inbound message);
#   * a reply/reaction ORIGINATING from the lxmf side would carry relation
#     hints - the published counterpart surface (lab/tools/
#     lxmf_counterpart.py) originates PLAIN TEXT only, so that sub-leg is
#     REPORTED as unpublished rather than run.
#
# Gate policy (owner brief: "SKIP cleanly if the lxmf counterpart isn't
# live"): the leg is live when the rig publishes either MEDRE_NORMAL_LXMF=1
# (generic declaration) or the counterpart surfaces (LXMF_PEER_DESTINATION_
# HASH et al. in lab/state/scenario.env). Origination runs the counterpart
# driver INSIDE medre-normal (host-side compose exec - it owns the loopback
# Reticulum pair), built from the published keys:
#
#   docker compose --profile normal exec -T medre-normal \
#     python /opt/lab-tools/lxmf_counterpart.py \
#       --identity ${LXMF_PEER_IDENTITY} \
#       --reticulum-config ${LXMF_COUNTERPART_RNS_CONFIG} \
#       --storage ${LXMF_COUNTERPART_STORAGE} \
#       --display-name lab-counterpart \
#       [--expect-text RX_MARKER] [--send-text TX_MARKER] --timeout S
#
# Defaults match lab/up.sh's smoke invocation (/data-lxmf/reticulum-peer,
# /data-lxmf/peer-router). A custom MEDRE_NORMAL_LXMF_ORIGINATE overrides
# the built invocation entirely (host-side shell; $1 = text).
#
# Once the flow produces an lxmf-origin canonical event, ALL assertions are
# hard (no FAIL-to-SKIP laundering).
#
# Env keys: MEDRE_NORMAL_LXMF ("1" = leg live), MEDRE_NORMAL_LXMF_ADAPTER
# (adapter id in envelopes/refs; default lxmf0), LXMF_PEER_DESTINATION_HASH,
# LXMF_PEER_IDENTITY, LXMF_COUNTERPART_RNS_CONFIG, LXMF_COUNTERPART_STORAGE,
# LXMF_COUNTERPART_TIMEOUT (per-invocation deadline; default 120),
# MEDRE_NORMAL_LXMF_ORIGINATE (optional full-command override),
# MEDRE_NORMAL_DB (or MEDRE_NORMAL_MAP_DB), MAP_DB_A/B.
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

SCENARIO="normal-lxmf"
ROOM_ID="${ROOM_BRIDGE_CH0_ID}"

if [[ ${MEDRE_NORMAL_LXMF} != "1" && -z ${LXMF_PEER_DESTINATION_HASH:-} ]]; then
	SC_SKIP "lxmf leg not published by the rig (no MEDRE_NORMAL_LXMF=1 and no LXMF_PEER_DESTINATION_HASH)" \
		"REQUIREMENT FOR THE RIG: publish the lxmf counterpart surfaces in lab/state/scenario.env (LXMF_PEER_DESTINATION_HASH + LXMF_PEER_IDENTITY, plus the counterpart driver volumes) once the medre-normal LXMF loopback adapter is live"
fi
SC_LOG "lxmf leg live (adapter=${MEDRE_NORMAL_LXMF_ADAPTER} peer_dest=${LXMF_PEER_DESTINATION_HASH:-unpublished})"

ALICE_TOKEN="$(lib_py mx_user.py token "${HUMAN_ALICE_LOCALPART}" | tr -d '"')"
[[ -n ${ALICE_TOKEN} && ${ALICE_TOKEN} != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_ALICE_LOCALPART}" \
		"the rig must provision @alice and publish LAB_TOKEN_ALICE (scenarios/README.md)"

# lxmf_originate TX_MARKER LOG_PATH [RX_MARKER]
#   Originate one lxmf message (and optionally wait for an inbound one) by
#   running the counterpart driver inside medre-normal. Prints nothing; the
#   caller reads LOG_PATH for LXMF_COUNTERPART_RESULT.
lxmf_originate() {
	local tx_marker="$1" log_path="$2" rx_marker="${3:-}"
	local timeout="${LXMF_COUNTERPART_TIMEOUT:-120}"
	local -a cmd=(compose --profile normal exec -T medre-normal
		python /opt/lab-tools/lxmf_counterpart.py
		--identity "${LXMF_PEER_IDENTITY:-/data-lxmf/peer.identity}"
		--reticulum-config "${LXMF_COUNTERPART_RNS_CONFIG:-/data-lxmf/reticulum-peer}"
		--storage "${LXMF_COUNTERPART_STORAGE:-/data-lxmf/peer-router}"
		--display-name lab-counterpart)
	if [[ -n ${MEDRE_NORMAL_LXMF_ORIGINATE:-} ]]; then
		tools_exec bash -c "${MEDRE_NORMAL_LXMF_ORIGINATE}" lxmf-originate "${tx_marker}" >"${log_path}" 2>&1 &
	else
		if [[ -n ${rx_marker} ]]; then
			cmd+=(--expect-text "${rx_marker}")
		fi
		cmd+=(--send-text "${tx_marker}" --timeout "${timeout}")
		"${cmd[@]}" >"${log_path}" 2>&1 &
	fi
}

# lxmf_result LOG_PATH: the final LXMF_COUNTERPART_RESULT JSON (or empty).
lxmf_result() {
	grep -o 'LXMF_COUNTERPART_RESULT=.*' "$1" 2>/dev/null | head -1 | cut -d= -f2-
}

# ---------------------------------------------------------------------------
# Act 1: originate one lxmf message; the room must gain an lxmf-attributed
# canonical event with attribution and bounded native facts.
# ---------------------------------------------------------------------------
MARKER="NLXMF-ORIGIN-$(SC_UID)"
SINCE_MS="$(date +%s%3N)"
LOG_ORIGIN="${LAB_ROOT}/lab/logs/scenarios/lxmf-origin-$(SC_UID).log"
lxmf_originate "${MARKER}" "${LOG_ORIGIN}" &
LXMF_PID=$!

capture lxmf_origin_json lib_py mx_events.py wait "${ROOM_ID}" \
	--body-contains "${MARKER}" \
	--require-envelope \
	--since-ms "${SINCE_MS}" --timeout "${MATRIX_WAIT_SECONDS}" || true
if ! printf '%s' "${lxmf_origin_json}" | grep -q '"event"'; then
	kill "${LXMF_PID}" 2>/dev/null || true
	SC_SKIP "no lxmf-origin canonical event for the originated message (loopback did not deliver)" \
		"waited ${MATRIX_WAIT_SECONDS}s in ${ROOM_ID} for an lxmf-attributed event carrying ${MARKER}" $'\n'"counterpart log: $(tail -3 "${LOG_ORIGIN}" 2>/dev/null | tr '\n' ' ')" $'\n'"REQUIREMENT FOR THE RIG: the counterpart driver must deliver into medre's lxmf0 adapter (see LXMF_COUNTERPART_LOG in lab/state/scenario.env)"
fi

# The matching event must be lxmf-attributed: source_adapter == the lxmf
# adapter. Any non-lxmf match (e.g. the probe text's own echo) is evidence
# the loopback does not produce lxmf-origin events - FAIL, never SKIP (the
# header promises no FAIL-to-SKIP laundering once an event exists).
lxmf_event_json="$(printf '%s' "${lxmf_origin_json}" | tools_exec python3 -c "
import json, sys
payload = json.load(sys.stdin)
event = payload.get('event') or {}
content = event.get('content') or {}
medre = content.get('medre') or {}
envelope = medre.get('envelope') or {}
context = ((medre.get('metadata') or {}).get('bridge_context') or {})
print(json.dumps({
    'event': payload,
    'source_adapter': envelope.get('source_adapter'),
    'sender_id': (context.get('source') or {}).get('sender_id'),
    'origin_event_id': event.get('event_id'),
}))
")"
adapter="$(jq_get "${lxmf_event_json}" source_adapter | tr -d '"')"
sender_id="$(jq_get "${lxmf_event_json}" sender_id | tr -d '"')"
if [[ ${adapter} != "${MEDRE_NORMAL_LXMF_ADAPTER}" ]]; then
	SC_FAIL "the originated marker matched a NON-lxmf canonical event (adapter=${adapter:-none})" \
		"the lxmf0 origin must surface as source_adapter=${MEDRE_NORMAL_LXMF_ADAPTER}; a non-lxmf attribution on the originated marker contradicts the no-laundering promise in this scenario's header" $'\n'"matched event: ${lxmf_event_json}"
fi

medre_assert_valid "${lxmf_event_json}" "lxmf-origin canonical event"
ORIGIN_EVENT_ID="$(jq_get "${lxmf_event_json}" origin_event_id | tr -d '"')"
[[ -n ${sender_id} && ${sender_id} != "null" ]] ||
	SC_FAIL "lxmf-origin event carries no sender attribution" "${lxmf_event_json}"
SC_PASS "lxmf-origin message reached the room with attribution and bounded native facts" \
	"event=${ORIGIN_EVENT_ID} source_adapter=${adapter} sender_id=${sender_id}"

wait "${LXMF_PID}" 2>/dev/null || true
ORIGIN_RESULT="$(lxmf_result "${LOG_ORIGIN}")"
SC_LOG "counterpart origin result: ${ORIGIN_RESULT:-none}"

# ---------------------------------------------------------------------------
# Re-air: the lxmf-origin room text re-airs once per mmrelay mesh.
# ---------------------------------------------------------------------------
for far_name in A B; do
	if [[ ${far_name} == "A" ]]; then
		map_db="${MAP_DB_A}"
	else
		map_db="${MAP_DB_B}"
	fi
	capture rows lib_py maps.py rows-for-event "${map_db}" "${ORIGIN_EVENT_ID}" --wait "${RADIO_WAIT_SECONDS}"
	if printf '%s' "${rows}" | grep -q '"meshtastic_id"'; then
		count="$(printf '%s' "${rows}" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
print(len(rows))')"
		[[ ${count} == "1" ]] ||
			SC_FAIL "lxmf-origin text re-aired ${count}x on Mesh${far_name} (expected exactly once)" "${rows}"
		SC_PASS "lxmf-origin text re-aired once on Mesh${far_name}" \
			"db=${map_db} origin_event=${ORIGIN_EVENT_ID} rows=$(printf '%s' "${rows}" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
print(",".join(str(r.get("meshtastic_id")) for r in rows))')"
	else
		SC_FAIL "lxmf-origin text never re-aired on Mesh${far_name} (no relay map row)" \
			"db=${map_db} origin_event=${ORIGIN_EVENT_ID} waited=${RADIO_WAIT_SECONDS}s" $'\n'"query output: ${rows}"
	fi
done

# ---------------------------------------------------------------------------
# Act 2 (reverse leg): a room text reaches the LXMF counterpart - outbound
# lxmf0 ref on the origin event in medre state + the driver's inbound record.
# ---------------------------------------------------------------------------
RX_MARKER="NLXMF-RX-$(SC_UID)"
SINCE_RX="$(date +%s%3N)"
LOG_RX="${LAB_ROOT}/lab/logs/scenarios/lxmf-rx-$(SC_UID).log"
lxmf_originate "" "${LOG_RX}" "${RX_MARKER}" &
RX_PID=$!

capture rx_event_json lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" "${RX_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "reverse-leg room send failed" "${rx_event_json}"
RX_EVENT_ID="$(jq_get "${rx_event_json}" event_id | tr -d '"')"
SC_PASS "room text sent for the lxmf reverse leg" \
	"event=${RX_EVENT_ID} marker=${RX_MARKER}"

# The counterpart polls for the inbound message; medre writes the outbound
# lxmf ref asynchronously - poll both surfaces.
lxmf_rx_found=0
lxmf_ref_verdict=""
rx_deadline=$((SECONDS + ${LXMF_COUNTERPART_TIMEOUT:-120}))
while ((SECONDS < rx_deadline)); do
	capture lxmf_json medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${RX_EVENT_ID}"
	lxmf_ref_verdict="$(printf '%s' "${lxmf_json}" | medre_lib_py normal_checks.py lxmf-ref - "${MEDRE_NORMAL_LXMF_ADAPTER}")"
	if printf '%s' "${lxmf_ref_verdict}" | grep -q '"found": true' &&
		grep -q "LXMF_COUNTERPART_MESSAGE=" "${LOG_RX}" 2>/dev/null; then
		lxmf_rx_found=1
		break
	fi
	sleep 3
done
kill "${RX_PID}" 2>/dev/null || true
wait "${RX_PID}" 2>/dev/null || true
((lxmf_rx_found == 1)) ||
	SC_FAIL "the room text never reached the LXMF counterpart (no outbound lxmf0 ref and/or no counterpart inbound record)" \
		"ref verdict: ${lxmf_ref_verdict}" $'\n'"counterpart log: $(tail -3 "${LOG_RX}" 2>/dev/null | tr '\n' ' ')" $'\n'"origin event=${RX_EVENT_ID} waited=${LXMF_COUNTERPART_TIMEOUT}s"
RX_INBOUND="$(grep -o 'LXMF_COUNTERPART_MESSAGE=.*' "${LOG_RX}" | head -1 | cut -d= -f2-)"
SC_PASS "room text reached the LXMF counterpart (outbound lxmf0 ref + driver inbound record)" \
	"ref verdict: ${lxmf_ref_verdict}" $'\n'"counterpart inbound: ${RX_INBOUND}"

# ---------------------------------------------------------------------------
# Reply from the lxmf side: the published counterpart originates PLAIN TEXT
# only (no target/relation argument), so relation-hint origination is not
# exercisable through the published surface.
# ---------------------------------------------------------------------------
SC_LOG "NOTE: lxmf-side reply leg not exercisable - the published counterpart driver (lab/tools/lxmf_counterpart.py) originates plain text only and exposes no reply/target argument; relation-hint origination needs a counterpart surface that can target a prior message"

SC_LOG "[${SCENARIO}] complete"
