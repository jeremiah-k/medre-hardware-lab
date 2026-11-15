#!/usr/bin/env bash
# =============================================================================
# Scenario: medre-parity
#
# Cross-implementation parity on a shared mesh: one radio text injected from
# node A's PEER must produce corpus-valid canonical events from BOTH bridges
# in the same room - @relay-a (mmrelay) AND the medre bot - and the two
# envelopes must AGREE on the observable native facts:
#
#   * channel identity: source_channel == the injected channel ("0"/"2"),
#     numeric, on both events (r14 invariant class);
#   * native reference: source_native_ref.native_message_id == the injected
#     packet id on both events (both bridges observed the same over-air
#     packet), numeric native_channel_id;
#   * attribution: bridge_context.source.sender_id (the native node id)
#     agrees; sender_name agrees whenever both bridges resolved one (node-db
#     attribution; a fresh medre node db may lag, which is a PRODUCT finding
#     when it persists);
#   * rendering: prefix format MAY differ between the bridges, but each body
#     carries the same authored text exactly once.
#
# Variants: primary channel 0 and secondary channel 2 (per-meshnet x channel
# rooms).
#
# Every assertion is state/API only; every event is judged by the corpus
# validator with medre's installed schema as the authority.
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=lib/medre_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/medre_lib.sh"

require_rig
require_medre_bridge
resolve_rooms

SCENARIO="medre-parity"

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

run_variant() {
	local variant="$1" room_id="$2" channel_arg="$3" expected_channel="$4"
	local marker since_ms radio_out relay_json medre_json packet_id
	local relay_event_id medre_event_id relay_sender medre_sender
	local relay_name medre_name count

	marker="PARITY-${variant}-$(SC_UID)"
	# since-ms bound is captured BEFORE the blocking injection (2026-11-10
	# triage: emissions can predate a bound captured afterwards).
	since_ms="$(date +%s%3N)"

	SC_LOG "[${variant}] injecting AlphaLab radio text from the peer node: ${marker}"
	local radio_args=(send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${marker}")
	if [[ -n ${channel_arg} ]]; then
		radio_args+=(--channel "${channel_arg}")
	fi
	capture radio_out lib_py radio.py "${radio_args[@]}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] radio injection failed" "${radio_out}"
	packet_id="$(jq_get "${radio_out}" packet_id | tr -d '"')"
	[[ ${packet_id} =~ ^[0-9]+$ ]] ||
		SC_FAIL "[${variant}] radio injection did not report a packet id" "${radio_out}"
	SC_PASS "[${variant}] radio text injected on channel ${expected_channel}" \
		"packet_id=${packet_id} marker=${marker}"

	# --- Both bridges' events, corpus-validated --------------------------------
	capture relay_json lib_py mx_events.py wait "${room_id}" \
		--sender "${BOT_A_MXID}" --body-contains "${marker}" \
		--require-envelope --envelope-kind message.created \
		--source-channel "${expected_channel}" --native-ref-is-numeric \
		--native-ref-msg-id "${packet_id}" \
		--since-ms "${since_ms}" --timeout "${MATRIX_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] no canonical @relay-a event on channel ${expected_channel} for packet ${packet_id}" "${relay_json}"
	relay_event_id="$(jq_get "${relay_json}" event.event_id | tr -d '"')"
	medre_assert_valid "${relay_json}" "[${variant}] @relay-a event"
	SC_PASS "[${variant}] @relay-a envelope agrees with the injected packet" \
		"event=${relay_event_id} source_channel=${expected_channel} native_message_id=${packet_id}"

	capture medre_json lib_py mx_events.py wait "${room_id}" \
		--sender "${MEDRE_BOT_MXID}" --body-contains "${marker}" \
		--require-envelope --envelope-kind message.created \
		--source-channel "${expected_channel}" --native-ref-is-numeric \
		--native-ref-msg-id "${packet_id}" \
		--since-ms "${since_ms}" --timeout "${MATRIX_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] no canonical ${MEDRE_BOT_MXID} event on channel ${expected_channel} for packet ${packet_id} (medre bot did not bridge the radio text, or its envelope disagrees on channel/packet)" "${medre_json}"
	medre_event_id="$(jq_get "${medre_json}" event.event_id | tr -d '"')"
	medre_assert_valid "${medre_json}" "[${variant}] medre bot event"

	# --- Envelope agreement ------------------------------------------------------
	relay_sender="$(jq_get "${relay_json}" event.content.medre.metadata.bridge_context.source.sender_id | tr -d '"')"
	medre_sender="$(jq_get "${medre_json}" event.content.medre.metadata.bridge_context.source.sender_id | tr -d '"')"
	# A missing sender attribution makes the parity claim VACUOUS: hard-fail
	# on it (same discipline as normal-parity), never skip silently.
	[[ -n ${relay_sender} && ${relay_sender} != "null" ]] ||
		SC_FAIL "[${variant}] @relay-a envelope carries no sender attribution (the parity claim is vacuous without it)" \
			"${relay_json}"
	[[ ${relay_sender} == "${medre_sender}" ]] ||
		SC_FAIL "[${variant}] sender attribution disagreement (native node id)" \
			"relay_a=${relay_sender}" $'\n'"medre=${medre_sender}" $'\n'"packet=${packet_id} events=${relay_event_id}/${medre_event_id}"
	SC_PASS "[${variant}] sender attribution agrees on the native node id" \
		"sender_id=${relay_sender}"

	relay_name="$(jq_get "${relay_json}" event.content.medre.metadata.bridge_context.source.sender_name | tr -d '"')"
	medre_name="$(jq_get "${medre_json}" event.content.medre.metadata.bridge_context.source.sender_name | tr -d '"')"
	if [[ -n ${relay_name} && ${relay_name} != "null" &&
		-n ${medre_name} && ${medre_name} != "null" ]]; then
		[[ ${relay_name} == "${medre_name}" ]] ||
			SC_FAIL "[${variant}] sender_name disagreement (node-db attribution)" \
				"relay_a=${relay_name} medre=${medre_name} events=${relay_event_id}/${medre_event_id}"
		SC_PASS "[${variant}] sender_name (longname attribution) agrees" "sender_name=${relay_name}"
	else
		SC_LOG "[${variant}] NOTE: sender_name unresolved on one bridge (relay_a=${relay_name}, medre=${medre_name}) - attribution contract allows absent labels; corpus requires one of name/short_name"
	fi

	# --- Same authored text, once per rendering ---------------------------------
	for ev in "${relay_json}" "${medre_json}"; do
		count="$(body_count "${ev}" event.content.body "${marker}")"
		[[ ${count} == "1" ]] ||
			SC_FAIL "[${variant}] authored text not carried exactly once in the body" \
				"occurrences=${count} event_json=${ev}"
	done
	SC_PASS "[${variant}] both bridges carry the same authored text exactly once" \
		"marker=${marker} relay=${relay_event_id} medre=${medre_event_id}"
}

run_variant "ch0" "${ROOM_ALPHA_CH0_ID}" "" "0"

if [[ -n ${ROOM_ALPHA_CH2_ID} ]]; then
	run_variant "ch2" "${ROOM_ALPHA_CH2_ID}" "2" "2"
else
	SC_SKIP "secondary-channel variant needs the rig's #lab-alpha-ch2 room" \
		"publish ROOM_ALPHA_CH2_ALIAS in lab/state/scenario.env (scenarios/README.md)"
fi

SC_LOG "[${SCENARIO}] complete"
