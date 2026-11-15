#!/usr/bin/env bash
# =============================================================================
# Probe (r26 qualification): authored Matrix message carrying reaction context
#
# r26 behaviors under test (medre core #504 + matrix adapter #56 + meshtastic
# adapter #49):
#   * Authored content + a reaction relation on one event: the authored body
#     is preserved in canonical storage exactly once, and the reaction is
#     represented exactly once (core #506: "Authored messages carrying
#     reaction context retain their text" / matrix #51: "Authored messages
#     carrying reaction context retain their body ... without changing the
#     stored relation tuple").
#   * Represented-relation body suppression ON THE WIRE: a reaction event's
#     payload body is not additional message content (core #506: "Reaction
#     and delete payloads remain suppressed even when native fields represent
#     every relation"), so the MeshC wire text is the DESCRIBED reaction
#     (meshtastic renderer describe_reaction: 'reacted KEY to "<preview>"' or
#     the honest 'an earlier message' wording) and must NOT also contain the
#     authored body, and must NOT be a native key-only tapback (meshtastic
#     #44 fragment: "Only reaction-only events can become native key-only
#     tapbacks"; native tapbacks additionally require a Meshtastic source).
#   * The reaction preview comes from core relation enrichment; with the lab
#     content_ceiling enabled (core #504 ceiling-governed reply-quote grant) a
#     resolved target QUOTES the seed text; without the grant it degrades
#     honestly to 'an earlier message' (never an empty quote). Both shapes are
#     designed; the probe records which fired.
#   * lxmf0 declares reactions="unsupported" -> designed SKIP (no outbound
#     lxmf ref).
#
# PASS = all of: canonical message.reacted row with the authored body stored
# once, one reaction relation row with the exact key, MeshC wire text is the
# described reaction (not a bare-key tapback, authored body absent, key once,
# within byte budget), room clean, no outbound lxmf ref.
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

SCENARIO="probe-r26-authored-reaction"
ROOM_ID="${ROOM_BRIDGE_CH0_ID}"

radio_ingest_ready "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${MEDRE_NORMAL_BOT_MXID}" "${ROOM_ID}" "medre-normal"

# (1) Seed: a c-peer radio text bridged by medre (its Matrix event is the
# reaction target; the seed's own body is the quote-preview candidate).
SEED_MARKER="R26R1-$(SC_UID)"
SINCE_SEED="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" "${SEED_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "c-peer seed injection failed" "${out}"
SEED_PACKET="$(jq_get "${out}" packet_id | tr -d '"')"

capture seed_json lib_py mx_events.py wait "${ROOM_ID}" \
	--sender "${MEDRE_NORMAL_BOT_MXID}" --body-contains "${SEED_MARKER}" \
	--require-envelope --envelope-kind message.created \
	--since-ms "${SINCE_SEED}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "no medre canonical event for the MeshC seed (probe precondition)" \
	"seed_packet=${SEED_PACKET}" $'\n'"${seed_json}"
MEDRE_SEED_EVENT="$(jq_get "${seed_json}" event.event_id | tr -d '"')"
SC_PASS "seed bridged: medre canonical event is the reaction target" \
	"seed_packet=${SEED_PACKET} medre_event=${MEDRE_SEED_EVENT}"

# (2) Sniffer armed BEFORE the authored reaction send.
AUTHORED_BODY="R26R2-$(SC_UID) please check this"
start_sniffer_checked SNIFF_C "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "reacted"
trap 'stop_sniffer "${SNIFF_C_PID:-}" "${SNIFF_C:-}"' EXIT

SINCE_REACT="$(date +%s%3N)"
capture out lib_py mx_probe.py authored-react "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" \
	"${MEDRE_SEED_EVENT}" "🦄" "${AUTHORED_BODY}"
((SC_STATUS == 0)) || SC_FAIL "authored reaction-context send failed" "${out}"
REACT_EVENT="$(jq_get "${out}" event_id | tr -d '"')"
SC_PASS "authored message with annotation relation sent" \
	"react_event=${REACT_EVENT} target=${MEDRE_SEED_EVENT}"

# (3) Canonical state: kind message.reacted, authored body stored once,
# exactly one reaction relation with the exact key.
capture react_state medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${REACT_EVENT}" \
	--wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "medre stored no canonical event for the authored reaction-context message" \
		"react_event=${REACT_EVENT} db=${MEDRE_NORMAL_DB}" $'\n'"state output: ${react_state}"
verdict="$(printf '%s' "${react_state}" | tools_exec python3 -c '
import json, sys
data = json.load(sys.stdin)
events = data.get("events", [])
problems = []
body_hits = 0
if not events:
    problems.append("no canonical event for the authored reaction event id")
for event in events:
    kind = event.get("event_kind")
    payload = event.get("payload") or {}
    relations = event.get("relations") or []
    body_content = event.get("body_content") or {}
    if kind != "message.reacted":
        problems.append(f"event_kind={kind!r} != message.reacted (matrix codec classifies annotation relations)")
    body = body_content.get("body") if body_content.get("body") is not None else payload.get("body")
    if body != sys.argv[1]:
        problems.append(f"stored body={body!r} != the authored body (authored text must be preserved in storage)")
    body_hits = sum(1 for v in body_content.values() if sys.argv[1] in str(v))
    if body_hits != 1:
        problems.append(f"authored body appears {body_hits}x in body_content, expected exactly once")
    reaction_rels = [r for r in relations if r.get("relation_type") == "reaction"]
    if len(reaction_rels) != 1:
        problems.append(f"reaction relation rows={len(reaction_rels)} != 1 (reaction context must not duplicate)")
    else:
        rel_key = reaction_rels[0].get("key")
        if rel_key != sys.argv[2]:
            problems.append(f"relation key={rel_key!r} != exact canonical key")
    lxmf_out = [
        ref for ref in event.get("native_refs", [])
        if ref.get("direction") == "outbound" and ref.get("adapter") == "lxmf0"
    ]
    if lxmf_out:
        problems.append(f"outbound lxmf0 refs exist: {lxmf_out!r} (lxmf declares reactions unsupported: designed SKIP)")
print(json.dumps({"problems": problems, "events": events}))
' "${AUTHORED_BODY}" "🦄")"
printf '%s' "${verdict}" | grep -q '"problems": \[\]' ||
	SC_FAIL "canonical authored-reaction contract violated (r26: body preserved once, reaction context not duplicated)" \
		"${verdict}"
SC_PASS "canonical message.reacted: authored body stored exactly once + exactly one reaction relation with the exact key" \
	"react_event=${REACT_EVENT}"

# (4) MeshC wire: the DESCRIBED reaction (not a native tapback, no authored
# body duplication, key exactly once, within the byte budget).
wait_until "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" grep -q '"match"' "${SNIFF_C}" ||
	SC_FAIL "no radio packet captured for the authored reaction-context event (meshtastic reactions=native: a described reaction MUST air)" \
		"sniffer log: $(cat "${SNIFF_C}")"
SNIFF_MATCH="$(grep -o '{"match":.*}' "${SNIFF_C}" | head -1)"
wire_verdict="$(printf '%s' "${SNIFF_MATCH}" | tools_exec python3 -c '
import json, sys
payload = json.load(sys.stdin)
view = payload.get("match") or {}
text = str(view.get("text") or "")
problems = []
if text.strip() == sys.argv[1]:
    problems.append("radio text is the BARE KEY: a native key-only tapback (forbidden for matrix-source events; meshtastic #44)")
if sys.argv[2] in text:
    problems.append("authored body leaked onto the radio wire (reaction payload body must be suppressed; core #506)")
if text.count(sys.argv[1]) != 1:
    problems.append(f"reaction key must appear EXACTLY once in radio text, got {text.count(sys.argv[1])}")
if "reacted" not in text or sys.argv[1] not in text:
    problems.append(f"radio text is not the described reaction: {text!r}")
n = len(text.encode("utf-8"))
if n > 227:
    problems.append(f"{n} UTF-8 bytes exceeds the radio limit 227")
print(json.dumps({"problems": problems, "text": text, "view": view}))
' "🦄" "${AUTHORED_BODY}")"
printf '%s' "${wire_verdict}" | grep -q '"problems": \[\]' ||
	SC_FAIL "MeshC wire contract violated (described reaction shape)" "${wire_verdict}"
wire_text="$(jq_get "${wire_verdict}" text | tr -d '"')"
if printf '%s' "${wire_text}" | grep -q "${SEED_MARKER}"; then
	PREVIEW_SHAPE="quoted seed text (ceiling-governed reply-quote grant fired, core #504)"
elif printf '%s' "${wire_text}" | grep -q "an earlier message"; then
	PREVIEW_SHAPE="honest degradation (no grant borrow; core #504 fallback wording)"
else
	PREVIEW_SHAPE="UNEXPECTED preview shape"
fi
SC_PASS "MeshC wire: described m.emote reaction, authored body suppressed, no native tapback, key once, within budget" \
	"wire_text=${wire_text}" $'\n'"preview=${PREVIEW_SHAPE}"

# (5) Room cleanliness: matrix-origin events never loop back into the room.
capture room_scan lib_py mx_events.py scan "${ROOM_ID}" --since-ms "${SINCE_REACT}" \
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
	SC_FAIL "medre authored room events for a matrix-origin reaction (echo/loop)" \
		"scan since=${SINCE_REACT}: ${room_scan}"
SC_PASS "room clean: no medre-authored events for the matrix-origin reaction" \
	"since_ms=${SINCE_REACT}"

stop_sniffer "${SNIFF_C_PID}" "${SNIFF_C}"
trap - EXIT

SC_PASS "probe-r26-authored-reaction complete"
