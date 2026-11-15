#!/usr/bin/env bash
# =============================================================================
# Scenario: normal-fanout
#
# THE LOOP-FREEDOM PROOF on the normal topology. One HUMAN text in the shared
# bridge room must air EXACTLY ONCE per mesh - and never again:
#
#   Alice (human, no envelope) sends text to #lab-bridge:
#     * mmrelay-a airs it on MeshA (its own bridged packet P_a),
#     * mmrelay-b airs it on MeshB (its own bridged packet P_b),
#     * medre-normal airs it on MeshC (its own bridged packet P_c).
#   Three collecting sniffers (a-peer, b-peer, c-peer - one client per node)
#   armed BEFORE the send must each capture EXACTLY ONE packet carrying the
#   marker, with NO nested prefix (the r15 loop signature: a body carrying a
#   second relay prefix, e.g. "[!b39fb5c5/Beta]: relay-a: [!02b885ac/AlphaLab]:
#   ..."), and every capture within the radio UTF-8 byte limit.
#
#   The collectors' windows span a settle period. Across it:
#     * the marker packet count per mesh stays exactly 1 (no re-bridged
#       echo of any bridge's canonical copy of the airs),
#     * no bot-authored canonical event carrying the marker appears in the
#       room WITHOUT being corpus-valid and nested-prefix-free (self-echo
#       re-emissions are a meshtasticd behavior; any that appear must stay
#       well-formed and must not trigger further radio traffic),
#     * the mmrelay send queues stay flat (no cascade backlog), and
#     * every watched state table (relay A map, relay B map, medre-normal
#       canonical events + native refs) is flat (no re-bridge cascade).
#
#   LXMF leg (conditional, contract-room scope): when the rig publishes
#   MEDRE_NORMAL_LXMF=1, the ch0 (contract-room) variant's human text must
#   reach the LXMF loopback (an outbound lxmf-adapter ref on the origin
#   event in medre-normal state); otherwise the leg is skipped with the
#   reason recorded. The rig's lxmf-bridge route scopes the matrix->lxmf
#   fan-out to #lab-bridge ONLY (lab/config/medre-normal.yaml
#   routes.lxmf-bridge.source_room), so other variants REPORT the ref
#   presence without asserting it: its absence there is the configured
#   contract, not a miss (2026-11-11 live run: ch0 ref landed, ch2
#   correctly produced none - the pre-C16 scenario text over-scoped the
#   assertion before the leg was ever published).
#
# Variants: primary bridge channel 0 and secondary channel 2.
#
# Env keys (defaults in scenarios/config.env; rig overrides via
# lab/state/scenario.env): ROOM_BRIDGE_CH0_ALIAS, ROOM_BRIDGE_CH2_ALIAS,
# MEDRE_NORMAL_MAP_DB/MEDRE_NORMAL_DB, NODE_C_PEER_HOST/PORT, MESHNET_C,
# MEDRE_NORMAL_LXMF, MEDRE_NORMAL_LXMF_ADAPTER, MEDRE_QUEUE_LOG_A/B,
# MEDRE_TRUNCATE_BYTES (radio UTF-8 byte limit, 227).
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

SCENARIO="normal-fanout"
ALICE_TOKEN="$(lib_py mx_user.py token "${HUMAN_ALICE_LOCALPART}" | tr -d '"')"
[[ -n "${ALICE_TOKEN}" && "${ALICE_TOKEN}" != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_ALICE_LOCALPART}" \
		"the rig must provision @alice and publish LAB_TOKEN_ALICE (scenarios/README.md)"

COLLECT_WINDOW=$((MATRIX_WAIT_SECONDS + SETTLE_SECONDS * 2 + 30))

# run_variant VARIANT ROOM_ID CHANNEL (-1 any) || true-safe channel arg
run_variant() {
	local variant="$1" room_id="$2" expected_channel="$3"
	local marker since_ms out
	local verdict_a verdict_b verdict_c scan_json lxmf_json
	local before_a before_b before_m after_a after_b after_m

	marker="NFAN-${variant}-$(SC_UID)"
	SC_LOG "[${variant}] bridge-room text: ${marker} (collect window ${COLLECT_WINDOW}s)"

	# --- Three collectors armed BEFORE the send (one client per node) --------
	start_collector_checked COLLECT_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
		--timeout "${COLLECT_WINDOW}" --contains "${marker}"
	start_collector_checked COLLECT_B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
		--timeout "${COLLECT_WINDOW}" --contains "${marker}"
	start_collector_checked COLLECT_C "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
		--timeout "${COLLECT_WINDOW}" --contains "${marker}"
	trap 'stop_collector "${COLLECT_A_PID:-}" "${COLLECT_A:-}"; stop_collector "${COLLECT_B_PID:-}" "${COLLECT_B:-}"; stop_collector "${COLLECT_C_PID:-}" "${COLLECT_C:-}"' EXIT

	# since-ms bound BEFORE the act (2026-11-10 triage: emissions can predate
	# a bound captured afterwards).
	since_ms="$(date +%s%3N)"

	capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${room_id}" "${marker}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] human seed send failed" "${out}"
	ORIGIN_EVENT_ID="$(jq_get "${out}" event_id | tr -d '"')"
	SC_PASS "[${variant}] human text sent into the bridge room" \
		"event=${ORIGIN_EVENT_ID} marker=${marker}"

	# --- Pre-settle snapshot (queue counters + state tables) -----------------
	queue_snapshot BEFORE_A "${MEDRE_QUEUE_LOG_A}" "${MEDRE_QUEUE_CONTAINER_A}"
	queue_snapshot BEFORE_B "${MEDRE_QUEUE_LOG_B}" "${MEDRE_QUEUE_CONTAINER_B}"
	table_snapshot BEFORE_TABLES

	# --- Settle, then stop the collectors (their windows span the settle) ----
	sleep "${SETTLE_SECONDS}"
	sleep "${SETTLE_SECONDS}"

	queue_snapshot AFTER_A "${MEDRE_QUEUE_LOG_A}" "${MEDRE_QUEUE_CONTAINER_A}"
	queue_snapshot AFTER_B "${MEDRE_QUEUE_LOG_B}" "${MEDRE_QUEUE_CONTAINER_B}"
	table_snapshot AFTER_TABLES
	queue_assert_flat "[${variant}] queue" "${BEFORE_A}" "${BEFORE_B}" "${AFTER_A}" "${AFTER_B}" 3 5
	table_assert_flat "[${variant}] tables" "${BEFORE_TABLES}" "${AFTER_TABLES}"

	wait_until "$((COLLECT_WINDOW + 20))" grep -q '"packets"' "${COLLECT_A}" ||
		SC_FAIL "[${variant}] a-peer collector window ended without a result" "collector log: $(cat "${COLLECT_A}")"
	wait_until "$((COLLECT_WINDOW + 20))" grep -q '"packets"' "${COLLECT_B}" ||
		SC_FAIL "[${variant}] b-peer collector window ended without a result" "collector log: $(cat "${COLLECT_B}")"
	wait_until "$((COLLECT_WINDOW + 20))" grep -q '"packets"' "${COLLECT_C}" ||
		SC_FAIL "[${variant}] c-peer collector window ended without a result" "collector log: $(cat "${COLLECT_C}")"

	verdict_a="$(collector_result "${COLLECT_A}")"
	verdict_b="$(collector_result "${COLLECT_B}")"
	verdict_c="$(collector_result "${COLLECT_C}")"

	check_mesh() { # NAME COLLECTOR_RESULT
		local name="$1" result="$2" analysis
		analysis="$(printf '%s' "${result}" | medre_lib_py normal_checks.py fanout-packets - "${marker}" "${expected_channel}" "${MEDRE_TRUNCATE_BYTES}")"
		printf '%s' "${analysis}" | grep -q '"ok": true' ||
			SC_FAIL "[${variant}] ${name} fanout violates the exactly-once/loop-free contract" \
				"verdict: ${analysis}" $'\n'"collector: ${result}"
		SC_PASS "[${variant}] ${name}: exactly one air, nested-prefix-free, within the byte limit" \
			"verdict: ${analysis}"
	}
	check_mesh "MeshA (a-peer)" "${verdict_a}"
	check_mesh "MeshB (b-peer)" "${verdict_b}"
	check_mesh "MeshC (c-peer)" "${verdict_c}"

	# --- Room-side loop signature scan ----------------------------------------
	capture scan_json lib_py mx_events.py scan "${room_id}" \
		--body-contains "${marker}" --since-ms "${since_ms}" --limit 500
	scan_verdict="$(printf '%s' "${scan_json}" | medre_lib_py normal_checks.py room-scan - "${marker}")"
	printf '%s' "${scan_verdict}" | grep -q '"ok": true' ||
		SC_FAIL "[${variant}] room scan found the loop signature (bot echo, doubled marker, or human_events != 1)" \
			"verdict: ${scan_verdict}" $'\n'"scan: ${scan_json}"
	SC_PASS "[${variant}] room scan clean: exactly one human origin (human_events==1 asserted), bot echoes (if any) nested-free" \
		"verdict: ${scan_verdict}"

	# --- LXMF leg (conditional; hard only for the route-contract room) ---------
	# medre writes outbound refs asynchronously: poll until the lxmf ref
	# appears (or the wait budget is spent), never judge a single read. The
	# hard assertion applies to the ch0 contract room (routes.lxmf-bridge
	# .source_room); non-contract rooms REPORT (absence is the configured
	# contract there, not a miss).
	if [[ "${MEDRE_NORMAL_LXMF}" == "1" && "${room_id}" == "${ROOM_BRIDGE_CH0_ID}" ]]; then
		lxmf_found=0
		lxmf_verdict=""
		local lxmf_deadline=$((SECONDS + RADIO_WAIT_SECONDS))
		while ((SECONDS < lxmf_deadline)); do
			capture lxmf_json medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${ORIGIN_EVENT_ID}"
			lxmf_verdict="$(printf '%s' "${lxmf_json}" | medre_lib_py normal_checks.py lxmf-ref - "${MEDRE_NORMAL_LXMF_ADAPTER}")"
			if printf '%s' "${lxmf_verdict}" | grep -q '"found": true'; then
				lxmf_found=1
				break
			fi
			sleep 2
		done
		((lxmf_found == 1)) ||
			SC_FAIL "[${variant}] LXMF leg live but the human text never reached the loopback" \
				"verdict: ${lxmf_verdict}" $'\n'"ref adapters seen: $(jq_get "${lxmf_verdict}" ref_adapters)" $'\n'"origin event=${ORIGIN_EVENT_ID} waited=${RADIO_WAIT_SECONDS}s"
		SC_PASS "[${variant}] human text reached the LXMF loopback (outbound ${MEDRE_NORMAL_LXMF_ADAPTER} ref)" \
			"verdict: ${lxmf_verdict}"
	elif [[ "${MEDRE_NORMAL_LXMF}" == "1" ]]; then
		# Non-contract room: report, never fail (the lxmf-bridge route does
		# not wire this room to the lxmf leg).
		capture lxmf_json medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${ORIGIN_EVENT_ID}"
		lxmf_verdict="$(printf '%s' "${lxmf_json}" | medre_lib_py normal_checks.py lxmf-ref - "${MEDRE_NORMAL_LXMF_ADAPTER}")"
		if printf '%s' "${lxmf_verdict}" | grep -q '"found": true'; then
			SC_LOG "[${variant}] NOTE: lxmf0 ref present outside the route-contract room (superset of the configured scope): ${lxmf_verdict}"
		else
			SC_LOG "[${variant}] NOTE: no lxmf0 ref for this variant - expected per contract: the rig's lxmf-bridge route scopes fan-out to the ch0 room (lab/config/medre-normal.yaml routes.lxmf-bridge.source_room)"
		fi
	else
		SC_LOG "[${variant}] NOTE: LXMF leg skipped - rig has not published MEDRE_NORMAL_LXMF=1"
	fi

	stop_collector "${COLLECT_A_PID}" "${COLLECT_A}"
	stop_collector "${COLLECT_B_PID}" "${COLLECT_B}"
	stop_collector "${COLLECT_C_PID}" "${COLLECT_C}"
	trap - EXIT
}

run_variant "ch0" "${ROOM_BRIDGE_CH0_ID}" -1

if [[ -n ${ROOM_BRIDGE_CH2_ID} ]]; then
	run_variant "ch2" "${ROOM_BRIDGE_CH2_ID}" 2
else
	SC_SKIP "secondary-channel variant needs the rig's #lab-bridge-ch2 room" \
		"publish ROOM_BRIDGE_CH2_ALIAS in lab/state/scenario.env (scenarios/lib/normal_lib.sh header)"
fi

SC_LOG "[${SCENARIO}] complete"
