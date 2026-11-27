#!/usr/bin/env bash
# =============================================================================
# Scenario: untargeted-emote
#
# A radio message carrying a reply id that no map row can resolve (an emote
# with a packet id but no resolvable target) must relay as ORDINARY authored
# text on the peer mesh - never as an empty reaction and never dropped.
#
# Assertions (state/API only):
#   1. Relay B relays the unresolved-reply text to the room as an ordinary
#      m.text event from @relay-b whose envelope is event_kind=message.created
#      with NO m.relates_to on the event; the envelope's relation_info carries
#      the canonical "reply" hint that preserves the unresolvable target (a
#      contract variable - see the note at the assertion).
#   2. The event body is non-empty and carries the authored marker.
#   3. Relay A (peer mesh) relays that authored text onward to AlphaLab: A's
#      map gains a row keyed by B's event id (A's map row is written after the
#      confirmed radio send), proving the text reached the peer mesh as an
#      ordinary message.
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_rig
resolve_rooms

SCENARIO="untargeted-emote"
MARKER="EMOTE-UNTARGETED-$(SC_UID)"
UNRESOLVABLE_ID=$((4000000000 + RANDOM * 17))
SINCE_MS="$(date +%s%3N)"

# --- Act: radio text from the BETA PEER replying to a packet id in no map ------
capture out lib_py radio.py send-text "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	"${MARKER}" --reply-id "${UNRESOLVABLE_ID}"
((SC_STATUS == 0)) || SC_FAIL "radio injection with unresolved reply id failed" "${out}"
SC_PASS "injected BetaLab text with unresolved reply id" \
	"reply_id=${UNRESOLVABLE_ID} marker=${MARKER}"

# --- Assertion 1+2: ordinary authored message, not a reaction ------------------
# Contract scope: "ordinary authored text" = the Matrix CONTENT carries no
# relation (the --no-relation filter below) and the event is a plain wire
# m.text. What mmrelay writes into the medre.envelope v1 relation_info for
# an UNRESOLVABLE wire reply target is a contract variable (same framing as
# scenario-normal-reactions.sh): the current build deterministically
# preserves the unresolvable radio-native target as a canonical envelope
# hint - relation_info "reply" with an unresolved target - while the wire
# event stays relation-free. That shipped shape is pinned strictly here;
# an envelope-hint change moves this filter, not the wire-level contract.
capture event_json lib_py mx_events.py wait "${ROOM_BETA_CH0_ID}" \
	--sender "${BOT_B_MXID}" --body-contains "${MARKER}" --msgtype m.text \
	--require-envelope --envelope-kind message.created --relation-info reply \
	--no-relation --since-ms "${SINCE_MS}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "unresolved emote was not relayed as an ordinary authored message" "${event_json}"
EVENT_ID="$(jq_get "${event_json}" event.event_id | tr -d '"')"
BODY="$(jq_get "${event_json}" event.content.body | tr -d '"')"
[[ ${#BODY} -ge ${#MARKER} ]] ||
	SC_FAIL "authored body unexpectedly empty" "${event_json}"
SC_PASS "unresolved reply relayed as ordinary authored text (content relation-free, envelope reply hint, non-empty body)" \
	"event=${EVENT_ID} body_length=${#BODY}" $'\n'"${event_json}"

# --- Assertion 3: peer mesh received it as ordinary text ------------------------
capture out lib_py maps.py rows-for-event "${MAP_DB_A}" "${EVENT_ID}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "relay A did not relay the authored text to AlphaLab (no map row for B's event)" "${out}"
SC_PASS "authored text bridged onward to the peer mesh" "A_rows=${out}"
SC_LOG "[${SCENARIO}] complete"
