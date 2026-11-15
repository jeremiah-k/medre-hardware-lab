#!/usr/bin/env bash
# =============================================================================
# Scenario: reply-correlation
#
# A radio reply from the BETA PEER node to an Alpha-origin message must
# produce a Matrix reply event whose native relation targets the ORIGINAL
# event, with matching native channel ids on both sides, and must land on
# Alpha as a structured radio reply bound to the original packet id.
#
# Variants: primary channel 0 and the secondary channel 2 (per-meshnet x
# channel rooms: #lab-alpha-ch0/#lab-alpha-ch2 with Beta replies from the
# beta peer on the same channel index).
#
# Assertions (state/API only):
#   1. The reply event from @relay-b has m.relates_to.m.in_reply_to ==
#      original event id, envelope relation_info=reply, and the bridge_context
#      relation hint target_native_ref.native_message_id == original event id.
#   2. The reply event's envelope source_native_ref carries the SAME channel
#      id as the original event's envelope source_native_ref ("0" / "2").
#   3. Relay B's map gains a row keyed by the reply event (B's radio packet),
#      and relay A's map gains a row keyed by the reply event too: A resolved
#      the original through its own map and bridged the reply as a structured
#      radio reply (rows are written only after confirmed radio sends).
#   4. Radio-side: a sniffer on the ALPHA peer catches A's structured reply
#      packet with reply_id == A's own mapped packet id for the original.
#   5. Evidence immutability: the original event and both its map rows are
#      unchanged after the reply flow (re-verified at the end).
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_rig
resolve_rooms

SCENARIO="reply-correlation"

run_variant() {
	local variant="$1" room_id="$2" channel_arg="$3" expected_channel="$4"
	local marker reply_marker since_ms event_id native_packet out

	marker="REPLY-${variant}-$(SC_UID)"
	reply_marker="R${variant}-$(SC_UID)"
	since_ms="$(date +%s%3N)"

	# --- Act 1: Alpha-origin message from the ALPHA PEER on the channel ------
	local radio_args=(send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${marker}")
	if [[ -n ${channel_arg} ]]; then
		radio_args+=(--channel "${channel_arg}")
	fi
	capture out lib_py radio.py "${radio_args[@]}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] radio injection failed" "${out}"

	capture event_id_from_json lib_py mx_events.py wait "${room_id}" \
		--sender "${BOT_A_MXID}" --body-contains "${marker}" \
		--require-envelope --envelope-kind message.created \
		--source-channel "${expected_channel}" --native-ref-is-numeric \
		--since-ms "${since_ms}" --timeout "${MATRIX_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] no canonical AlphaLab event on channel ${expected_channel}" "${event_id_from_json}"
	event_id="$(jq_get "${event_id_from_json}" event.event_id | tr -d '"')"
	native_packet="$(jq_get "${event_id_from_json}" event.content.medre.envelope.source_native_ref.native_message_id | tr -d '"')"

	# Beta must have bridged the original to its radio before a radio reply
	# can target it (B's map row keyed by the original event).
	capture out lib_py maps.py rows-for-event "${MAP_DB_B}" "${event_id}" --wait "${RADIO_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] relay B map missing the original event row" "${out}"
	local b_packet
	b_packet=$(printf '%s' "${out}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')
	SC_PASS "[${variant}] original event mapped in both relay namespaces" \
		"original_event=${event_id} A_packet(envelope)=${native_packet} B_packet(bridge)=${b_packet}"

	# --- Act 2: radio reply from the BETA PEER on the same channel ------------
	# The sniffer on the ALPHA peer is armed first: if relay A bridges the
	# reply as a structured radio reply, its packet targets A's own mapped
	# packet id for the original.
	local a_map_packet=""
	capture out lib_py maps.py rows-for-event "${MAP_DB_A}" "${event_id}" --wait "${RADIO_WAIT_SECONDS}"
	if ((SC_STATUS == 0)); then
		a_map_packet=$(printf '%s' "${out}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')
	fi
	if [[ -n ${a_map_packet} ]]; then
		start_sniffer SNIFF "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
			--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
			--expect-text "${reply_marker}" --expect-reply-id "${a_map_packet}"
	else
		start_sniffer SNIFF "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
			--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
			--expect-text "${reply_marker}"
	fi
	trap 'stop_sniffer "${SNIFF_PID}" "${SNIFF}"' EXIT

	local reply_args=(send-text "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" "${reply_marker}" --reply-id "${b_packet}")
	if [[ -n ${channel_arg} ]]; then
		reply_args+=(--channel "${channel_arg}")
	fi
	# The wait's lower bound must predate the injection: relay B can emit
	# the Matrix reply while the blocking injection command is still
	# settling, and events older than --since-ms are invisible to it.
	local since_reply
	since_reply="$(date +%s%3N)"
	capture out lib_py radio.py "${reply_args[@]}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] radio reply injection failed" "${out}"

	# --- Assertions ------------------------------------------------------------
	capture reply_json lib_py mx_events.py wait "${room_id}" \
		--sender "${BOT_B_MXID}" --body-contains "${reply_marker}" \
		--require-envelope --relation-info reply \
		--relates-to "${event_id}" --hint-target-msg-id "${event_id}" \
		--since-ms "${since_reply}" --timeout "${MATRIX_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] reply event missing native relation to the original event" "${reply_json}"

	local reply_id reply_channel
	reply_id="$(jq_get "${reply_json}" event.event_id | tr -d '"')"
	reply_channel="$(jq_get "${reply_json}" event.content.medre.envelope.source_native_ref.native_channel_id | tr -d '"')"
	[[ ${reply_channel} == "${expected_channel}" ]] ||
		SC_FAIL "[${variant}] reply envelope source channel != original channel" \
			"reply=${reply_id} reply_channel=${reply_channel} expected=${expected_channel}"
	SC_PASS "[${variant}] reply relation targets the original with matching channel id" \
		"reply=${reply_id} in_reply_to=${event_id} channel=${reply_channel}"

	# Radio-side: A's structured reply must target A's own mapped packet id.
	if [[ -n ${a_map_packet} ]]; then
		if wait_until "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" grep -q '"match"' "${SNIFF}"; then
			SC_PASS "[${variant}] structured radio reply observed on the Alpha peer (reply_id=${a_map_packet})" \
				"sniff: $(grep -o '"match":.*' "${SNIFF}" | head -1)"
		else
			SC_FAIL "[${variant}] relay A did not send a structured radio reply targeting packet ${a_map_packet}" \
				"sniff log: $(cat "${SNIFF}")"
		fi
	fi
	stop_sniffer "${SNIFF_PID}" "${SNIFF}"
	trap - EXIT

	# Relay B maps its own radio reply packet; relay A maps the reply event too.
	capture out lib_py maps.py rows-for-event "${MAP_DB_B}" "${reply_id}" --wait "${RADIO_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] relay B map missing its radio reply packet row" "${out}"
	capture out_a lib_py maps.py rows-for-event "${MAP_DB_A}" "${reply_id}" --wait "${RADIO_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] relay A did not bridge the reply to its radio (no structured reply row)" "${out_a}"
	SC_PASS "[${variant}] structured reply delivered on Alpha (map rows on both sides)" \
		"B_rows=${out}" $'\n'"A_rows=${out_a}"

	# --- Evidence immutability re-check ----------------------------------------
	capture recheck lib_py mx_events.py fetch "${room_id}" "${event_id}" --timeout 30
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] original event disappeared from the room" "${recheck}"
	local recheck_canon recheck_ref orig_canon
	recheck_canon="$(jq_get "${recheck}" content.medre.envelope.canonical_event_id | tr -d '"')"
	recheck_ref="$(jq_get "${recheck}" content.medre.envelope.source_native_ref.native_message_id | tr -d '"')"
	orig_canon="$(jq_get "${event_id_from_json}" event.content.medre.envelope.canonical_event_id | tr -d '"')"
	[[ ${recheck_canon} == "${orig_canon}" && ${recheck_ref} == "${native_packet}" ]] ||
		SC_FAIL "[${variant}] original event evidence was rewritten by later activity" \
			"before: canonical=${orig_canon} packet=${native_packet}" $'\n'"after: canonical=${recheck_canon} packet=${recheck_ref}"
	capture out lib_py maps.py rows-for-event "${MAP_DB_A}" "${event_id}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] A's original map row vanished" "${out}"
	capture out lib_py maps.py rows-for-event "${MAP_DB_B}" "${event_id}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] B's original map row vanished" "${out}"
	SC_PASS "[${variant}] original evidence unchanged after the reply flow" \
		"event=${event_id} canonical=${orig_canon} packet=${native_packet}"
}

# --- Variant 1: primary channel (omitted on the wire => defaults to "0") -----
run_variant "ch0" "${ROOM_ALPHA_CH0_ID}" "" "0"

# --- Variant 2: secondary channel 2 -------------------------------------------
if [[ -n ${ROOM_ALPHA_CH2_ID} ]]; then
	run_variant "ch2" "${ROOM_ALPHA_CH2_ID}" "2" "2"
else
	SC_SKIP "secondary-channel variant needs the rig's #lab-alpha-ch2 room" \
		"publish ROOM_ALPHA_CH2_ALIAS in lab/state/scenario.env (scenarios/README.md)"
fi

SC_LOG "[${SCENARIO}] complete"
