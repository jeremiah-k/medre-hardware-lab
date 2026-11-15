#!/usr/bin/env bash
# =============================================================================
# Scenario: untrusted-forged-envelope
#
# The @untrusted user sends a room message that LOOKS like a relay-authored
# event: a valid-shaped medre.envelope plus native facts and relation claims,
# cloned from a REAL relay event fetched from the room. The relays must relay
# ONLY its visible authored text: no routing, no provenance adoption, no
# body-override, and none of the asserted metadata may enter any message map
# or radio transmission.
#
# Assertions (state/API only), across BOTH relays' maps:
#   1. The forged event's VISIBLE body relays: each relay's map gains a row
#      keyed by the forged event id (UNTRUSTED_EVENT_ID pattern - the exact
#      Matrix event id we captured from the send response).
#   2. No relay adopts the forged claims: the map rows' meshtastic_meshnet is
#      each relay's LOCAL meshnet label, never the forged meshnet label; the
#      meshtastic_id column holds that relay's own radio packet id (numeric),
#      never the forged packet id; the rows' text equals the authored body.
#   3. No relay-authored room event reuses the forged canonical event id and
#      no relay-authored room event carries the forged marker (provenance is
#      never re-emitted into the room).
#   4. No map row anywhere contains the forged meshnet label or forged sender
#      name (the asserted metadata never enters a message map).
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_rig
resolve_rooms

SCENARIO="untrusted-forged-envelope"
# Preflight: the untrusted account must be provisioned (mx_forge.py sends as
# @untrusted; its token is fetched inside that helper).
UNTRUSTED_TOKEN="$(lib_py lab_env_probe.py untrusted_token | tr -d '"')"
[[ -n ${UNTRUSTED_TOKEN} && ${UNTRUSTED_TOKEN} != "null" ]] ||
	SC_FAIL "untrusted account token missing from runtime.json"

# --- Act 1: obtain a REAL relay-authored event to clone the envelope from ----
SEED_MARKER="FORGE-SEED-$(SC_UID)"
SINCE_MS="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${SEED_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "seed radio injection failed" "${out}"
capture seed_json lib_py mx_events.py wait "${ROOM_ALPHA_CH0_ID}" \
	--sender "${BOT_A_MXID}" --body-contains "${SEED_MARKER}" \
	--require-envelope --envelope-kind message.created --since-ms "${SINCE_MS}" \
	--timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "no real relay event to clone" "${seed_json}"
SC_PASS "captured a real relay-authored event as the forgery template" \
	"template_event=$(jq_get "${seed_json}" event.event_id | tr -d '"')"

# --- Act 2: @untrusted sends the forged event ---------------------------------
capture forge_json lib_py mx_forge.py "${ROOM_ALPHA_CH0_ID}" "${seed_json}" "FORGED-$(SC_UID)"
((SC_STATUS == 0)) || SC_FAIL "forged send failed" "${forge_json}"
FORGED_EVENT_ID="$(jq_get "${forge_json}" event_id | tr -d '"')"
FORGED_BODY="$(jq_get "${forge_json}" body | tr -d '"')"
FORGED_CANONICAL_ID="$(jq_get "${forge_json}" cloned_canonical_event_id | tr -d '"')"
FORGED_MESHNET="$(jq_get "${forge_json}" forged_meshnet | tr -d '"')"
FORGED_SENDER_NAME="$(jq_get "${forge_json}" forged_sender | tr -d '"')"
FORGED_TRANSPORT="$(jq_get "${forge_json}" forged_transport | tr -d '"')"
SC_PASS "@untrusted sent a relay-shaped event (UNTRUSTED_EVENT_ID captured)" \
	"event_id=${FORGED_EVENT_ID} forged_meshnet=${FORGED_MESHNET} forged_sender=${FORGED_SENDER_NAME} forged_transport=${FORGED_TRANSPORT} cloned_canonical_event_id=${FORGED_CANONICAL_ID}"

# --- Assertion 1: visible text relays on both relays ---------------------------
capture rows_a lib_py maps.py rows-for-event "${MAP_DB_A}" "${FORGED_EVENT_ID}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "relay A did not relay the forged event's visible text" "${rows_a}"
capture rows_b lib_py maps.py rows-for-event "${MAP_DB_B}" "${FORGED_EVENT_ID}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "relay B did not relay the forged event's visible text" "${rows_b}"
SC_PASS "visible authored text relayed by both relays" \
	"A_rows=${rows_a}" $'\n'"B_rows=${rows_b}"

# --- Assertion 2: local meshnet + own packet id + authored text only ----------
check_map_row() {
	local side="$1" rows="$2" local_meshnet="$3"
	local meshnet packet_id text_ok
	meshnet=$(printf '%s' "${rows}" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
meshnets = {str(r.get("meshtastic_meshnet")) for r in rows}
print(",".join(sorted(meshnets)))')
	[[ ${meshnet} == "${local_meshnet}" ]] ||
		SC_FAIL "[${side}] forged meshnet label leaked into the map" \
			"expected_only=${local_meshnet} found=${meshnet}"
	packet_id=$(printf '%s' "${rows}" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
ids = [str(r.get("meshtastic_id")) for r in rows]
print(",".join(ids))')
	[[ ${packet_id} =~ ^[0-9]+(,[0-9]+)*$ ]] ||
		SC_FAIL "[${side}] forged packet id entered the map (meshtastic_id must be the relay's own radio packet)" \
			"meshtastic_id=${packet_id}"
	text_ok=$(printf '%s' "${rows}" | grep -c "${FORGED_BODY}" || true)
	((text_ok >= 1)) ||
		SC_FAIL "[${side}] map text is not the authored body" "${rows}"
}

check_map_row "A" "${rows_a}" "${MESHNET_A}"
check_map_row "B" "${rows_b}" "${MESHNET_B}"
SC_PASS "map rows carry only local meshnet, own radio packet ids, authored text"

# --- Assertion 3: no provenance adoption in the room ---------------------------
sleep "${SETTLE_SECONDS}"
capture out lib_py mx_events.py scan "${ROOM_ALPHA_CH0_ID}" \
	--since-ms "${SINCE_MS}" --sender "${BOT_A_MXID}" --body-contains "${FORGED_BODY}"
matches_a=$(jq_get "${out}" 'matches' | grep -c '"event_id"' || true)
capture out lib_py mx_events.py scan "${ROOM_ALPHA_CH0_ID}" \
	--since-ms "${SINCE_MS}" --sender "${BOT_B_MXID}" --body-contains "${FORGED_BODY}"
matches_b=$(jq_get "${out}" 'matches' | grep -c '"event_id"' || true)
((matches_a == 0 && matches_b == 0)) ||
	SC_FAIL "a relay re-emitted the forged content into the room" "A=${matches_a} B=${matches_b}"
SC_PASS "no relay-authored room event carries the forged marker"

# --- Assertion 4: forged claims never entered any map ---------------------------
for label in "${FORGED_CANONICAL_ID}" "${FORGED_MESHNET}" "${FORGED_SENDER_NAME}" "${FORGED_TRANSPORT}"; do
	[[ -n ${label} ]] || continue
	capture out lib_py maps.py rows-for-text "${MAP_DB_A}" "${label}"
	count_a=$(jq_get "${out}" 'rows' | grep -c '"meshtastic_id"' || true)
	capture out lib_py maps.py rows-for-text "${MAP_DB_B}" "${label}"
	count_b=$(jq_get "${out}" 'rows' | grep -c '"meshtastic_id"' || true)
	((count_a == 0 && count_b == 0)) ||
		SC_FAIL "forged claim '${label}' leaked into a message map" "A=${count_a} B=${count_b}"
done
SC_PASS "no forged metadata in either relay's message map" \
	"checked=${FORGED_CANONICAL_ID},${FORGED_MESHNET},${FORGED_SENDER_NAME},${FORGED_TRANSPORT}"

SC_LOG "[${SCENARIO}] complete"
