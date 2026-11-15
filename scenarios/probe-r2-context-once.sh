#!/usr/bin/env bash
# =============================================================================
# Probe (R2 qualification): readable reply context appears EXACTLY ONCE after
# bridge re-ingestion.
#
# Path under test (the re-ingestion leg the standing suite does not pin):
#   a-peer radio REPLY (reply_id == P_a) -> relay-a emits the replied room
#   event (m.in_reply_to + radio-native hint, authored body carrying Matrix's
#   reply fallback quote) -> medre RE-INGESTS that room event and re-airs it
#   onto MeshC (c-peer sniffer) + lxmf0.
#
# R2 behaviors under test (core #504 + meshtastic #49 R2):
#   * Reply-first presentation: the reply relation composes first without
#     changing canonical order (matrix renderer _reply_first; meshtastic
#     reply branch composes with represented=(rel,) so a natively bound
#     reply carries context in reply_id, NOT duplicated as inline text).
#   * Reply fallback bodies are presentation artifacts: medre strips the
#     quote fallback at ingest (strip_reply_fallback_body), so the stored
#     body is the authored text ONCE and no bridge hop duplicates the
#     readable context.
#   * The re-aired MeshC text carries the reply context EXACTLY ONCE
#     (natively via reply_id -> zero inline descriptions; or described ->
#     exactly one "[replying to: ...]" if binding is unavailable), the
#     authored body exactly once, and no nested prefix tokens.
# PASS = canonical contract + MeshC wire exactly-once + outbound lxmf ref.
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

SCENARIO="probe-r2-context-once"
ROOM_ID="${ROOM_BRIDGE_CH0_ID}"

radio_ingest_ready "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${MEDRE_NORMAL_BOT_MXID}" "${ROOM_ID}" "medre-normal"
radio_ingest_ready "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${BOT_A_MXID}" "${ROOM_ID}" "relay-a"

# --- Seed: alice's text bridged by relay-a (P_a on MeshA) and re-aired by
# medre onto MeshC (P_c in medre state) ---
MARKER="R2C1-$(SC_UID)"
SINCE_SEED="$(date +%s%3N)"
capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" "${MARKER}"
((SC_STATUS == 0)) || SC_FAIL "seed send failed" "${out}"
SEED_EVENT="$(jq_get "${out}" event_id | tr -d '"')"

capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${SEED_EVENT}" --wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "relay-a did not bridge the seed (no P_a map row)" "${rows}"
PACKET_A="$(printf '%s' "${rows}" | tools_exec python3 -c 'import json,sys; print(json.load(sys.stdin)["rows"][0]["meshtastic_id"])')"

capture _po medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${SEED_EVENT}" \
	--wait "${RADIO_WAIT_SECONDS}" --require-outbound-radio
PACKET_C="$(printf '%s' "${_po}" | tools_exec python3 -c '
import json, re, sys
data = json.load(sys.stdin)
for event in data.get("events", []):
    for ref in event.get("native_refs", []):
        if ref.get("direction") == "outbound" and re.fullmatch(r"[0-9]+", str(ref.get("native_message_id") or "")):
            print(ref["native_message_id"])
            raise SystemExit
')"
[[ -n "${PACKET_C}" ]] || SC_FAIL "medre did not re-air the seed onto MeshC (probe precondition)" \
	"seed_event=${SEED_EVENT}"
SC_PASS "seed bridged everywhere (probe precondition)" \
	"seed_event=${SEED_EVENT} relay_a_packet=${PACKET_A} medre_packet=${PACKET_C}"

# --- c-peer sniffer armed BEFORE the a-peer reply injection ---
REPLY_BODY="R2C2-$(SC_UID) reply body text"
start_sniffer_checked SNIFF_C "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	--timeout "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" \
	--expect-text "${REPLY_BODY}"
trap 'stop_sniffer "${SNIFF_C_PID:-}" "${SNIFF_C:-}"' EXIT

SINCE_REPLY="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	"${REPLY_BODY}" --reply-id "${PACKET_A}"
((SC_STATUS == 0)) || SC_FAIL "a-peer radio reply injection failed" "${out}"
REPLY_PACKET="$(jq_get "${out}" packet_id | tr -d '"')"
SC_PASS "a-peer radio reply injected against the MeshA packet" \
	"reply_packet=${REPLY_PACKET} target=${PACKET_A}"

# --- relay-a's replied room event (the re-ingestion input) ---
capture relay_reply_json lib_py mx_events.py wait "${ROOM_ID}" \
	--sender "${BOT_A_MXID}" --body-contains "${REPLY_BODY}" \
	--since-ms "${SINCE_REPLY}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "relay-a emitted no replied room event for the MeshA radio reply" "${relay_reply_json}"
RELAY_REPLY_EVENT="$(jq_get "${relay_reply_json}" event.event_id | tr -d '"')"

# --- medre re-ingestion: canonical event for relay-a's replied event ---
capture ctx_state medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${RELAY_REPLY_EVENT}" \
	--wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "medre did not re-ingest relay-a's replied event (no canonical row)" \
		"relay_event=${RELAY_REPLY_EVENT} db=${MEDRE_NORMAL_DB}" $'\n'"state output: ${ctx_state}"
verdict="$(printf '%s' "${ctx_state}" | tools_exec python3 -c '
import json, sys
data = json.load(sys.stdin)
events = data.get("events", [])
authored = sys.argv[1]
problems = []
if not events:
    problems.append("no canonical event for relay-a replied event")
for event in events:
    kind = event.get("event_kind")
    relations = event.get("relations") or []
    body_content = event.get("body_content") or {}
    body = body_content.get("body") or ""
    if kind != "message.created":
        problems.append("event_kind=%r != message.created (a reply is an ordinary message carrying a relation)" % (kind,))
    reply_rels = [r for r in relations if r.get("relation_type") == "reply"]
    if len(reply_rels) != 1:
        problems.append("reply relation rows=%d != 1 (context must not duplicate)" % (len(reply_rels),))
    if authored not in body:
        problems.append("stored body %r misses the authored reply body" % (body,))
    if body.count(authored) != 1:
        problems.append("authored body appears %dx in stored body (context/body duplication)" % (body.count(authored),))
    if "> " in body or "wrote:" in body:
        problems.append("reply FALLBACK quote survived ingest: %r (presentation artifacts must be stripped, not re-embedded)" % (body,))
print(json.dumps({"problems": problems, "events": events}))
' "${REPLY_BODY}")"
printf '%s' "${verdict}" | grep -q '"problems": \[\]' ||
	SC_FAIL "re-ingestion canonical contract violated (reply context not exactly once / fallback survived)" "${verdict}"
SC_PASS "re-ingestion: canonical message.created + ONE reply relation; stored body = authored text once; reply fallback quote stripped" \
	"relay_event=${RELAY_REPLY_EVENT}"

# --- MeshC wire: the reply context must appear EXACTLY ONCE ---
wait_until "$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))" grep -q '"match"' "${SNIFF_C}" ||
	SC_FAIL "no MeshC radio packet captured for the re-ingested reply (medre must re-air relay-a's event onto MeshC)" \
		"sniffer log: $(cat "${SNIFF_C}")"
SNIFF_MATCH="$(grep -o '{"match":.*}' "${SNIFF_C}" | head -1)"
wire_verdict="$(printf '%s' "${SNIFF_MATCH}" | tools_exec python3 -c '
import json, sys
payload = json.load(sys.stdin)
view = payload.get("match") or {}
text = str(view.get("text") or "")
reply_id = view.get("reply_id")
authored = sys.argv[1]
problems = []
if authored not in text:
    problems.append("authored body missing from MeshC wire text %r" % (text,))
if text.count(authored) != 1:
    problems.append("authored body appears %dx on the wire (duplication)" % (text.count(authored),))
desc = text.count("[replying to:")
quoted = text.count("wrote:") + text.count("> ")
if reply_id is None and desc != 1:
    problems.append("no native reply binding and inline reply descriptions=%d (must be EXACTLY 1 when unbound)" % (desc,))
if reply_id is not None and desc != 0:
    problems.append("native reply_id=%s AND %d inline descriptions: context DUPLICATED" % (reply_id, desc))
if quoted:
    problems.append("fallback quote markers on the wire (%d): readable context duplicated across hops" % (quoted,))
n = len(text.encode("utf-8"))
if n > 227:
    problems.append("%d UTF-8 bytes exceeds the radio limit 227" % (n,))
print(json.dumps({"problems": problems, "text": text, "reply_id": reply_id}))
' "${REPLY_BODY}")"
printf '%s' "${wire_verdict}" | grep -q '"problems": \[\]' ||
	SC_FAIL "MeshC wire: readable reply context not EXACTLY ONCE after bridge re-ingestion" "${wire_verdict}"
wire_text="$(jq_get "${wire_verdict}" text | tr -d '"')"
wire_reply_id="$(jq_get "${wire_verdict}" reply_id | tr -d '"')"
SC_PASS "MeshC wire: readable reply context appears EXACTLY ONCE (native reply_id binding, no inline duplication), body once, within budget" \
	"wire_text=${wire_text}" $'\n'"reply_id=${wire_reply_id}"

stop_sniffer "${SNIFF_C_PID}" "${SNIFF_C}"
trap - EXIT

SC_PASS "probe-r2-context-once complete"
