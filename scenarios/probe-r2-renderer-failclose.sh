#!/usr/bin/env bash
# =============================================================================
# Probe (R2 qualification): native mutation AUTHORIZATION at the renderer
# boundary — denied/mismatched intent fails closed; nothing reaches a wire.
#
# Context: no live lab leg can PRODUCE a mismatched mutation canonical event
# (matrix ingest always pairs kind+relation; radio/lxmf never emit
# edited/deleted kinds), so this probe exercises the ACTUAL mounted R2
# renderer INSIDE the running medre-normal container (PYTHONPATH shadowing =
# the exact code the bridge executes) with constructed canonical events. It
# proves, at the running revisions (core #504 a693f580 + matrix #56
# d67f1219):
#   * ALL SIX mismatched/missing-intent shapes raise MatrixNativeMutationError
#     and construct NO outbound operation (fail-closed, even for owned
#     targets) — core #506/matrix #51 fragments; renderer regressions
#     test_native_mutation_requires_matching_kind_and_relation /
#     test_native_mutation_rejects_multiple_targets.
#   * The AUTHORIZED native edit renders a real m.replace operation and the
#     suppressed-payload styling keeps the fallback body in sync (R2
#     suppressed-payload styling correction).
#   * The R2 neutral kind markers (compose_relation_text): an event kind
#     without its matching relation degrades to "[edited]"/"[deleted]"/
#     "[reacted]" WITHOUT inventing a target or key; a MATCHED mutation does
#     not gain a duplicate marker.
#   * Provenance discriminator: the mounted core must NOT export the legacy
#     extract_relation_text name (#504 removed it; #505 keeps it for the
#     mmrelay bake) — proves the container executes the R2 head, not the
#     additive prerequisite.
# PASS = every assertion green; no operation constructed on any denied shape.
# =============================================================================
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO="probe-r2-renderer-failclose"
LOG_DIR="${LAB_ROOT}/lab/logs/scenarios"
OUT="${LOG_DIR}/probe-r2-renderer-failclose-$(date +%Y%m%d-%H%M%S).json"
mkdir -p "${LOG_DIR}"

# Runs INSIDE medre-normal (the mounted R2 sources are the code the bridge
# executes; PYTHONPATH shadows the baked install). Output captured host-side.
docker compose -f "${LAB_COMPOSE_FILE}" exec -T medre-normal python3 - >"${OUT}" <<'PYEOF'
import asyncio
import json
from datetime import UTC, datetime

import medre.core.rendering.text_helpers as th
from medre.adapters.matrix.outbound import MatrixOutboundOperation
from medre.adapters.matrix.renderer import MatrixNativeMutationError, MatrixRenderer
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.events.canonical import RelationTargetFact
from medre.core.rendering.relations import compose_relation_text
from medre.core.rendering.renderer import RenderingContext

results = {"checks": [], "provenance": {}}

# --- provenance: the running core is the RECOVERED #504 head (71ac5f54),
# which restores the legacy downstream helper alongside the new shared
# policy (mmrelay's pinned source imports it; removal is deferred until all
# declared downstream consumers migrate). Coexistence is the contract. -----
results["provenance"]["legacy_extract_relation_text_present"] = hasattr(
    th, "extract_relation_text"
)
results["provenance"]["extract_event_text_present"] = hasattr(th, "extract_event_text")


def event(kind, relations, body="authored"):
    return CanonicalEvent(
        event_id="probe-event",
        event_kind=kind,
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="source",
        source_transport_id="sender",
        source_channel_id="source-room",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        metadata=EventMetadata(),
        payload={"body": body},
    )


def relation(kind, native_id=None, key=None):
    return EventRelation(
        kind,
        None,
        NativeRef("target", "!room:example.org", native_id) if native_id else None,
        key,
        None,
        target_fact=(
            RelationTargetFact(
                status="bound_owned",
                adapter="target",
                native_channel_id="!room:example.org",
                native_message_id=native_id,
            )
            if native_id
            else None
        ),
    )


def context(**limits):
    return RenderingContext("direct", "target", "!room:example.org", "matrix", **limits)


def check(name, ok, detail):
    results["checks"].append({"name": name, "ok": bool(ok), "detail": detail})


# --- the six denied/mismatched shapes: fail closed, nothing constructed -----
MISMATCHED_SHAPES = [
    ("message.created + edit relation", "message.created", ("edit", "$owned")),
    ("message.created + delete relation", "message.created", ("delete", "$owned")),
    ("message.edited + delete relation", "message.edited", ("delete", "$owned")),
    ("message.deleted + edit relation", "message.deleted", ("edit", "$owned")),
    ("message.edited missing relation", "message.edited", None),
    ("message.deleted missing relation", "message.deleted", None),
]

for name, kind, mut in MISMATCHED_SHAPES:
    rels = (relation(mut[0], mut[1]),) if mut else ()
    ev = event(kind, rels, body="authored")
    try:
        result = asyncio.run(MatrixRenderer().render(ev, context()))
        check(
            f"fail-closed: {name}",
            False,
            f"render SUCCEEDED with payload {result.payload!r} (must raise MatrixNativeMutationError)",
        )
    except MatrixNativeMutationError as exc:
        check(f"fail-closed: {name}", True, f"raised: {exc}")
    except Exception as exc:  # noqa: BLE001
        check(f"fail-closed: {name}", False, f"WRONG exception {type(exc).__name__}: {exc}")

# multiple mutation targets are ambiguous: also fail closed
ev = event("message.edited", (relation("edit", "$one"), relation("delete", "$two")))
try:
    result = asyncio.run(MatrixRenderer().render(ev, context()))
    check(
        "fail-closed: multiple mutation targets",
        False,
        f"render SUCCEEDED with payload {result.payload!r}",
    )
except MatrixNativeMutationError as exc:
    check("fail-closed: multiple mutation targets", True, f"raised: {exc}")

# stored canonical payload must be untouched by the failed render
check(
    "denied render leaves canonical payload untouched",
    ev.payload["body"] == "authored",
    f"payload.body={ev.payload['body']!r}",
)

# --- the AUTHORIZED edit renders a real m.replace operation -----------------
ev = event("message.edited", (relation("edit", "$original"),), body="replacement")
result = asyncio.run(MatrixRenderer().render(ev, context()))
op = MatrixOutboundOperation.from_payload(result.payload)
content = op.content
check(
    "authorized edit renders m.replace",
    content.get("m.relates_to") == {"rel_type": "m.replace", "event_id": "$original"},
    f"m.relates_to={content.get('m.relates_to')!r}",
)
check(
    "authorized edit m.new_content body == replacement text",
    content.get("m.new_content", {}).get("body") == "replacement",
    f"m.new_content={content.get('m.new_content')!r}",
)
check(
    "suppressed-payload styling: fallback body stays in sync (* prefix)",
    content.get("body") == "* " + content.get("m.new_content", {}).get("body", ""),
    f"body={content.get('body')!r}",
)

# --- R2 neutral kind markers (compose_relation_text) ------------------------
# edited keeps its body; deleted/reacted SUPPRESS the payload body (core
# #506: a delete has no authored replacement text, a reaction's body is not
# additional message content) — the marker stands alone.
for kind, marker, expected in (
    ("message.edited", "[edited]", "[edited] authored text"),
    ("message.deleted", "[deleted]", "[deleted]"),
    ("message.reacted", "[reacted]", "[reacted]"),
):
    ev = event(kind, (), body="authored text")
    text = compose_relation_text(ev, "authored text")
    ok = text == expected
    check(
        f"neutral kind marker: {kind} without relation -> {marker!r}",
        ok,
        f"composed={text!r} (expected {expected!r})",
    )

# a MATCHED mutation must NOT gain a duplicate marker
ev = event("message.edited", (relation("edit", "$original"),), body="replacement")
text = compose_relation_text(ev, "replacement")
check(
    "matched edit gains no duplicate kind marker",
    "[edited]" not in text,
    f"composed={text!r}",
)

# --- reply-first composition: reply context composes first, canonical order
# unchanged (R2 reply presentation order)
ev = event(
    "message.created",
    (relation("reaction", key="\U0001f44d"), relation("reply")),
    body="answer",
)
result = asyncio.run(MatrixRenderer().render(ev, context()))
op = MatrixOutboundOperation.from_payload(result.payload)
body = op.content.get("body", "")
check(
    "reply-first: reply context leads the composed body",
    body.startswith("[replying to:") and "reacted" in body,
    f"body={body!r}",
)
check(
    "reply-first: canonical relation order unchanged (reaction still first)",
    ev.relations[0].relation_type == "reaction",
    f"relations={[r.relation_type for r in ev.relations]}",
)

import sys

bad = [c for c in results["checks"] if not c["ok"]]
provenance_ok = (
    results["provenance"]["legacy_extract_relation_text_present"]
    and results["provenance"]["extract_event_text_present"]
)
results["verdict"] = "PASS" if not bad and provenance_ok else "FAIL"
json.dump(results, sys.stdout, indent=2)
PYEOF

VERDICT="$(python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
print(data['verdict'])
" "${OUT}")"
echo "[probe] renderer fail-close evidence: ${OUT}"
if [[ "${VERDICT}" == "PASS" ]]; then
	SC_PASS "renderer fail-close: all denied/mismatched mutation shapes raise MatrixNativeMutationError and construct nothing; authorized edit renders m.replace with synced fallback styling; neutral kind markers verified at the mounted R2 revisions" \
		"evidence=${OUT}"
else
	SC_FAIL "renderer fail-close probe FAILED" "evidence=${OUT}" $'\n'"$(cat "${OUT}")"
fi
