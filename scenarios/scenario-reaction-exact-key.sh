#!/usr/bin/env bash
# =============================================================================
# Scenario: reaction-exact-key
#
# A radio tapback from the BETA PEER node must relay the EXACT reaction symbol as the
# canonical relation key (never a prose-recovered paraphrase), scoped to the
# radio-native target packet id - including a reaction whose target packet id
# is 0.
#
# Assertions (state/API only):
#   1. For a normal target: a room event from @relay-b with envelope
#      event_kind=message.reacted whose bridge_context relation hint carries
#      relation_type=reaction, key == the exact injected symbol (byte-exact,
#      Unicode modifiers included), and target_native_ref
#      {adapter=mmrelay.meshtastic, native_channel_id="0",
#      native_message_id == Beta's packet id for the original event}.
#   2. The reacted event is an m.emote fallback carrying the exact key, and
#      the key string appears nowhere re-worded (the symbol itself is the
#      assertion, not the preview text).
#
# NOT live-testable: a tapback with reply_id=0 against a seeded packet-id-0
# map row. Over the wire reply_id=0 is a proto3 default and is
# indistinguishable from "no reply" somewhere in the daily-firmware +
# python-transport stack (proven live 2026-11-10: relay B routed the
# reply_id=0 tapback to plain-text handling, no relation branch), so the
# packet-id-0 contract ("zero stays a valid target, never dropped as
# falsy") is covered by the relay's crafted-dict unit tests instead
# (tests/test_meshtastic_utils_messages_routing.py).
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_rig
resolve_rooms

SCENARIO="reaction-exact-key"

# run_reaction VARIANT KEY [target_packet_id seed_event_id]
run_reaction() {
	local variant="$1" key="$2" target_packet="$3" seed_event_id="${4:-}"
	local marker since_ms orig_json orig_event_id out react_json react_id

	marker="REACT-${variant}-$(SC_UID)"
	since_ms="$(date +%s%3N)"

	# --- Act 1: Alpha-origin message from the ALPHA PEER; Beta bridges it ----
	capture out lib_py radio.py send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${marker}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] radio injection failed" "${out}"
	capture orig_json lib_py mx_events.py wait "${ROOM_ALPHA_CH0_ID}" \
		--sender "${BOT_A_MXID}" --body-contains "${marker}" \
		--require-envelope --envelope-kind message.created --source-channel "0" \
		--since-ms "${since_ms}" --timeout "${MATRIX_WAIT_SECONDS}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] no canonical AlphaLab event" "${orig_json}"
	orig_event_id="$(jq_get "${orig_json}" event.event_id | tr -d '"')"

	if [[ -z ${target_packet} ]]; then
		# Real target: Beta's own retransmission packet id.
		capture out lib_py maps.py rows-for-event "${MAP_DB_B}" "${orig_event_id}" --wait "${RADIO_WAIT_SECONDS}"
		((SC_STATUS == 0)) ||
			SC_FAIL "[${variant}] relay B map missing the original row" "${out}"
		target_packet="$(printf '%s' "${out}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')"
	fi

	# --- Act 2: radio tapback from the BETA PEER -------------------------------
	local since_react; since_react="$(date +%s%3N)"
	capture out lib_py radio.py send-reaction "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" "${key}" "${target_packet}"
	((SC_STATUS == 0)) || SC_FAIL "[${variant}] tapback injection failed" "${out}"

	# --- Assertions ------------------------------------------------------------
	# The reacted event's envelope source_native_ref is the tapback packet
	# itself; the RADIO TARGET lives in the relation hint. The hint key must
	# equal the injected symbol byte-for-byte.
	capture react_json lib_py mx_events.py wait "${ROOM_ALPHA_CH0_ID}" \
		--sender "${BOT_B_MXID}" --require-envelope --envelope-kind message.reacted \
		--relation-info reaction --native-ref-is-numeric --native-ref-channel "0" \
		--hint-target-msg-id "${target_packet}" --hint-key-equals "${key}" \
		--since-ms "${since_react}" --timeout "${MATRIX_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "[${variant}] no reacted event carrying the exact key ${key} and target packet ${target_packet}" "${react_json}"
	react_id="$(jq_get "${react_json}" event.event_id | tr -d '"')"
	local msgtype; msgtype="$(jq_get "${react_json}" event.content.msgtype | tr -d '"')"
	[[ "${msgtype}" == "m.emote" ]] ||
		SC_FAIL "[${variant}] radio reaction must render as the m.emote fallback" "${react_json}"
	SC_PASS "[${variant}] exact reaction key relayed with radio-native target" \
		"key=${key} target_packet=${target_packet} event=${react_id}" $'\n'"${react_json}"
}

# --- Case 1: plain symbol ------------------------------------------------------
run_reaction "emoji" "🦄" "" ""

# --- Case 2: symbol with skin-tone modifier (byte-exact key requirement) -------
run_reaction "modifier" "👍🏽" "" ""

SC_LOG "[$SCENARIO] complete"
