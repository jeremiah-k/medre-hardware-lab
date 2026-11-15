#!/usr/bin/env bash
# =============================================================================
# Scenario: blank-reply-plugin
#
# A blank Matrix reply (empty body carrying a native m.in_reply_to relation to
# a mapped radio-origin event) must NEVER enter the structured reply branch
# that would send a radio reply. Whether it stays observable to plugins is
# reported as an OWNER-ACCEPTED DIAGNOSTIC: the owner resolved (2026-11-10)
# that empty-body events are accepted current behavior — real clients and
# Synapse effectively prevent them — so this scenario reports WHICH gate
# fired (plugin observation vs the empty-text early-return in
# on_room_message) and only the radio-side guarantee is pass/fail. The r13
# doc claim is being corrected on the mmrelay branch separately.
#
# Needs the echo_observed test plugin mounted in both relays (see
# scenarios/README.md REQUIREMENT FOR THE RIG #1).
#
# Assertions:
#   PASS/FAIL: no message_map row keyed by the blank reply's event id in
#   EITHER relay (a structured radio reply would have written one after the
#   confirmed send), and no relay-authored room event referencing it.
#   DIAGNOSTIC (always PASS, evidence printed): whether the echo_observed
#   plugin observed the blank reply (normal-path visibility) or the
#   empty-text early-return gate fired instead.
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_rig
resolve_rooms

[[ -n ${PLUGIN_OBS_A} && -n ${PLUGIN_OBS_B} ]] ||
	SC_SKIP "echo_observed paths not configured (PLUGIN_OBS_A/B empty)" \
		"REQUIREMENT FOR THE RIG #1 must be implemented; see scenarios/README.md"

SCENARIO="blank-reply-plugin"
MARKER="BLANKSEED-$(SC_UID)"
SINCE_MS="$(date +%s%3N)"

# --- Act 1: Alpha-origin message from the peer node, mapped in both relays ---
capture out lib_py radio.py send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${MARKER}"
((SC_STATUS == 0)) || SC_FAIL "seed radio injection failed" "${out}"
capture seed_json lib_py mx_events.py wait "${ROOM_ALPHA_CH0_ID}" \
	--sender "${BOT_A_MXID}" --body-contains "${MARKER}" \
	--require-envelope --envelope-kind message.created --since-ms "${SINCE_MS}" \
	--timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "no canonical AlphaLab seed event" "${seed_json}"
SEED_EVENT_ID="$(jq_get "${seed_json}" event.event_id | tr -d '"')"
SC_PASS "seed event mapped in #lab-alpha-ch0" "seed_event=${SEED_EVENT_ID}"

# --- Act 2: blank Matrix reply from the driver user ------------------------------
capture blank_json lib_py mx_blank_reply.py "${ROOM_ALPHA_CH0_ID}" "${SEED_EVENT_ID}" "BLANK-$(SC_UID)"
((SC_STATUS == 0)) || SC_FAIL "blank reply send failed" "${blank_json}"
BLANK_EVENT_ID="$(jq_get "${blank_json}" event_id | tr -d '"')"
SC_PASS "blank reply sent" "event_id=${BLANK_EVENT_ID} in_reply_to=${SEED_EVENT_ID}"

sleep "${SETTLE_SECONDS}"

# --- Assertion 1 (PASS/FAIL): no structured radio reply --------------------------
capture rows_a lib_py maps.py rows-for-event "${MAP_DB_A}" "${BLANK_EVENT_ID}"
count_a=$(jq_get "${rows_a}" 'rows' | grep -c '"meshtastic_id"' || true)
capture rows_b lib_py maps.py rows-for-event "${MAP_DB_B}" "${BLANK_EVENT_ID}"
count_b=$(jq_get "${rows_b}" 'rows' | grep -c '"meshtastic_id"' || true)
((count_a == 0 && count_b == 0)) ||
	SC_FAIL "blank reply entered the structured reply branch (map rows exist)" \
		"A_rows=${rows_a}" $'\n'"B_rows=${rows_b}"
SC_PASS "no message_map row for the blank reply in either relay (no radio reply sent)"

# --- Assertion 2 (PASS/FAIL): no relay-authored room event references it ---------
capture out lib_py mx_events.py scan "${ROOM_ALPHA_CH0_ID}" \
	--since-ms "${SINCE_MS}" --sender "${BOT_A_MXID}" --relates-to "${BLANK_EVENT_ID}"
scan_a=$(jq_get "${out}" 'matches' | grep -c '"event_id"' || true)
capture out lib_py mx_events.py scan "${ROOM_ALPHA_CH0_ID}" \
	--since-ms "${SINCE_MS}" --sender "${BOT_B_MXID}" --relates-to "${BLANK_EVENT_ID}"
scan_b=$(jq_get "${out}" 'matches' | grep -c '"event_id"' || true)
((scan_a == 0 && scan_b == 0)) ||
	SC_FAIL "a relay produced an event referencing the blank reply" "A=${scan_a} B=${scan_b}"
SC_PASS "no relay-authored room event references the blank reply"

# --- DIAGNOSTIC (owner-accepted): which gate handled the blank reply -------------
capture obs_a lib_py observe.py find "${PLUGIN_OBS_A}" --event-id "${BLANK_EVENT_ID}" \
	--timeout "$((MATRIX_WAIT_SECONDS / 2))"
if ((SC_STATUS == 0)); then
	gate_a="plugin-observed (normal path)"
	evidence_a="${obs_a}"
else
	gate_a="empty-text early-return gate in on_room_message (event dropped before plugin dispatch)"
	evidence_a="${obs_a}"
fi
capture obs_b lib_py observe.py find "${PLUGIN_OBS_B}" --event-id "${BLANK_EVENT_ID}" \
	--timeout "$((MATRIX_WAIT_SECONDS / 2))"
if ((SC_STATUS == 0)); then
	gate_b="plugin-observed (normal path)"
	evidence_b="${obs_b}"
else
	gate_b="empty-text early-return gate in on_room_message (event dropped before plugin dispatch)"
	evidence_b="${obs_b}"
fi
echo "DIAGNOSTIC (owner-accepted behavior, 2026-11-10): relay A blank-reply gate: ${gate_a}"
printf '%s\n' "${evidence_a}" | SC_EVIDENCE
echo "DIAGNOSTIC (owner-accepted behavior, 2026-11-10): relay B blank-reply gate: ${gate_b}"
printf '%s\n' "${evidence_b}" | SC_EVIDENCE
SC_PASS "blank-reply routing reported (radio-side guarantee held; see DIAGNOSTIC lines above for the gate each relay used)"

SC_LOG "[$SCENARIO] complete"
