#!/usr/bin/env bash
# =============================================================================
# Scenario: channel-matrix
#
# The r14 regression class, live: whatever channel an AlphaLab or BetaLab
# message rides, every native reference the relay emits - the envelope source
# ref of the original event, and the relation targets of the far mesh's reply
# and reaction to it - must carry the SAME channel id on both sides, and
# never a meshnet label. With per-meshnet x channel rooms each room's native
# refs are unambiguous about origin mesh and channel.
#
# Sub-cases (each fully asserted through Matrix event content + map state):
#   alpha-ch0-omitted  peer radio send with no explicit channel (library
#                      default) into #lab-alpha-ch0              -> "0"
#   alpha-ch0-explicit peer radio send with explicit channelIndex 0     -> "0"
#   alpha-ch2          peer radio send on channelIndex 2 through
#                      #lab-alpha-ch2 (SKIP if the rig lacks the room)  -> "2"
#   beta-ch2           mirror of alpha-ch2 on the Beta mesh through
#                      #lab-beta-ch2 (SKIP if the rig lacks the room)   -> "2"
#   malformed          not injectable over the radio API (firmware assigns
#                      channels); documented, covered by unit tests.
#
# Per sub-case:
#   1. Original event's envelope source_native_ref: native_channel_id equals
#      the expected channel and native_message_id is numeric.
#   2. Far-mesh radio reply from the far PEER on the SAME channel: envelope
#      source channel == expected channel, relation targets the original.
#   3. Far-mesh radio reaction: relation hint target on the expected channel
#      with the exact key.
#   4. Same channel id on both sides: original's source channel == reply's
#      source channel == reaction's target channel.
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_rig
resolve_rooms

SCENARIO="channel-matrix"

# run_case VARIANT ROOM INJECT_HOST INJECT_PORT REPLY_HOST REPLY_PORT CHANNEL_ARG EXPECTED ORIGIN_BOT REPLY_BOT OTHER_MAP
run_case() {
	local variant="$1" room_id="$2" inject_host="$3" inject_port="$4" reply_host="$5" reply_port="$6" channel_arg="$7" expected="$8" origin_bot="$9" reply_bot="${10}" other_map="${11}"
	local marker since_ms out orig_json orig_id orig_channel orig_packet
	local reply_marker react_key b_packet

	marker="CH-${variant}-$(SC_UID)"
	reply_marker="CHR-${variant}-$(SC_UID)"
	react_key="🛰"
	since_ms="$(date +%s%3N)"

	# --- Act 1: origin-mesh message from its PEER node on the channel ---------
	local radio_args=(send-text "${inject_host}" "${inject_port}" "${marker}")
	if [[ "${channel_arg}" != "-" ]]; then
		radio_args+=(--channel "${channel_arg}")
	fi
	capture out lib_py radio.py "${radio_args[@]}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] radio injection failed" "${out}"

	capture orig_json lib_py mx_events.py wait "${room_id}" \
		--sender "${origin_bot}" --body-contains "${marker}" \
		--require-envelope --envelope-kind message.created \
		--source-channel "${expected}" --native-ref-is-numeric \
		--since-ms "${since_ms}" --timeout "${MATRIX_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] original event missing expected native channel ${expected} (and numeric ids)" "${orig_json}"
	orig_id="$(jq_get "${orig_json}" event.event_id | tr -d '"')"
	orig_channel="$(jq_get "${orig_json}" event.content.medre.envelope.source_native_ref.native_channel_id | tr -d '"')"
	orig_packet="$(jq_get "${orig_json}" event.content.medre.envelope.source_native_ref.native_message_id | tr -d '"')"
	SC_PASS "[${variant}] original event native ref on channel ${orig_channel} (packet ${orig_packet})"

	# The far relay must hold the original before its radio reply can target it.
	capture out lib_py maps.py rows-for-event "${other_map}" "${orig_id}" --wait "${RADIO_WAIT_SECONDS}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] far relay map missing the original row" "${out}"
	b_packet=$(printf '%s' "${out}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')

	# --- Act 2: far-mesh peer radio reply + reaction on the SAME channel ------
	local since_reply since_react
	since_reply="$(date +%s%3N)"
	local reply_args=(send-text "${reply_host}" "${reply_port}" "${reply_marker}" --reply-id "${b_packet}")
	if [[ "${channel_arg}" != "-" ]]; then
		reply_args+=(--channel "${channel_arg}")
	fi
	capture out lib_py radio.py "${reply_args[@]}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] far-mesh radio reply failed" "${out}"

	capture reply_json lib_py mx_events.py wait "${room_id}" \
		--sender "${reply_bot}" --body-contains "${reply_marker}" \
		--require-envelope --relation-info reply --relates-to "${orig_id}" \
		--native-ref-is-numeric --native-ref-channel "${expected}" \
		--since-ms "${since_reply}" --timeout "${MATRIX_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] far-mesh reply missing native relation/channel ${expected}" "${reply_json}"
	local reply_id reply_channel
	reply_id="$(jq_get "${reply_json}" event.event_id | tr -d '"')"
	reply_channel="$(jq_get "${reply_json}" event.content.medre.envelope.source_native_ref.native_channel_id | tr -d '"')"

	since_react="$(date +%s%3N)"
	local react_args=(send-reaction "${reply_host}" "${reply_port}" "${react_key}" "${b_packet}")
	if [[ "${channel_arg}" != "-" ]]; then
		react_args+=(--channel "${channel_arg}")
	fi
	capture out lib_py radio.py "${react_args[@]}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] far-mesh radio reaction failed" "${out}"

	capture react_json lib_py mx_events.py wait "${room_id}" \
		--sender "${reply_bot}" --require-envelope --envelope-kind message.reacted \
		--relation-info reaction --hint-key-equals "${react_key}" \
		--hint-target-msg-id "${b_packet}" --native-ref-is-numeric \
		--native-ref-channel "${expected}" \
		--since-ms "${since_react}" --timeout "${MATRIX_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] far-mesh reaction missing exact key / channel ${expected} target" "${react_json}"

	# --- Same channel id both sides --------------------------------------------
	[[ "${orig_channel}" == "${expected}" && "${reply_channel}" == "${expected}" ]] ||
		SC_FAIL "[${variant}] channel identity disagreement between original and reply" \
			"orig=${orig_channel} reply=${reply_channel} expected=${expected}"
	SC_PASS "[${variant}] reply+reaction carry the SAME channel id on both sides" \
		"original=${orig_id} ch=${orig_channel}" $'\n'"reply=${reply_id} ch=${reply_channel}" $'\n'"reaction_target_packet=${b_packet} key=${react_key}"
}

run_case "alpha-ch0-omitted" "${ROOM_ALPHA_CH0_ID}" "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" "-" "0" \
	"${BOT_A_MXID}" "${BOT_B_MXID}" "${MAP_DB_B}"
run_case "alpha-ch0-explicit" "${ROOM_ALPHA_CH0_ID}" "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" "0" "0" \
	"${BOT_A_MXID}" "${BOT_B_MXID}" "${MAP_DB_B}"

if [[ -n ${ROOM_ALPHA_CH2_ID} ]]; then
	run_case "alpha-ch2" "${ROOM_ALPHA_CH2_ID}" "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
		"${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" "2" "2" \
		"${BOT_A_MXID}" "${BOT_B_MXID}" "${MAP_DB_B}"
else
	SC_SKIP "alpha-ch2 sub-case needs the rig's #lab-alpha-ch2 room" \
		"publish ROOM_ALPHA_CH2_ALIAS in lab/state/scenario.env"
fi

if [[ -n ${ROOM_BETA_CH2_ID} ]]; then
	run_case "beta-ch2" "${ROOM_BETA_CH2_ID}" "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
		"${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "2" "2" \
		"${BOT_B_MXID}" "${BOT_A_MXID}" "${MAP_DB_A}"
else
	SC_SKIP "beta-ch2 sub-case needs the rig's #lab-beta-ch2 room" \
		"publish ROOM_BETA_CH2_ALIAS in lab/state/scenario.env"
fi

# The malformed-channel sub-case is not injectable over the TCP radio API: the
# firmware assigns channels, so no malformed packet channel can be produced
# live. Record the limitation and finish with the injectable sub-cases' result.
SC_LOG "NOTE: malformed-channel sub-case skipped (not injectable over the radio API;"
SC_LOG "NOTE: parsing branches covered by mmrelay unit tests: test_on_meshtastic_message_text_app_malformed_channel_defaults_to_zero)"
SC_LOG "[$SCENARIO] complete"
