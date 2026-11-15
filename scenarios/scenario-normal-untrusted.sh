#!/usr/bin/env bash
# =============================================================================
# Scenario: normal-untrusted
#
# @untrusted forges peer/canonical claims in the SHARED bridge room
# (#lab-bridge): cloning the medre block of a REAL medre-normal event and
# adapting every identity claim (meshnet label UntrustedClaims, sender
# Pretender, node id !f0rged00, packet id 999001) so the forgery is coherent
# and valid-shaped. Trust must be denied by SENDER identity, not by shape,
# on ALL THREE bridges (relay-a, relay-b, medre-normal):
#
#   1. No radio traffic on ANY mesh carries a forged claim (collectors on
#      a-peer, b-peer and c-peer across the whole claim window): no bridge
#      adopts the forged provenance and no peer-trust is granted. The
#      visible authored body MAY relay (documented mmrelay contract for
#      untrusted users; the medre bridge's visible-text policy is observed
#      and reported - only CLAIM adoption is a failure).
#   2. mmrelay message maps (A and B): any row keyed by the forged event
#      carries only the local meshnet label and that relay's own numeric
#      packet id - never UntrustedClaims or 999001 (mirrors
#      untrusted-forged-envelope).
#   3. medre-normal state: no AUTHORITATIVE adoption of the forged claims
#      (no canonical identity column, payload, body, native ref or relation
#      row carries UntrustedClaims/Pretender/!f0rged00/999001; no ref keyed
#      by 999001). Claims confined to the stored UNVERIFIED SOURCE
#      OBSERVATIONS namespace are the documented contract (core 93f43311;
#      see triage/2026-11-11-medre-interop-run.md).
#   4. Unmapped-room negative: @alice sends PLAIN text in the unmapped room
#      (mapped NOWHERE) - no radio traffic carrying it on any of the three
#      meshes, no mmrelay map rows, no medre-normal state row.
#   5. Radio DM privacy negative (medre side): a DM from c-peer addressed to
#      the medre-OWNED node must never surface in the mapped bridge rooms
#      (receipt proven by a paired MeshC positive control).
#
# Requires the @untrusted account (mx_forge.py sends as @untrusted).
#
# Env keys: ROOM_BRIDGE_CH0_ALIAS, ROOM_OTHER_ALIAS, MEDRE_NORMAL_DB (or
# MEDRE_NORMAL_MAP_DB), NODE_{A,B,C}_PEER_HOST/PORT, MAP_DB_A/B,
# MESHNET_A/B (expected local labels in relay rows).
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
if [[ -z "${ROOM_OTHER_ID:-}" ]]; then
	SC_SKIP "unmapped room unresolved (the unmapped-room negative needs it)" \
		"publish ROOM_OTHER_ALIAS in lab/state/scenario.env (scenarios/config.env default: #lab-other:lab.test)"
fi

SCENARIO="normal-untrusted"
UNTRUSTED_TOKEN="$(lib_py lab_env_probe.py untrusted_token | tr -d '"')"
[[ -n "${UNTRUSTED_TOKEN}" && "${UNTRUSTED_TOKEN}" != "null" ]] ||
	SC_SKIP "untrusted account token missing from runtime.json" \
		"the rig must provision @untrusted (scenarios/README.md scenario list)"

ALICE_TOKEN="$(lib_py mx_user.py token "${HUMAN_ALICE_LOCALPART}" | tr -d '"')"
[[ -n "${ALICE_TOKEN}" && "${ALICE_TOKEN}" != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_ALICE_LOCALPART}" \
		"the rig must provision @alice and publish LAB_TOKEN_ALICE (scenarios/README.md)"

CLAIMS=("UntrustedClaims" "Pretender" "!f0rged00" "999001")
ROOM_ID="${ROOM_BRIDGE_CH0_ID}"

# Radio-ingress readiness gate for the medre seed (startup drain drops
# inbound radio packets by design).
radio_ingest_ready "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${MEDRE_NORMAL_BOT_MXID}" "${ROOM_ID}" "medre-normal"

# wait_collector TMPFILE WINDOW: bounded wait for the collector's final
# result line (printed when its capture window closes).
wait_collector() {
	local file="$1" window="$2"
	wait_until "$((window + 20))" grep -q '"packets"' "${file}" ||
		SC_FAIL "collector window ended without a result" "collector log: $(cat "${file}")"
}

# radio_hits TMPFILE NEEDLE... -> JSON verdict {hits, count, poisoned}
# poisoned: the transport reported dropped queued packets mid-window (the
# collector's client was evicted/reconnected; packets may have been lost) -
# an ABSENCE verdict over a poisoned window is never clean evidence.
radio_hits() {
	local file="$1"
	shift
	local result
	result="$(collector_result "${file}")"
	printf '%s' "${result}" | tools_exec python3 -c "
import json, sys
data = json.load(sys.stdin)
needles = sys.argv[1:]
hits = [p for p in data.get('packets', []) for n in needles if n in (p.get('text') or '')]
drops = data.get('transport_drops') or []
print(json.dumps({'hits': hits[:5], 'count': len(hits), 'seen_total': data.get('seen_total'), 'poisoned': bool(drops), 'transport_drops': drops}))
" "$@"
}

# assert_clean_absence VERDICT LABEL EVIDENCE...
#   A zero-count window only proves absence when the window was NOT poisoned
#   (no transport drops): a clean 0 over a poisoned window must FAIL, not
#   pass (2026-11-11 review: the drop signature means packets may have been
#   lost mid-capture, so the zero cannot distinguish silence from loss).
assert_clean_absence() {
	local verdict="$1" label="$2"
	shift 2
	printf '%s' "${verdict}" | grep -q '"poisoned": false' ||
		SC_FAIL "${label}: collector window POISONED (transport dropped queued packets mid-window) - its zero is not evidence" \
			"verdict: ${verdict}" "$@" $'\n'"rerun the scenario on a quiet rig (one client per peer node while scenarios run)"
}

stop_all_collectors() {
	stop_collector "${COLLECT_A_PID:-}" "${COLLECT_A:-}"
	stop_collector "${COLLECT_B_PID:-}" "${COLLECT_B:-}"
	stop_collector "${COLLECT_C_PID:-}" "${COLLECT_C:-}"
}

# =============================================================================
# Act 1: capture a REAL medre-normal event as the forgery template
# =============================================================================
SEED_MARKER="NFORGE-SEED-$(SC_UID)"
SINCE_MS="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" "${SEED_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "seed radio injection failed" "${out}"
capture seed_json lib_py mx_events.py wait "${ROOM_ID}" \
	--sender "${MEDRE_NORMAL_BOT_MXID}" --body-contains "${SEED_MARKER}" \
	--require-envelope --envelope-kind message.created --since-ms "${SINCE_MS}" \
	--timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "no real medre-normal event to clone (seed)" "${seed_json}"
SEED_EVENT_ID="$(jq_get "${seed_json}" event.event_id | tr -d '"')"
medre_assert_valid "${seed_json}" "seed medre-normal event"
SC_PASS "captured a real medre-normal event as the forgery template" \
	"template_event=${SEED_EVENT_ID}"

# =============================================================================
# Act 2: collectors armed, then @untrusted sends the forged event
# =============================================================================
CLAIM_WINDOW=$((SNIFF_WAIT_SECONDS + 90))
start_collector_checked COLLECT_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	--timeout "${CLAIM_WINDOW}"
start_collector_checked COLLECT_B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	--timeout "${CLAIM_WINDOW}"
start_collector_checked COLLECT_C "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	--timeout "${CLAIM_WINDOW}"
trap 'stop_all_collectors' EXIT

FORGE_MARKER="NFORGED-$(SC_UID)"
capture forge_json lib_py mx_forge.py "${ROOM_ID}" "${seed_json}" "${FORGE_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "forged send failed" "${forge_json}"
FORGED_EVENT_ID="$(jq_get "${forge_json}" event_id | tr -d '"')"
SC_PASS "@untrusted sent a medre-shaped forged event into the shared bridge room" \
	"event_id=${FORGED_EVENT_ID} forged_meshnet=$(jq_get "${forge_json}" forged_meshnet | tr -d '"')" \
	"forged_sender=$(jq_get "${forge_json}" forged_sender | tr -d '"')" \
	"cloned_canonical_event_id=$(jq_get "${forge_json}" cloned_canonical_event_id | tr -d '"')"

sleep "${SETTLE_SECONDS}"

# --- Assertion 2+3: claim-absence in relay maps and medre authoritative state
# mmrelay maps have no observation namespace: forged claims must not appear
# AT ALL. medre state MAY store claims as UNVERIFIED SOURCE OBSERVATIONS
# (documented contract) - authoritative adoption is judged below via the
# medre-claim-verdict on the forged event's stored canonical rows, so a
# raw text search here would misjudge legitimate observation storage (the
# SCENARIO-class defect the 2026-11-11 stress triage documented).
for claim in "${CLAIMS[@]}"; do
	for map_db in "${MAP_DB_A}" "${MAP_DB_B}"; do
		capture rows lib_py maps.py rows-for-text "${map_db}" "${claim}"
		count="$(jq_get "${rows}" rows | grep -c '"meshtastic_id"' || true)"
		((count == 0)) ||
			SC_FAIL "forged claim '${claim}' leaked into an mmrelay message map" \
				"db=${map_db} rows=${rows}"
	done
done
SC_PASS "no forged claim in either relay's map" \
	"checked=${CLAIMS[*]} dbs=${MAP_DB_A},${MAP_DB_B}"

capture out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "999001"
count="$(printf '%s' "${out}" | grep -c '"native_message_id"' || true)"
((count == 0)) ||
	SC_FAIL "forged packet id 999001 adopted as a native ref in medre-normal state" \
		"db=${MEDRE_NORMAL_DB} events=${out}"
SC_PASS "forged packet id never adopted as a medre native ref" "db=${MEDRE_NORMAL_DB}"

# mmrelay claim-free handling of the forged event's VISIBLE text (documented
# contract): if a map row exists it carries local meshnet + own packet id.
capture rows_a lib_py maps.py rows-for-event "${MAP_DB_A}" "${FORGED_EVENT_ID}"
if printf '%s' "${rows_a}" | grep -q '"meshtastic_id"'; then
	meshnet_a="$(printf '%s' "${rows_a}" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
print(",".join(sorted({str(r.get("meshtastic_meshnet")) for r in rows})))')"
	ids_a="$(printf '%s' "${rows_a}" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
print(",".join(str(r.get("meshtastic_id")) for r in rows))')"
	[[ "${meshnet_a}" == "${MESHNET_A}" ]] ||
		SC_FAIL "relay-a adopted the forged meshnet label" \
			"expected=${MESHNET_A} found=${meshnet_a} rows=${rows_a}"
	[[ "${ids_a}" =~ ^[0-9]+(,[0-9]+)*$ ]] ||
		SC_FAIL "relay-a map meshtastic_id is not its own radio packet id" \
			"meshtastic_id=${ids_a}"
	SC_PASS "relay-a relayed only the visible text with local attribution" \
		"db=${MAP_DB_A} meshnet=${meshnet_a} packet_ids=${ids_a}"
else
	SC_LOG "NOTE: relay-a did not map the forged event's visible text (observed behavior; untrusted-forged-envelope documents the baseline)"
fi

# medre state: whatever the medre bridge did with the forged event's visible
# text, its stored canonical event must adopt nothing AUTHORITATIVE (core
# 93f43311: inbound wire claims live as UNVERIFIED SOURCE OBSERVATIONS
# under metadata.native.data.matrix.relay.*; authoritative surfaces are the
# canonical identity columns, payload/body, native ref identity, and
# relation rows - none may carry a forged claim, and the real sender must
# be preserved).
capture out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${FORGED_EVENT_ID}"
if printf '%s' "${out}" | grep -q '"event_id"'; then
	verdict="$(printf '%s' "${out}" | medre_lib_py normal_checks.py medre-claim-verdict - "${CLAIMS[@]}")"
	printf '%s' "${verdict}" | grep -q '"ok": true' ||
		SC_FAIL "medre-normal adopted forged claims into authoritative canonical state" \
			"verdict: ${verdict}" $'\n'"state output: ${out}"
	SC_PASS "medre stored only unverified observations of the forged claims; authoritative state adopts nothing" \
		"db=${MEDRE_NORMAL_DB} verdict: ${verdict}"
else
	SC_LOG "NOTE: medre state holds no canonical event for the forged event id (observed behavior: untrusted visible text not ingested, or ingested under medre's own canonical id)"
fi

# --- Assertion 1: claim-absence on radio across the claim window --------------
wait_collector "${COLLECT_A}" "${CLAIM_WINDOW}"
wait_collector "${COLLECT_B}" "${CLAIM_WINDOW}"
wait_collector "${COLLECT_C}" "${CLAIM_WINDOW}"
for mesh in A B C; do
	eval "collect_file=\${COLLECT_${mesh}}"
	verdict="$(radio_hits "${collect_file}" "${CLAIMS[@]}")"
	printf '%s' "${verdict}" | grep -q '"count": 0' ||
		SC_FAIL "forged claims appeared on Mesh${mesh} radio" \
			"verdict: ${verdict}" $'\n'"collector: $(collector_result "${collect_file}")"
	assert_clean_absence "${verdict}" "Mesh${mesh} claim window" \
		$'\n'"collector: $(collector_result "${collect_file}")"
	SC_PASS "Mesh${mesh} radio claim-free across the claim window" "verdict: ${verdict}"
done
stop_all_collectors
trap - EXIT

# =============================================================================
# Act 3: unmapped-room negative (fresh collector generation)
# =============================================================================
OTHER_MARKER="NOTHER-$(SC_UID)"
OTHER_WINDOW=$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))
start_collector_checked COLLECT_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	--timeout "${OTHER_WINDOW}" --contains "${OTHER_MARKER}"
start_collector_checked COLLECT_B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	--timeout "${OTHER_WINDOW}" --contains "${OTHER_MARKER}"
start_collector_checked COLLECT_C "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	--timeout "${OTHER_WINDOW}" --contains "${OTHER_MARKER}"
trap 'stop_all_collectors' EXIT

capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_OTHER_ID}" "${OTHER_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "unmapped-room send failed (is @alice in the unmapped room?)" "${out}"
OTHER_EVENT_ID="$(jq_get "${out}" event_id | tr -d '"')"
SC_PASS "plain text sent into the unmapped room" \
	"event=${OTHER_EVENT_ID} marker=${OTHER_MARKER} room=${ROOM_OTHER_ID}"

wait_collector "${COLLECT_A}" "${OTHER_WINDOW}"
wait_collector "${COLLECT_B}" "${OTHER_WINDOW}"
wait_collector "${COLLECT_C}" "${OTHER_WINDOW}"
for mesh in A B C; do
	eval "collect_file=\${COLLECT_${mesh}}"
	verdict="$(radio_hits "${collect_file}" "${OTHER_MARKER}")"
	printf '%s' "${verdict}" | grep -q '"count": 0' ||
		SC_FAIL "unmapped-room text leaked to Mesh${mesh} radio" \
			"verdict: ${verdict}" $'\n'"collector: $(collector_result "${collect_file}")"
	assert_clean_absence "${verdict}" "Mesh${mesh} unmapped-room window" \
		$'\n'"collector: $(collector_result "${collect_file}")"
	SC_PASS "unmapped-room negative: Mesh${mesh} radio silent" "verdict: ${verdict}"
done

capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${OTHER_EVENT_ID}"
count="$(jq_get "${rows}" rows | grep -c '"meshtastic_id"' || true)"
((count == 0)) || SC_FAIL "unmapped-room negative: relay-a wrote a map row for the unmapped-room text" "${rows}"
capture rows lib_py maps.py rows-for-event "${MAP_DB_B}" "${OTHER_EVENT_ID}"
count="$(jq_get "${rows}" rows | grep -c '"meshtastic_id"' || true)"
((count == 0)) || SC_FAIL "unmapped-room negative: relay-b wrote a map row for the unmapped-room text" "${rows}"
capture out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${OTHER_EVENT_ID}"
count="$(printf '%s' "${out}" | grep -c '"event_id"' || true)"
((count == 0)) || SC_FAIL "unmapped-room negative: medre state holds a canonical event for the unmapped-room text" "${out}"
SC_PASS "unmapped-room negative: no map rows (both relays), no medre-normal state row" \
	"event=${OTHER_EVENT_ID} dbs=${MAP_DB_A},${MAP_DB_B},${MEDRE_NORMAL_DB}"
stop_all_collectors
trap - EXIT

# =============================================================================
# Act 4: radio DM privacy negative (release priority: DM privacy, medre side)
#
# A radio DIRECT MESSAGE from the tooling peer addressed to the medre-OWNED
# MeshC node reaches medre-normal's radio adapter, whose packet classifier
# IGNORES direct messages ("direct message to specific node",
# medre-adapter-meshtastic packet_classifier rule 7). The DM must NEVER
# surface in the mapped Matrix rooms. Receipt of the DM is proven
# NON-vacuously by a paired positive control on the SAME sender surface,
# channel, and window: a Matrix-origin human text that medre MUST transmit
# on MeshC (the c-peer sniffer captures it and yields the medre node id used
# as the DM destination). mmrelay's DM side is unit-covered
# (test_meshtastic_utils_messages_routing.py: direct_message_skips_relay /
# direct_message_interactions_never_reach_matrix); this act is the medre-side
# lab assertion.
# =============================================================================

# dm_sniff_match VAR TMPFILE: extract the {"match":...} line or empty.
dm_sniff_match() {
	local __var="$1" tmp="$2" line=""
	if grep -q '"match"' "${tmp}"; then
		line="$(grep -o '{"match":.*}' "${tmp}" | head -1)"
	fi
	printf -v "${__var}" '%s' "${line}"
}

MTX_CTRL="NDMCTRL-$(SC_UID)"
start_sniffer_checked SNIFF_DM "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	--timeout "${SNIFF_WAIT_SECONDS}" --expect-text "${MTX_CTRL}"
trap 'stop_sniffer "${SNIFF_DM_PID:-}" "${SNIFF_DM:-}"' EXIT

capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" "${MTX_CTRL}"
((SC_STATUS == 0)) || SC_FAIL "DM-act positive-control Matrix send failed" "${out}"

if ! wait_until "${SNIFF_WAIT_SECONDS}" grep -q '"match"' "${SNIFF_DM}"; then
	DM_SNIFF_EVIDENCE="$(cat "${SNIFF_DM}" 2>/dev/null || true)"
	stop_sniffer "${SNIFF_DM_PID:-}" "${SNIFF_DM:-}"
	trap - EXIT
	SC_SKIP "DM-act instrument unresolvable: medre-normal transmitted no MeshC radio text in the control window" \
		"control marker=${MTX_CTRL} sniffer log: ${DM_SNIFF_EVIDENCE}" $'\n'"REQUIREMENT: the matrix->MeshC radio leg must be alive to (a) capture the medre-owned node id and (b) prove the DM was actually receivable by medre in the same window; normal-fanout owns the matrix->radio assertion - rerun this act on a rig where that leg passes"
fi
dm_sniff_match DM_MATCH "${SNIFF_DM}"
MEDRE_NODE_ID="$(printf '%s' "${DM_MATCH}" | tools_exec python3 -c '
import json, sys
match = json.load(sys.stdin)["match"]
print(match.get("from_id") or "")')"
[[ "${MEDRE_NODE_ID}" =~ ^![0-9a-f]{8}$ ]] ||
	SC_FAIL "DM-act control capture carries no usable medre node id" "match: ${DM_MATCH}"
stop_sniffer "${SNIFF_DM_PID}" "${SNIFF_DM}"
trap - EXIT
SC_PASS "DM-act positive control: medre TX'd on MeshC; medre-owned node id captured" \
	"control_marker=${MTX_CTRL} medre_node=${MEDRE_NODE_ID}"

# The DM itself (c-peer client slot is free again after the sniffer stopped).
DM_MARKER="NDMPRV-$(SC_UID)"
SINCE_DM="$(date +%s%3N)"
capture dm_json lib_py radio.py send-text "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${DM_MARKER}" --dest "${MEDRE_NODE_ID}"
((SC_STATUS == 0)) || SC_FAIL "DM injection failed" "${dm_json}"
DM_PACKET_ID="$(jq_get "${dm_json}" packet_id | tr -d '"')"
SC_PASS "radio DM injected, addressed to the medre-owned node" \
	"dm_marker=${DM_MARKER} dest=${MEDRE_NODE_ID} packet_id=${DM_PACKET_ID}"

# Privacy surface: NO event in the mapped bridge room carries the DM body
# (any sender). A bounded wait that TIMES OUT is the pass verdict; a match is
# a PRODUCT privacy failure.
capture dm_room lib_py mx_events.py wait "${ROOM_ID}" \
	--body-contains "${DM_MARKER}" --since-ms "${SINCE_DM}" \
	--timeout "${MATRIX_WAIT_SECONDS}"
if ((SC_STATUS == 0)); then
	SC_FAIL "radio DM PRIVACY LEAK: the DM body surfaced in the mapped bridge room" \
		"room=${ROOM_ID} event: ${dm_room}" $'\n'"dm_packet_id=${DM_PACKET_ID} dest=${MEDRE_NODE_ID}"
fi
SC_PASS "radio DM never surfaced in the mapped bridge room" \
	"room=${ROOM_ID} marker=${DM_MARKER} absence_window=${MATRIX_WAIT_SECONDS}s"

# Adapter contract observation (report, not a verdict): the classifier's
# direct-message rule IGNORES the packet, so medre state should hold NO
# canonical event for the DM packet id; a stored event would be a divergence
# from the pin's documented classification contract.
capture dm_state medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${DM_PACKET_ID}"
if printf '%s' "${dm_state}" | grep -q '"event_id"'; then
	SC_LOG "NOTE: medre state holds a canonical event for the DM packet id ${DM_PACKET_ID} (divergence from the classifier's direct-message ignore contract; room surface stays clean): ${dm_state}"
else
	SC_LOG "NOTE: medre state holds no canonical event for the DM packet id (classifier direct-message ignore contract holds end to end)"
fi

SC_LOG "[${SCENARIO}] complete"
