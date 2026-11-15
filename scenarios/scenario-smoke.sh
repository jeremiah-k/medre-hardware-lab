#!/usr/bin/env bash
# =============================================================================
# Scenario: smoke
#
# Radio-origin text from node A's PEER (AlphaLab) must reach #lab-alpha-ch0
# as a canonical relay-authored Matrix event, and the far mesh (BetaLab) must
# receive the authored body verbatim on its radio.
#
# Assertions (state/API only):
#   1. Room event from @relay-a in #lab-alpha-ch0 carrying the injected
#      marker, with a valid medre.envelope v1: source_adapter
#      mmrelay.meshtastic, source_channel "0", event_kind message.created,
#      source_transport_id a native node id (!hex), numeric source_native_ref.
#   2. Relay B's SQLite message map gains a row for that Matrix event whose
#      meshtastic_text carries the marker (B bridged it to Beta ch0).
#   3. Radio-side delivery on the far mesh: a sniffer attached to node B's
#      PEER (meshtasticd-b-peer) receives a text packet carrying the marker.
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_rig
resolve_rooms

SCENARIO="smoke"
MARKER="SMOKE-$(SC_UID)"
SINCE_MS="$(date +%s%3N)"

# Far-side sniffer is attached BEFORE the triggering action; its deadline
# covers the full Matrix-leg wait so it is still listening when relay B
# retransmits.
start_sniffer SNIFF "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" --expect-text "${MARKER}"
trap 'stop_sniffer "${SNIFF_PID}" "${SNIFF}"' EXIT

SC_LOG "[${SCENARIO}] injecting AlphaLab radio text from peer node: ${MARKER}"
capture RADIO_OUT lib_py radio.py send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${MARKER}"
if ((SC_STATUS != 0)); then
	SC_FAIL "radio injection on node A's peer failed" "${RADIO_OUT}"
fi
SC_PASS "radio text injected from the AlphaLab peer node" "${RADIO_OUT}"

# --- Assertion 1: canonical Matrix event -------------------------------------
capture EVENT_JSON lib_py mx_events.py wait "${ROOM_ALPHA_CH0_ID}" \
	--sender "${BOT_A_MXID}" --body-contains "${MARKER}" \
	--require-envelope --envelope-kind message.created --source-channel "0" \
	--native-ref-is-numeric --since-ms "${SINCE_MS}" \
	--timeout "${MATRIX_WAIT_SECONDS}"
if ((SC_STATUS != 0)); then
	SC_FAIL "no canonical relay-authored room event for the AlphaLab marker" "${EVENT_JSON}"
fi
EVENT_ID="$(jq_get "${EVENT_JSON}" event.event_id | tr -d '"')"
NATIVE_REF_MSG="$(jq_get "${EVENT_JSON}" event.content.medre.envelope.source_native_ref.native_message_id | tr -d '"')"
SOURCE_TRANSPORT="$(jq_get "${EVENT_JSON}" event.content.medre.envelope.source_transport_id | tr -d '"')"
SOURCE_ADAPTER="$(jq_get "${EVENT_JSON}" event.content.medre.envelope.source_adapter | tr -d '"')"
SCHEMA_VERSION="$(jq_get "${EVENT_JSON}" event.content.medre.envelope.schema_version)"

EVIDENCE=$(printf 'event_id=%s\nsource_adapter=%s schema_version=%s\nsource_transport_id=%s source_native_ref.native_message_id=%s' \
	"${EVENT_ID}" "${SOURCE_ADAPTER}" "${SCHEMA_VERSION}" "${SOURCE_TRANSPORT}" "${NATIVE_REF_MSG}")

[[ ${SOURCE_ADAPTER} == "mmrelay.meshtastic" ]] ||
	SC_FAIL "envelope source_adapter mismatch" "${EVIDENCE}"
[[ ${SCHEMA_VERSION} == "1" ]] ||
	SC_FAIL "envelope schema_version must be the integer 1" "${EVIDENCE}"
[[ ${SOURCE_TRANSPORT} == \!* ]] ||
	SC_FAIL "envelope source_transport_id must be a native node id" "${EVIDENCE}"
[[ -n ${NATIVE_REF_MSG} && ${NATIVE_REF_MSG} != "null" ]] ||
	SC_FAIL "envelope source_native_ref.native_message_id missing" "${EVIDENCE}"

BODY_HAS_MARKER=$(jq_get "${EVENT_JSON}" 'event.content.body' | grep -c "${MARKER}" || true)
[[ ${BODY_HAS_MARKER} -ge 1 ]] ||
	SC_FAIL "authored body not carried verbatim in the room event" "${EVENT_JSON}"
SC_PASS "canonical envelope v1 present on the Matrix event" "${EVIDENCE}"

# --- Assertion 3: far-peer radio delivery (sniff while the flow is live) ------
if wait_until "${SNIFF_WAIT_SECONDS}" grep -q '"match"' "${SNIFF}"; then
	SNIFF_MATCH=$(grep -o '"match":.*' "${SNIFF}" | head -1)
	SC_PASS "far mesh radio delivery observed on node B's peer" \
		"sniffer=${NODE_B_PEER_HOST}:${NODE_B_PEER_PORT} ${SNIFF_MATCH}"
else
	SC_FAIL "sniffer on node B's peer never received the authored body" \
		"sniff log: $(cat "${SNIFF}")"
fi

# --- Assertion 2: relay B bridged the event to Beta ch0 ------------------------
capture MAP_JSON lib_py maps.py rows-for-event "${MAP_DB_B}" "${EVENT_ID}" --wait "${RADIO_WAIT_SECONDS}"
if ((SC_STATUS != 0)); then
	SC_FAIL "relay B map has no row for the AlphaLab-origin event (Beta radio never received the authored body)" "${MAP_JSON}"
fi
MAP_TEXT_HAS=$(printf '%s' "${MAP_JSON}" | grep -c "${MARKER}" || true)
[[ ${MAP_TEXT_HAS} -ge 1 ]] ||
	SC_FAIL "relay B map row text does not carry the authored marker" "${MAP_JSON}"
SC_PASS "Beta's radio bridge state written (relay B map row)" \
	"db=${MAP_DB_B} event_id=${EVENT_ID} rows=${MAP_JSON}"

SC_LOG "[${SCENARIO}] complete"
