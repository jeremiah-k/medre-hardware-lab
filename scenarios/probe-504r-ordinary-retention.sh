#!/usr/bin/env bash
# =============================================================================
# Probe (#504 recovery qualification): ordinary-message retention under
# secondary reaction context
#
# Recovery evidence boundary #2 (rendering-r2-recovery report): the recorded
# annotation probes prove only message.reacted - an authored Matrix annotation
# whose body is stored once and suppressed on the radio. They do NOT prove the
# mirror contract: an ORDINARY authored message that carries reaction-shaped
# context secondarily remains message.created and retains its authored body.
#
# Leg 1 (live, radio -> Matrix, the classification boundary the rig CAN
# produce): a c-peer TEXT packet --reply-id bound to medre's re-aired packet
# with authored text that CONTAINS an emoji plus words and NO Data.emoji flag.
# The classifier must keep it an ordinary reply (message.created + reply
# relation; packet_classifier: reaction requires the native emoji flag), the
# authored text must arrive verbatim on Matrix exactly once, and the event
# must NOT take the described-reaction shape (no leading-newline emote, no
# "reacted" wording). Corpus-validated.
#
# Leg 2 (in-container renderer, the canonical shape no codec emits live):
# none of the mounted codecs constructs message.created WITH a reaction
# relation (matrix promotes annotations to message.reacted; meshtastic
# builds reaction relations only on emoji-flag packets; lxmf decodes no
# relations), so the canonical-model shape - message.created + unbound
# reaction relation + authored body - is proven at the MOUNTED recovered
# renderer inside medre-normal, mirroring the recovery suite's unit tier:
# extract_event_text composes the secondary reaction description once and
# preserves the authored body exactly once without mutating the event. The
# legacy extract_relation_text import (the mmrelay bake contract at the
# same core tree) is asserted alongside.
#
# PASS = both legs green. Radio transmissions: sim radios only, lab normal
# topology, one API client per node (injection via c-peer only).
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

SCENARIO="probe-504r-ordinary-retention"
ROOM_ID="${ROOM_BRIDGE_CH0_ID}"
OUT_DIR="${LAB_ROOT}/lab/logs/scenarios"
OUT="${OUT_DIR}/probe-504r-ordinary-retention-$(date +%Y%m%d-%H%M%S).json"

radio_ingest_ready "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${MEDRE_NORMAL_BOT_MXID}" "${ROOM_ID}" "medre-normal"

# (1) Seed: alice's Matrix text is bridged and re-aired onto MeshC; its
# re-air packet is the ordinary reply's target.
MARKER="ORD504R-$(SC_UID)"
SINCE_SEED="$(date +%s%3N)"
capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_ID}" \
	"${MARKER} please review"
((SC_STATUS == 0)) || SC_FAIL "seed send failed" "${out}"
SEED_EVENT="$(jq_get "${out}" event_id | tr -d '"')"

capture out medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${SEED_EVENT}" \
	--wait "${RADIO_WAIT_SECONDS}" --require-outbound-radio
((SC_STATUS == 0)) ||
	SC_FAIL "medre never re-aired the seed text onto MeshC (no outbound radio ref)" \
		"db=${MEDRE_NORMAL_DB} origin_event=${SEED_EVENT} waited=${RADIO_WAIT_SECONDS}s" $'\n'"state output: ${out}"
PACKET_C="$(printf '%s' "${out}" | tools_exec python3 -c '
import json, re, sys
data = json.load(sys.stdin)
for event in data.get("events", []):
    for ref in event.get("native_refs", []):
        if ref.get("direction") == "outbound" and re.fullmatch(
            r"[0-9]+", str(ref.get("native_message_id") or "")
        ):
            print(ref["native_message_id"])
            raise SystemExit
')"
SC_PASS "seed bridged and re-aired; ordinary reply target known" \
	"matrix_event=${SEED_EVENT} medre_packet=${PACKET_C} db=${MEDRE_NORMAL_DB}"

# (2) LIVE leg: ordinary authored reply carrying an emoji INSIDE its text,
# NO Data.emoji flag. Must stay message.created with the body preserved.
AUTHORED_TEXT="🦄 ${MARKER} nice find"
SINCE_REPLY="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_C_PEER_HOST}" "${NODE_C_PEER_PORT}" \
	"${AUTHORED_TEXT}" --reply-id "${PACKET_C}"
((SC_STATUS == 0)) || SC_FAIL "c-peer ordinary reply injection failed" "${out}"
REPLY_PACKET="$(jq_get "${out}" packet_id | tr -d '"')"
SC_PASS "ordinary emoji-bearing reply injected (no Data.emoji flag)" \
	"reply_packet=${REPLY_PACKET} target_packet=${PACKET_C} text=${AUTHORED_TEXT}"

capture reply_json lib_py mx_events.py wait "${ROOM_ID}" \
	--sender "${MEDRE_NORMAL_BOT_MXID}" --body-contains "${MARKER}" \
	--require-envelope --envelope-kind message.created --relation-info reply \
	--since-ms "${SINCE_REPLY}" --timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "no medre message.created event for the ordinary reply" \
		"reply_packet=${REPLY_PACKET} since=${SINCE_REPLY}" $'\n'"${reply_json}"

verdict="$(printf '%s' "${reply_json}" | tools_exec python3 -c "
import json, sys
payload = json.load(sys.stdin)
event = payload.get('event') or {}
content = event.get('content') or {}
body = content.get('body') or ''
marker, emoji = sys.argv[1], sys.argv[2]
problems = []
if event.get('type') != 'm.room.message':
    problems.append(f\"event type {event.get('type')!r} != m.room.message (ordinary reply)\")
if content.get('msgtype') != 'm.text':
    problems.append(f\"msgtype {content.get('msgtype')!r} != m.text (an ordinary authored message; the described-reaction emote shape would be m.emote)\")
if body.startswith('\n'):
    problems.append('ordinary body carries the reaction-emote leading newline (misclassified reaction shape)')
if 'reacted' in body:
    problems.append(f'ordinary body carries described-reaction wording: {body!r}')
if body.count(marker) != 1:
    problems.append(f'authored marker appears {body.count(marker)}x in the body, expected exactly once')
if emoji not in body:
    problems.append(f'authored emoji {emoji!r} lost from the preserved body: {body!r}')
print(json.dumps({'problems': problems, 'body': body}, ensure_ascii=False))
" "${MARKER}" "🦄")"
problems="$(jq_get "${verdict}" problems)"
if [[ ${problems} == "[]" ]]; then
	SC_PASS "LIVE leg: ordinary reply stayed message.created, authored body preserved verbatim exactly once" \
		"reply_packet=${REPLY_PACKET}" $'\n'"body=$(jq_get "${verdict}" body)"
else
	SC_FAIL "LIVE leg: ordinary-message retention violated" \
		"reply_packet=${REPLY_PACKET} problems=${problems}"
fi

medre_assert_valid "${reply_json}" "ordinary reply event"

# Canonical state: the stored event keeps its kind and authored body.
capture reply_state medre_state_py events-for-native "${MEDRE_NORMAL_DB}" "${REPLY_PACKET}" \
	--wait "${RADIO_WAIT_SECONDS}"
((SC_STATUS == 0)) ||
	SC_FAIL "medre stored no canonical event for the ordinary reply packet" \
		"reply_packet=${REPLY_PACKET} db=${MEDRE_NORMAL_DB}" $'\n'"state output: ${reply_state}"
state_verdict="$(printf '%s' "${reply_state}" | tools_exec python3 -c "
import json, sys
data = json.load(sys.stdin)
marker = sys.argv[1]
events = data.get('events', [])
problems = []
if not events:
    problems.append('no canonical event for the reply packet id')
for event in events:
    if event.get('event_kind') != 'message.created':
        problems.append(f\"stored kind {event.get('event_kind')!r} != message.created\")
    body_content = event.get('body_content') or {}
    payload = event.get('payload') or {}
    body = body_content.get('body') if body_content.get('body') is not None else payload.get('body')
    if not isinstance(body, str) or body.count(marker) != 1:
        problems.append(f'stored body does not preserve the authored text exactly once: {body!r}')
    reaction_rels = [r for r in (event.get('relations') or []) if r.get('relation_type') == 'reaction']
    if reaction_rels:
        problems.append(f'ordinary reply acquired reaction relation rows: {reaction_rels!r}')
print(json.dumps({'problems': problems}, ensure_ascii=False))
" "${MARKER}")"
state_problems="$(jq_get "${state_verdict}" problems)"
if [[ ${state_problems} == "[]" ]]; then
	SC_PASS "canonical state: message.created stored, authored body once, zero reaction relations" \
		"reply_packet=${REPLY_PACKET} db=${MEDRE_NORMAL_DB}"
else
	SC_FAIL "canonical state: ordinary-message retention violated" \
		"reply_packet=${REPLY_PACKET} problems=${state_problems}"
fi

# (3) RENDERER leg: the canonical secondary-reaction shape, executed by the
# MOUNTED recovered core inside medre-normal (same policy the bridge runs).
docker compose -f "${LAB_COMPOSE_FILE}" exec -T medre-normal python3 - >"${OUT}" <<'PYEOF'
import json
from datetime import UTC, datetime

from medre.core.events import CanonicalEvent, EventMetadata, EventRelation
from medre.core.rendering import extract_event_text, extract_relation_text

checks = []

def check(label, ok, detail=""):
    checks.append({"check": label, "ok": bool(ok), "detail": detail})

ev = CanonicalEvent(
    event_id="ordinary",
    event_kind="message.created",
    schema_version=1,
    timestamp=datetime.now(UTC),
    source_adapter="source",
    source_transport_id="sender",
    source_channel_id="channel",
    parent_event_id=None,
    lineage=(),
    relations=(EventRelation("reaction", None, None, "👍", None),),
    payload={"body": "please check this"},
    metadata=EventMetadata(),
)
rendered = extract_event_text(ev)
check(
    "secondary reaction composed once with preserved authored body",
    rendered == "reacted 👍 to an earlier message please check this",
    f"rendered={rendered!r}",
)
check("authored body appears exactly once", rendered.count("please check this") == 1)
check("event kind unchanged", ev.event_kind == "message.created")
check("payload not mutated", ev.payload == {"body": "please check this"})

plain = CanonicalEvent(
    event_id="plain",
    event_kind="message.created",
    schema_version=1,
    timestamp=datetime.now(UTC),
    source_adapter="source",
    source_transport_id="sender",
    source_channel_id="channel",
    parent_event_id=None,
    lineage=(),
    relations=(),
    payload={"body": "authored"},
    metadata=EventMetadata(),
)
check(
    "legacy extract_relation_text import + authored passthrough (mmrelay bake contract)",
    extract_relation_text(plain) == "authored",
)

failed = [c for c in checks if not c["ok"]]
print(json.dumps({
    "probe": "probe-504r-ordinary-retention/renderer",
    "checks": checks,
    "passed": len(checks) - len(failed),
    "failed": len(failed),
    "verdict": "PASS" if not failed else "FAIL",
}, ensure_ascii=False, indent=1))
PYEOF
RENDER_VERDICT="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["verdict"])
' "${OUT}")"
echo "[probe] renderer ordinary-retention evidence: ${OUT}"
if [[ ${RENDER_VERDICT} == "PASS" ]]; then
	RENDER_COUNTS="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(str(d["passed"]) + " passed / " + str(d["failed"]) + " failed")
' "${OUT}")"
	SC_PASS "RENDERER leg: mounted recovered policy preserves ordinary body under secondary reaction context" \
		"evidence=${OUT} ${RENDER_COUNTS}"
else
	SC_FAIL "RENDERER leg failed at the mounted renderer" "evidence=${OUT}" $'\n'"$(cat "${OUT}")"
fi
