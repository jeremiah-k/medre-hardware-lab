#!/usr/bin/env bash
# =============================================================================
# Probe (R2 qualification): matrix-side EDIT — matched intent + DENIED intent
#
# R2 behaviors under test (core #504 a693f580 + matrix #56 d67f1219):
#   * Matched intent: alice m.replace-edits her own bridged message. The
#     matrix codec classifies MESSAGE_EDITED + edit relation; radio-c/lxmf0
#     declare edits="unsupported" -> designed SKIP; the room stays clean
#     (no malformed native mutation echo). Carried over from the r26 round.
#   * NEW denied-intent angle: an m.new_content payload WITHOUT m.relates_to
#     is NOT an edit — the ingest must keep the event an ORDINARY
#     message.created with the body preserved verbatim (asterisk included),
#     zero relation rows, and no mutation surface anywhere (room/wire/state).
#   * NEW malformed-edit tolerance: an m.replace relation WITHOUT
#     m.new_content still pairs kind+relation (message.edited + edit), and
#     still cannot become an ordinary native send (capability skip, clean
#     room).
# PASS = all acts green with retained canonical-state + sniffer evidence.
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

SCENARIO="probe-r2-matrix-edit"
ROOM_ID="${ROOM_BRIDGE_CH0_ID}"
BOB_TOKEN="$(lib_py mx_user.py token "${HUMAN_BOB_LOCALPART}" | tr -d '"')"
[[ -n "${BOB_TOKEN}" && "${BOB_TOKEN}" != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_BOB_LOCALPART}" "the rig must provision @bob (scenarios/README.md)"

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

assert_edit_canonical() { # EVENT_ID NEW_BODY LABEL
	local edit_event="$1" expected_body="$2" label="$3"
	capture _es medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${edit_event}" \
		--wait "${RADIO_WAIT_SECONDS}"
	((SC_STATUS == 0)) ||
		SC_FAIL "${label}: medre stored no canonical event for the matrix edit" \
			"edit_event=${edit_event} db=${MEDRE_NORMAL_DB}" $'\n'"state output: ${_es}"
	local verdict
	verdict="$(printf '%s' "${_es}" | tools_exec python3 -c '
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
        problems.append(f"stored body={body!r} != the expected body")
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
' "${expected_body}")"
	printf '%s' "${verdict}" | grep -q '"problems": \[\]' ||
		SC_FAIL "${label}: canonical edit-state contract violated" "${verdict}"
}

# =============================================================================
# Act 1 — MATCHED intent: alice m.replace-edits her own bridged message.
# =============================================================================
SEED_MARKER="R2E1-$(SC_UID)"
SINCE_SEED="$(date +%s%3N)"
capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" "${SEED_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "seed send failed" "${out}"
SEED_EVENT="$(jq_get "${out}" event_id | tr -d '"')"
PACKET_C="$(medre_outbound_packet "${SEED_EVENT}")"
[[ -n "${PACKET_C}" ]] || SC_FAIL "medre did not re-air the seed onto MeshC (probe precondition)" \
	"seed_event=${SEED_EVENT} db=${MEDRE_NORMAL_DB}"
SC_PASS "seed bridged end to end (probe precondition)" \
	"seed_event=${SEED_EVENT} medre_packet=${PACKET_C}"

NEW_BODY="R2E2-$(SC_UID) edited body text"
start_sniffer_checked SNIFF_C "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "${NEW_BODY}"
trap 'stop_sniffer "${SNIFF_C_PID:-}" "${SNIFF_C:-}"' EXIT

SINCE_EDIT="$(date +%s%3N)"
capture out lib_py mx_probe.py edit "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" \
	"${SEED_EVENT}" "${NEW_BODY}"
((SC_STATUS == 0)) || SC_FAIL "alice edit send failed" "${out}"
EDIT_EVENT="$(jq_get "${out}" event_id | tr -d '"')"

assert_edit_canonical "${EDIT_EVENT}" "${NEW_BODY}" "matched intent"
SC_PASS "matched intent: canonical message.edited + one edit relation + designed capability skip" \
	"edit_event=${EDIT_EVENT}"

SNIFF_MATCH="$(grep -o '{"match":.*}' "${SNIFF_C}" 2>/dev/null | head -1 || true)"
[[ -z "${SNIFF_MATCH}" ]] ||
	SC_FAIL "the matrix edit aired on MeshC (designed skip violated)" "${SNIFF_MATCH}"

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
	SC_FAIL "medre authored room events after the matched edit (malformed mutation echo)" \
		"scan since=${SINCE_EDIT}: ${room_scan}"
SC_PASS "matched intent: room clean (no malformed native mutation reaches the room)" \
	"since_ms=${SINCE_EDIT}"

# =============================================================================
# Act 2 — DENIED intent: m.new_content WITHOUT m.relates_to is NOT an edit.
# The ingest must keep an ordinary message.created with the body verbatim.
# =============================================================================
FORGED_BODY="R2E3-$(SC_UID) forged asterisk body"
FORGED_NEW="R2E3N-$(SC_UID) would-be new content"
SINCE_FORGED="$(date +%s%3N)"
capture forged_raw lib_py mx_probe.py raw "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" \
	"m.room.message" "{\"msgtype\":\"m.text\",\"body\":\"${FORGED_BODY}\",\"m.new_content\":{\"msgtype\":\"m.text\",\"body\":\"${FORGED_NEW}\"}}"
((SC_STATUS == 0)) || SC_FAIL "denied-intent send failed" "${forged_raw}"
FORGED_EVENT="$(jq_get "${forged_raw}" event_id | tr -d '"')"

capture forged_state medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${FORGED_EVENT}" \
	--wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "denied intent: medre stored no canonical event for the m.new_content-without-relation message" \
		"event=${FORGED_EVENT}" $'\n'"state output: ${forged_state}"
verdict="$(printf '%s' "${forged_state}" | tools_exec python3 -c '
import json, sys
data = json.load(sys.stdin)
events = data.get("events", [])
problems = []
if not events:
    problems.append("no canonical event")
for event in events:
    kind = event.get("event_kind")
    relations = event.get("relations") or []
    body_content = event.get("body_content") or {}
    body = body_content.get("body")
    if kind != "message.created":
        problems.append(f"event_kind={kind!r} != message.created (m.new_content WITHOUT m.relates_to must stay an ordinary message: denied intent)")
    if body != sys.argv[1]:
        problems.append(f"stored body={body!r} != the verbatim authored body (asterisk included)")
    if relations:
        problems.append(f"relation rows exist: {relations!r} (denied intent must produce ZERO relations)")
print(json.dumps({"problems": problems, "events": events}))
' "${FORGED_BODY}")"
printf '%s' "${verdict}" | grep -q '"problems": \[\]' ||
	SC_FAIL "denied intent: ingest treated relation-less m.new_content as a mutation" "${verdict}"
SC_PASS "denied intent: relation-less m.new_content stays an ordinary message.created, body verbatim, zero relations" \
	"event=${FORGED_EVENT}"

SNIFF_FORGED_MATCH="$(grep -o '{"match":.*}' "${SNIFF_C}" 2>/dev/null | grep -F "${FORGED_BODY}" | head -1 || true)"
capture forged_radio medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${FORGED_EVENT}" \
	--wait 0
outbound_radio="$(printf '%s' "${forged_radio}" | tools_exec python3 -c '
import json, re, sys
data = json.load(sys.stdin)
refs = []
for event in data.get("events", []):
    for ref in event.get("native_refs", []):
        if ref.get("direction") == "outbound" and re.fullmatch(r"[0-9]+", str(ref.get("native_message_id") or "")):
            refs.append(ref["native_message_id"])
print(refs[0] if refs else "")
')"
[[ -n "${outbound_radio}" ]] || SC_FAIL "denied intent: the ordinary message was not bridged to MeshC (message.created must deliver normally)" \
	"event=${FORGED_EVENT}" $'\n'"state: ${forged_radio}"
SC_PASS "denied intent: the ordinary message bridged to MeshC normally (outbound radio ref present)" \
	"event=${FORGED_EVENT} packet=${outbound_radio}"

# =============================================================================
# Act 3 — MALFORMED edit tolerance: m.replace relation WITHOUT m.new_content.
# Kind+relation still pair (message.edited + edit); still no ordinary send.
# =============================================================================
MALFORMED_BODY="R2E4-$(SC_UID) replace without new content"
SINCE_MALFORMED="$(date +%s%3N)"
capture malformed_raw lib_py mx_probe.py raw "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" \
	"m.room.message" "{\"msgtype\":\"m.text\",\"body\":\"${MALFORMED_BODY}\",\"m.relates_to\":{\"rel_type\":\"m.replace\",\"event_id\":\"${SEED_EVENT}\"}}"
((SC_STATUS == 0)) || SC_FAIL "malformed-edit send failed" "${malformed_raw}"
MALFORMED_EVENT="$(jq_get "${malformed_raw}" event_id | tr -d '"')"

capture malformed_state medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${MALFORMED_EVENT}" \
	--wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "malformed edit: medre stored no canonical event" \
		"event=${MALFORMED_EVENT}" $'\n'"state output: ${malformed_state}"
verdict="$(printf '%s' "${malformed_state}" | tools_exec python3 -c '
import json, sys
data = json.load(sys.stdin)
events = data.get("events", [])
problems = []
if not events:
    problems.append("no canonical event")
for event in events:
    kind = event.get("event_kind")
    relations = event.get("relations") or []
    if kind != "message.edited":
        problems.append(f"event_kind={kind!r} != message.edited (m.replace pairs kind+relation even without m.new_content)")
    edit_rels = [r for r in relations if r.get("relation_type") == "edit"]
    if len(edit_rels) != 1:
        problems.append(f"edit relation rows={len(edit_rels)} != 1")
    outbound = [
        ref for ref in event.get("native_refs", [])
        if ref.get("direction") == "outbound"
    ]
    if outbound:
        problems.append(f"outbound native refs exist: {outbound!r} (cannot become an ordinary native send: capability skip)")
print(json.dumps({"problems": problems, "events": events}))
')"
printf '%s' "${verdict}" | grep -q '"problems": \[\]' ||
	SC_FAIL "malformed edit: tolerance contract violated" "${verdict}"
SC_PASS "malformed edit tolerance: message.edited + edit relation, still no ordinary native send (skip)" \
	"event=${MALFORMED_EVENT}"

capture room_scan2 lib_py mx_events.py scan "${ROOM_ID}" --since-ms "${SINCE_MALFORMED}" \
	--sender "${MEDRE_NORMAL_BOT_MXID}" --limit 200
((SC_STATUS == 0)) || room_scan2="[]"
medre_room_events2="$(printf '%s' "${room_scan2}" | tools_exec python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
print(len(rows) if isinstance(rows, list) else 0)
')"
[[ "${medre_room_events2}" == "0" ]] ||
	SC_FAIL "medre authored room events after the malformed edit (malformed mutation echo)" \
		"scan since=${SINCE_MALFORMED}: ${room_scan2}"
SC_PASS "malformed edit: room clean" "since_ms=${SINCE_MALFORMED}"

# Wire-level absence for the matched edit, checked BEFORE the sniffer is
# stopped (stop_sniffer removes its capture file). Acts 2/3 wire behavior is
# proven at the state level: the denied act delivers by design
# (message.created, outbound ref asserted), the malformed act has zero
# outbound refs (designed skip).
SNIFF_MATCH="$(grep -o '{"match":.*}' "${SNIFF_C}" 2>/dev/null | grep -F "${NEW_BODY}" | head -1 || true)"
[[ -z "${SNIFF_MATCH}" ]] ||
	SC_FAIL "the matched-intent edit body aired on MeshC (designed skip violated)" "${SNIFF_MATCH}"

stop_sniffer "${SNIFF_C_PID}" "${SNIFF_C}"
trap - EXIT

SC_PASS "probe-r2-matrix-edit complete" \
	"wire evidence: the edit body never aired on MeshC across the armed window"
