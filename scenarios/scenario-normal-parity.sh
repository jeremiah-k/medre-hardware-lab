#!/usr/bin/env bash
# =============================================================================
# Scenario: normal-parity
#
# Per-mesh radio injections on the normal topology: each mesh's owner bridge
# must emit a corpus-valid canonical event agreeing on the observable native
# facts, and every OTHER bridge's consumption of that event must agree with
# the origin envelope's metadata.
#
# In the normal topology each radio packet has exactly ONE direct radio
# observer (the mesh owner: mmrelay-a on MeshA, mmrelay-b on MeshB,
# medre-normal on MeshC). The "two observe it" surface is therefore the
# SHARED canonical Matrix event: the far bridges consume the same event and
# re-derive artifacts from it. For each per-mesh injection the scenario
# asserts:
#
#   * OWNER EVENT (required): corpus-valid under the medre installed schema
#     authority, source_channel == the injected channel (numeric; r14
#     invariant class), source_native_ref.native_message_id == the injected
#     packet id, and bridge_context.source.sender_id == the injecting peer
#     node's native id (attribution). The meshnet label stays confined to
#     native_facts (corpus leak checks), and the body carries the authored
#     text exactly once.
#   * FAR MMRELAY RE-BRIDGE (required for every origin): the far mmrelay's
#     message_map row for the owner event - cross-mesh delivery of
#     radio-origin/medre-origin texts through the shared room is the
#     documented delivery path (stress-suite smoke) and matches the normal
#     contract that room texts re-air once per mesh. The row must carry that
#     relay's OWN numeric packet id (never the origin's) and the authored
#     text once; the recorded meshnet label is REPORTED (relay-local vs
#     origin-label policy is not asserted here).
#   * MEDRE RE-AIR (required when the origin owner is an mmrelay): medre
#     state must gain an outbound radio native ref for the owner event
#     (polled - medre writes outbound refs asynchronously) with a channel
#     agreeing with the envelope.
#   * SAME-PACKET AGREEMENT (conditional): if two canonical events in the
#     room ever claim the SAME native packet id (two bridges observing the
#     same over-air packet), their channel + sender identity must agree.
#
# Variants: primary channel 0 and secondary channel 2 on all three meshes.
#
# Every event is judged by the corpus validator with medre's installed
# schema as the authority (medre_lib.sh). State/API assertions only; radio
# captures belong to the fanout/replies/reactions scenarios.
#
# Env keys: ROOM_BRIDGE_CH0_ALIAS/ROOM_BRIDGE_CH2_ALIAS, MEDRE_NORMAL_DB
# (or MEDRE_NORMAL_MAP_DB), NODE_{A,B,C}_PEER_HOST/PORT, MAP_DB_A/B,
# MEDRE_NORMAL_RADIO_ADAPTER (unasserted when empty), MESHNET_A/B/C.
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

SCENARIO="normal-parity"

# Readiness gate (once per run): mmrelay drops inbound radio packets during
# its startup drain window, so a run started seconds after a relay restart
# would misread every leg as missing. The drain is process-global, so one
# canary on the first-injected mesh covers the whole run (including the ch2
# variant); channel-specific breakage is owned by the variant assertions.
radio_ingest_ready "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${BOT_A_MXID}" "${ROOM_BRIDGE_CH0_ID}" "relay-a"

# Attribution anchors: read the tooling peers' native ids BEFORE any
# injection/sniffer holds their client slots.
peer_node_id PEER_A_ID "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}"
peer_node_id PEER_B_ID "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}"
peer_node_id PEER_C_ID "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}"
SC_PASS "tooling peer identities read" \
	"a-peer=${PEER_A_ID} b-peer=${PEER_B_ID} c-peer=${PEER_C_ID}"

# body_count JSON PATH SUBSTR -> occurrences of SUBSTR inside the field
body_count() {
	printf '%s' "$1" | tools_exec python3 -c "
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
value = current if isinstance(current, str) else ''
print(value.count(sys.argv[2]))
" "$2" "$3"
}

# run_variant VARIANT ROOM_ID CHANNEL_ARG EXPECTED_CHANNEL
run_variant() {
	local variant="$1" room_id="$2" channel_arg="$3" expected_channel="$4"
	local variant_since out dup_verdict

	# One since-ms bound for the whole variant: captured BEFORE the first
	# blocking injection, it predates every later emission of the variant
	# (2026-11-10 triage: emissions can predate a bound captured after).
	variant_since="$(date +%s%3N)"

	run_mesh() { # MESH PEER_HOST PEER_PORT OWNER_BOT PEER_NODE_ID MESHNET_LABEL
		local mesh="$1" host="$2" port="$3" owner_bot="$4" peer_id="$5" mesh_label="$6"
		local marker packet_id owner_json owner_event_id owner_sender marker_count
		marker="NPAR-${variant}-${mesh}-$(SC_UID)"

		local radio_args=(send-text "${host}" "${port}" "${marker}")
		if [[ -n ${channel_arg} ]]; then
			radio_args+=(--channel "${channel_arg}")
		fi
		capture out lib_py radio.py "${radio_args[@]}"
		((SC_STATUS == 0)) || SC_FAIL "[${variant}/${mesh}] radio injection failed" "${out}"
		packet_id="$(jq_get "${out}" packet_id | tr -d '"')"
		[[ ${packet_id} =~ ^[0-9]+$ ]] ||
			SC_FAIL "[${variant}/${mesh}] radio injection did not report a packet id" "${out}"
		SC_PASS "[${variant}/${mesh}] radio text injected on ${mesh_label:-Mesh} channel ${expected_channel}" \
			"packet_id=${packet_id} marker=${marker}"

		capture owner_json lib_py mx_events.py wait "${room_id}" \
			--sender "${owner_bot}" --body-contains "${marker}" \
			--require-envelope --envelope-kind message.created \
			--source-channel "${expected_channel}" --native-ref-is-numeric \
			--native-ref-msg-id "${packet_id}" \
			--since-ms "${variant_since}" --timeout "${MATRIX_WAIT_SECONDS}"
		((SC_STATUS == 0)) ||
			SC_FAIL "[${variant}/${mesh}] no canonical ${owner_bot} event on channel ${expected_channel} for packet ${packet_id}" "${owner_json}"
		owner_event_id="$(jq_get "${owner_json}" event.event_id | tr -d '"')"
		medre_assert_valid "${owner_json}" "[${variant}/${mesh}] ${owner_bot} owner event"

		owner_sender="$(jq_get "${owner_json}" event.content.medre.metadata.bridge_context.source.sender_id | tr -d '"')"
		[[ ${owner_sender} == "${peer_id}" ]] ||
			SC_FAIL "[${variant}/${mesh}] sender attribution disagrees with the injecting peer node" \
				"envelope sender_id=${owner_sender} peer_node=${peer_id}" $'\n'"event=${owner_event_id} packet=${packet_id}"
		marker_count="$(body_count "${owner_json}" event.content.body "${marker}")"
		[[ ${marker_count} == "1" ]] ||
			SC_FAIL "[${variant}/${mesh}] authored text not carried exactly once in the owner body" \
				"occurrences=${marker_count} event_json=${owner_json}"
		SC_PASS "[${variant}/${mesh}] owner event agrees on channel, packet id, and peer attribution" \
			"event=${owner_event_id} sender_id=${owner_sender} channel=${expected_channel} packet=${packet_id}"

		# --- Far mmrelay re-bridge rows (required: cross-mesh delivery) ---------
		local far_name map_db rows row_ids row_mesh far_relay_mxid
		for far_name in A B; do
			if [[ ${far_name} == "A" ]]; then
				far_relay_mxid="${BOT_A_MXID}"
				map_db="${MAP_DB_A}"
			else
				far_relay_mxid="${BOT_B_MXID}"
				map_db="${MAP_DB_B}"
			fi
			if [[ ${far_relay_mxid} == "${owner_bot}" ]]; then
				continue
			fi
			capture rows lib_py maps.py rows-for-event "${map_db}" "${owner_event_id}" --wait "${RADIO_WAIT_SECONDS}"
			if printf '%s' "${rows}" | grep -q '"meshtastic_id"'; then
				row_ids="$(printf '%s' "${rows}" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
print(",".join(str(r.get("meshtastic_id")) for r in rows))')"
				row_mesh="$(printf '%s' "${rows}" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
print(",".join(sorted({str(r.get("meshtastic_meshnet")) for r in rows})))')"
				[[ ${row_ids} =~ ^[0-9]+(,[0-9]+)*$ ]] ||
					SC_FAIL "[${variant}/${mesh}] relay-${far_name} re-bridge row packet id is not numeric" \
						"meshtastic_id=${row_ids} rows=${rows}"
				printf '%s' "${rows}" | grep -q "${marker}" ||
					SC_FAIL "[${variant}/${mesh}] relay-${far_name} re-bridge row lost the authored text" "${rows}"
				SC_PASS "[${variant}/${mesh}] relay-${far_name} re-bridged the ${mesh} origin (own numeric packet id)" \
					"db=${map_db} row_packet_ids=${row_ids} row_meshnet=${row_mesh} (meshnet label reported, not asserted)"
			else
				SC_FAIL "[${variant}/${mesh}] relay-${far_name} never re-bridged the ${mesh} origin event (no map row: no confirmed cross-mesh send)" \
					"db=${map_db} origin_event=${owner_event_id} owner=${owner_bot} waited=${RADIO_WAIT_SECONDS}s" $'\n'"query output: ${rows}"
			fi
		done

		# --- medre consumption ----------------------------------------------------
		if [[ ${owner_bot} == "${MEDRE_NORMAL_BOT_MXID}" ]]; then
			# medre is the owner: its state must hold the inbound ref for its
			# own mesh packet; the far relays' rows above are its delivery.
			capture out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${packet_id}" --wait "${RADIO_WAIT_SECONDS}"
			((SC_STATUS == 0)) ||
				SC_FAIL "[${variant}/${mesh}] medre state lacks the inbound native ref for its own mesh packet ${packet_id}" "${out}"
			SC_PASS "[${variant}/${mesh}] medre state holds the inbound ref for the ${mesh} packet" \
				"packet=${packet_id} db=${MEDRE_NORMAL_DB}"
		else
			# Owner is an mmrelay: medre must RE-AIR the origin onto MeshC
			# (outbound ref polled asynchronously; channel must agree).
			local medre_out ref_adapter ref_channel ref_packet
			capture medre_out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${owner_event_id}" \
				--wait "${RADIO_WAIT_SECONDS}" --require-outbound-radio
			((SC_STATUS == 0)) ||
				SC_FAIL "[${variant}/${mesh}] medre never re-aired the ${mesh} origin event onto MeshC (no outbound radio ref)" \
					"db=${MEDRE_NORMAL_DB} origin_event=${owner_event_id} waited=${RADIO_WAIT_SECONDS}s" $'\n'"state output: ${medre_out}"
			ref_adapter="$(printf '%s' "${medre_out}" | tools_exec python3 -c '
import json, sys, re
data = json.load(sys.stdin)
for event in data.get("events", []):
    for ref in event.get("native_refs", []):
        if ref.get("direction") == "outbound" and re.fullmatch(r"[0-9]+", str(ref.get("native_message_id") or "")):
            print(str(ref.get("adapter")))
            raise SystemExit
')"
			ref_channel="$(printf '%s' "${medre_out}" | tools_exec python3 -c '
import json, sys, re
data = json.load(sys.stdin)
for event in data.get("events", []):
    for ref in event.get("native_refs", []):
        if ref.get("direction") == "outbound" and re.fullmatch(r"[0-9]+", str(ref.get("native_message_id") or "")):
            print(str(ref.get("native_channel_id")))
            raise SystemExit
')"
			ref_packet="$(printf '%s' "${medre_out}" | tools_exec python3 -c '
import json, sys, re
data = json.load(sys.stdin)
for event in data.get("events", []):
    for ref in event.get("native_refs", []):
        if ref.get("direction") == "outbound" and re.fullmatch(r"[0-9]+", str(ref.get("native_message_id") or "")):
            print(str(ref.get("native_message_id")))
            raise SystemExit
')"
			[[ ${ref_channel} == "${expected_channel}" ]] ||
				SC_FAIL "[${variant}/${mesh}] medre outbound ref channel disagrees with the origin envelope" \
					"ref_channel=${ref_channel} expected=${expected_channel} origin_event=${owner_event_id}" $'\n'"state output: ${medre_out}"
			SC_PASS "[${variant}/${mesh}] medre re-aired the ${mesh} origin onto MeshC (outbound ref agrees on channel)" \
				"adapter=${ref_adapter} channel=${ref_channel} medre_packet=${ref_packet} db=${MEDRE_NORMAL_DB}"
		fi
	}

	run_mesh A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${BOT_A_MXID}" "${PEER_A_ID}" "${MESHNET_A}"
	run_mesh B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" "${BOT_B_MXID}" "${PEER_B_ID}" "${MESHNET_B}"
	run_mesh C "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" "${MEDRE_NORMAL_BOT_MXID}" "${PEER_C_ID}" "${MESHNET_C}"

	# --- Conditional same-packet agreement ----------------------------------------
	# Two canonical events claiming the SAME native packet: a channel
	# disagreement is HARD (incoherent metadata), and the header's
	# sender-agreement claim is ENFORCED with the purpose-built parity-agree
	# verdict (channel + packet id + bridge_context.source.sender_id must
	# agree for every same-packet pair - fetched full events, consecutive
	# pairs). Same-Matrix-sender duplicates remain REPORTED (self-echo
	# re-emission is a documented meshtasticd behavior class -
	# scenarios/README.md - judged only for well-formedness).
	capture out lib_py mx_events.py scan "${room_id}" \
		--require-envelope --native-ref-is-numeric --since-ms "${variant_since}" --limit 500
	dup_verdict="$(printf '%s' "${out}" | tools_exec python3 -c '
import json, sys
matches = json.load(sys.stdin).get("matches", [])
groups = {}
for match in matches:
    pid = match.get("source_native_ref_native_message_id")
    if pid is None:
        continue
    groups.setdefault(str(pid), []).append(match)
hard = []
soft = []
same_packet = []
for pid, group in sorted(groups.items()):
    if len(group) < 2:
        continue
    channels = {str(m.get("source_channel")) for m in group}
    senders = {str(m.get("sender")) for m in group}
    same_packet.append({"packet": pid, "events": [m.get("event_id") for m in group], "senders": sorted(senders)})
    if len(channels) > 1:
        hard.append(f"packet {pid}: canonical events disagree on channel {sorted(channels)}")
    if len(senders) == 1:
        soft.append(f"packet {pid}: {len(group)} events from {sorted(senders)} (self-echo re-emission)")
    else:
        soft.append(f"packet {pid}: {len(group)} events from {sorted(senders)} (cross-bridge same-packet)")
print(json.dumps({"same_packet_groups": same_packet, "problems": hard, "notes": soft}))')"
	printf '%s' "${dup_verdict}" | grep -q '"problems": \[\]' ||
		SC_FAIL "[${variant}] same-packet metadata disagreement across canonical events" \
			"verdict: ${dup_verdict}" $'\n'"scan: ${out}"
	# Sender-agreement on every same-packet pair (bounded: fetches happen
	# only when a same-packet group exists - the conditional surface).
	local -a pair_lines=()
	mapfile -t pair_lines < <(printf '%s' "${dup_verdict}" | tools_exec python3 -c '
import json, sys
for group in json.load(sys.stdin).get("same_packet_groups", []):
    events = [e for e in group.get("events", []) if e]
    for index in range(1, len(events)):
        print(events[0], events[index])
')
	local pair ev_a ev_b ev_a_json ev_b_json agree
	for pair in "${pair_lines[@]}"; do
		read -r ev_a ev_b <<<"${pair}"
		capture ev_a_json lib_py mx_events.py fetch "${room_id}" "${ev_a}" --timeout 30
		capture ev_b_json lib_py mx_events.py fetch "${room_id}" "${ev_b}" --timeout 30
		agree="$(medre_lib_py normal_checks.py parity-agree "${ev_a_json}" "${ev_b_json}")"
		printf '%s' "${agree}" | grep -q '"ok": true' ||
			SC_FAIL "[${variant}] same-packet sender-agreement violated (events claim one packet but disagree on attribution)" \
				"verdict: ${agree}" $'\n'"events=${ev_a}/${ev_b}" $'\n'"scan: ${out}"
	done
	if ((${#pair_lines[@]} > 0)); then
		SC_PASS "[${variant}] same-packet agreement holds (channel + sender attribution agree via parity-agree)" \
			"verdict: ${dup_verdict}"
	else
		SC_PASS "[${variant}] same-packet agreement holds (no channel incoherence; no same-packet group observed)" \
			"verdict: ${dup_verdict}"
	fi
}

run_variant "ch0" "${ROOM_BRIDGE_CH0_ID}" "" "0"

if [[ -n ${ROOM_BRIDGE_CH2_ID} ]]; then
	run_variant "ch2" "${ROOM_BRIDGE_CH2_ID}" "2" "2"
else
	SC_SKIP "secondary-channel variant needs the rig's #lab-bridge-ch2 room" \
		"publish ROOM_BRIDGE_CH2_ALIAS in lab/state/scenario.env (scenarios/lib/normal_lib.sh header)"
fi

SC_LOG "[${SCENARIO}] complete"
