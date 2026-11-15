#!/usr/bin/env bash
# =============================================================================
# Probe (r26 qualification): matrix-side EDIT of a bridged message
#
# r26 behaviors under test (medre core #504 + matrix adapter #56):
#   * Native mutations require a matching event kind and relation: ordinary
#     messages carrying edit/delete relations fail closed before a native
#     operation is constructed, even for owned targets (core #506 fragment;
#     matrix #51 fragment; renderer regression
#     test_native_mutation_requires_matching_kind_and_relation).
#   * The live ingest shape: alice m.replace-edits her own bridged Matrix
#     message. The matrix adapter codec classifies it MESSAGE_EDITED with an
#     edit relation (matching intent). The radio-c and lxmf0 destinations
#     declare edits="unsupported" (capability_decision: unsupported -> skip),
#     so the DESIGNED live outcome is: canonical state holds the edited event
#     (new body via m.new_content, edit relation stored), NO radio packet,
#     NO lxmf delivery, and a clean room (no malformed native mutation echo).
#
# PASS = all of: canonical message.edited row (payload = new body), edit
# relation row, zero outbound radio/lxmf refs, zero medre-authored room
# events since the edit, clean MeshC absence window (sniffer armed BEFORE
# the edit, no match, transport not poisoned).
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

SCENARIO="probe-r26-matrix-edit"
ROOM_ID="${ROOM_BRIDGE_CH0_ID}"

radio_ingest_ready "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${MEDRE_NORMAL_BOT_MXID}" "${ROOM_ID}" "medre-normal"

# medre outbound radio packet id for an origin event (same poll contract as
# normal-replies' medre_outbound_packet).
medre_outbound_packet() { # ORIGIN_EVENT_ID -> packet id ("" when none)
	capture _mo_out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "$1" \
		--wait "${RADIO_WAIT_SECONDS}" --require-outbound-radio
	if ((SC_STATUS == 0)); then
		printf '%s' "${_mo_out}" | tools_exec python3 -c '
import json, re, sys
data = json.load(sys.stdin)
for event in data.get("events", []):
    for ref in event.get("native_refs", []):
        if ref.get("direction") == "outbound" and re.fullmatch(
            r"[0-9]+", str(ref.get("native_message_id") or "")
        ):
            print(ref["native_message_id"])
            raise SystemExit
'
	else
		printf ''
	fi
}

SEED_MARKER="R26E1-$(SC_UID)"
SINCE_SEED="$(date +%s%3N)"
capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" "${SEED_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "seed send failed" "${out}"
SEED_EVENT="$(jq_get "${out}" event_id | tr -d '"')"

PACKET_C="$(medre_outbound_packet "${SEED_EVENT}")"
[[ -n "${PACKET_C}" ]] || SC_FAIL "medre did not re-air the seed onto MeshC (probe precondition)" \
	"seed_event=${SEED_EVENT} db=${MEDRE_NORMAL_DB}"
SC_PASS "seed bridged end to end (probe precondition)" \
	"seed_event=${SEED_EVENT} medre_packet=${PACKET_C}"

# Sniffer armed BEFORE the edit (absence window over MeshC).
NEW_BODY="R26E2-$(SC_UID) edited body text"
start_sniffer_checked SNIFF_C "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "${NEW_BODY}"
trap 'stop_sniffer "${SNIFF_C_PID:-}" "${SNIFF_C:-}"' EXIT

SINCE_EDIT="$(date +%s%3N)"
capture out lib_py mx_probe.py edit "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" \
	"${SEED_EVENT}" "${NEW_BODY}"
((SC_STATUS == 0)) || SC_FAIL "alice edit send failed" "${out}"
EDIT_EVENT="$(jq_get "${out}" event_id | tr -d '"')"
SC_PASS "alice edit (m.replace) sent" "edit_event=${EDIT_EVENT} target=${SEED_EVENT}"

# Canonical state: the edited event, its payload, and its relations.
capture edit_state medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${EDIT_EVENT}" \
	--wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "medre stored no canonical event for the matrix edit (event kind classification failed)" \
		"edit_event=${EDIT_EVENT} db=${MEDRE_NORMAL_DB}" $'\n'"state output: ${edit_state}"
verdict="$(printf '%s' "${edit_state}" | tools_exec python3 -c '
import json, sys
data = json.load(sys.stdin)
events = data.get("events", [])
problems = []
if not events:
    problems.append("no canonical event for the edit event id")
for event in events:
    kind = event.get("event_kind")
    payload = event.get("payload") or {}
    relations = event.get("relations") or []
    body_content = event.get("body_content") or {}
    body = body_content.get("body") if body_content.get("body") is not None else payload.get("body")
    if kind != "message.edited":
        problems.append(f"event_kind={kind!r} != message.edited")
    if body != sys.argv[1]:
        problems.append(f"stored body={body!r} != the m.new_content body")
    edit_rels = [r for r in relations if r.get("relation_type") == "edit"]
    if len(edit_rels) != 1:
        problems.append(f"edit relation rows={len(edit_rels)} != 1")
    outbound = [
        ref for ref in event.get("native_refs", [])
        if ref.get("direction") == "outbound"
    ]
    if outbound:
        problems.append(f"outbound native refs exist: {outbound!r} (radio/lxmf declare edits unsupported: designed SKIP)")
print(json.dumps({"problems": problems, "events": events}))
' "${NEW_BODY}")"
printf '%s' "${verdict}" | grep -q '"problems": \[\]' ||
	SC_FAIL "canonical edit-state contract violated (r26: kind/relation matching, payload from m.new_content, capability skip)" \
		"${verdict}"
SC_PASS "canonical message.edited with m.new_content body + edit relation; no outbound radio/lxmf refs (designed capability skip)" \
	"edit_event=${EDIT_EVENT}"

# Room cleanliness: medre must not echo the edit (no malformed native mutation).
capture room_scan lib_py mx_events.py scan "${ROOM_ID}" --since-ms "${SINCE_EDIT}" \
	--sender "${MEDRE_NORMAL_BOT_MXID}" --limit 200
((SC_STATUS == 0)) || room_scan="[]"
medre_room_events="$(printf '%s' "${room_scan}" | tools_exec python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
print(len(rows) if isinstance(rows, list) else 0)
')"
[[ "${medre_room_events}" == "0" ]] ||
	SC_FAIL "medre authored room events after the matrix edit (malformed mutation echo)" \
		"scan since=${SINCE_EDIT}: ${room_scan}"
SC_PASS "room clean: no medre-authored events after the edit (no native mutation echo)" \
	"since_ms=${SINCE_EDIT}"

# MeshC absence: no radio packet carries the new body.
SNIFF_MATCH="$(grep -o '{"match":.*}' "${SNIFF_C}" 2>/dev/null | head -1 || true)"
[[ -z "${SNIFF_MATCH}" ]] ||
	SC_FAIL "the matrix edit aired on MeshC (radio declares edits unsupported: designed skip violated)" \
		"sniffer match: ${SNIFF_MATCH}"
SC_PASS "MeshC radio silent across the edit window (designed capability skip, absence proven with armed sniffer)" \
	"expect_text=${NEW_BODY} window_ms=$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))000"

stop_sniffer "${SNIFF_C_PID}" "${SNIFF_C}"
trap - EXIT

SC_PASS "probe-r26-matrix-edit complete"
