#!/usr/bin/env bash
# =============================================================================
# Probe (r26 qualification): matrix-side DELETE (redaction) of a bridged
# message
#
# r26 behaviors under test (medre core #504 + matrix adapter #56):
#   * Delete payload bodies remain suppressed (core #506 fragment: "Reaction
#     and delete payloads remain suppressed even when native fields represent
#     every relation"; codec _payload for MESSAGE_DELETED = {"reason"} only).
#   * Native mutations require a matching event kind and relation
#     (message.deleted + delete relation = matched intent here).
#   * Radio-c and lxmf0 declare deletes="unsupported" -> designed SKIP:
#     canonical state holds the deleted event (reason-only payload, delete
#     relation), NO radio packet, NO lxmf delivery, clean room.
#
# PASS = all of: canonical message.deleted row (payload WITHOUT body), delete
# relation row, zero outbound radio/lxmf refs, zero medre-authored room
# events since the redaction, clean MeshC absence window (sniffer armed
# BEFORE the redaction, no match, transport not poisoned).
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

SCENARIO="probe-r26-matrix-delete"
ROOM_ID="${ROOM_BRIDGE_CH0_ID}"

radio_ingest_ready "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${MEDRE_NORMAL_BOT_MXID}" "${ROOM_ID}" "medre-normal"

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

SEED_MARKER="R26D1-$(SC_UID)"
SINCE_SEED="$(date +%s%3N)"
capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" "${SEED_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "seed send failed" "${out}"
SEED_EVENT="$(jq_get "${out}" event_id | tr -d '"')"

PACKET_C="$(medre_outbound_packet "${SEED_EVENT}")"
[[ -n ${PACKET_C} ]] || SC_FAIL "medre did not re-air the seed onto MeshC (probe precondition)" \
	"seed_event=${SEED_EVENT} db=${MEDRE_NORMAL_DB}"
SC_PASS "seed bridged end to end (probe precondition)" \
	"seed_event=${SEED_EVENT} medre_packet=${PACKET_C}"

start_sniffer_checked SNIFF_C "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "${SEED_MARKER}"
trap 'stop_sniffer "${SNIFF_C_PID:-}" "${SNIFF_C:-}"' EXIT

SINCE_REDACT="$(date +%s%3N)"
capture out lib_py mx_probe.py redact "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" \
	"${SEED_EVENT}" "probe-r26 delete"
((SC_STATUS == 0)) || SC_FAIL "alice redaction send failed" "${out}"
REDACT_EVENT="$(jq_get "${out}" event_id | tr -d '"')"
SC_PASS "alice redaction (m.room.redaction) sent" \
	"redact_event=${REDACT_EVENT} target=${SEED_EVENT}"

capture redact_state medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${REDACT_EVENT}" \
	--wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "medre stored no canonical event for the matrix redaction (message.deleted classification failed)" \
		"redact_event=${REDACT_EVENT} db=${MEDRE_NORMAL_DB}" $'\n'"state output: ${redact_state}"
verdict="$(printf '%s' "${redact_state}" | tools_exec python3 -c '
import json, sys
data = json.load(sys.stdin)
events = data.get("events", [])
problems = []
if not events:
    problems.append("no canonical event for the redaction event id")
for event in events:
    kind = event.get("event_kind")
    payload = event.get("payload") or {}
    relations = event.get("relations") or []
    body_content = event.get("body_content") or {}
    if kind != "message.deleted":
        problems.append(f"event_kind={kind!r} != message.deleted")
    if "body" in payload or body_content.get("body"):
        problems.append(f"delete event carries body content (payload keys={sorted(payload)}, body_content={body_content}): delete payloads must be suppressed (core #506)")
    delete_rels = [r for r in relations if r.get("relation_type") == "delete"]
    if len(delete_rels) != 1:
        problems.append(f"delete relation rows={len(delete_rels)} != 1")
    outbound = [
        ref for ref in event.get("native_refs", [])
        if ref.get("direction") == "outbound"
    ]
    if outbound:
        problems.append(f"outbound native refs exist: {outbound!r} (radio/lxmf declare deletes unsupported: designed SKIP)")
print(json.dumps({"problems": problems, "events": events}))
')"
printf '%s' "${verdict}" | grep -q '"problems": \[\]' ||
	SC_FAIL "canonical delete-state contract violated (r26: payload suppression, kind/relation matching, capability skip)" \
		"${verdict}"
SC_PASS "canonical message.deleted with reason-only payload (body suppressed) + delete relation; no outbound radio/lxmf refs" \
	"redact_event=${REDACT_EVENT}"

capture room_scan lib_py mx_events.py scan "${ROOM_ID}" --since-ms "${SINCE_REDACT}" \
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
[[ ${medre_room_events} == "0" ]] ||
	SC_FAIL "medre authored room events after the redaction (malformed mutation echo)" \
		"scan since=${SINCE_REDACT}: ${room_scan}"
SC_PASS "room clean: no medre-authored events after the redaction (no native mutation echo)" \
	"since_ms=${SINCE_REDACT}"

# Wire absence: any match carrying the seed text on a packet OTHER than the
# known precondition re-air (PACKET_C) would mean the redaction leaked to the
# radio. The seed's own over-air copy may legitimately appear in the capture
# (its transmission can land just after the sniffer arms - the outbound ref
# is written at plan time), so it is excluded by packet id.
leak="$(grep -o '{"match":.*}' "${SNIFF_C}" 2>/dev/null | tools_exec python3 -c '
import json, sys
leaks = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        view = (json.loads(line).get("match") or {})
    except Exception:
        continue
    pid = str(view.get("packet_id") or "")
    if pid and pid != sys.argv[1]:
        leaks.append(view)
print(json.dumps(leaks))
' "${PACKET_C}")"
[[ ${leak} == "[]" ]] ||
	SC_FAIL "the matrix redaction aired on MeshC (radio declares deletes unsupported: designed skip violated)" \
		"non-precondition matches: ${leak}"
SC_PASS "MeshC radio silent across the redaction window (designed capability skip, absence proven with armed sniffer)" \
	"window_ms=$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))000"

stop_sniffer "${SNIFF_C_PID}" "${SNIFF_C}"
trap - EXIT

SC_PASS "probe-r26-matrix-delete complete"
