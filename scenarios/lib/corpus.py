"""Corpus validator for canonical radio-origin wire events (interop authority).

ONE authority that judges BOTH implementations' Matrix emissions (@relay-a,
@relay-b, and the medre bot): every live bot-authored event is validated
against

  1. the shared interop contract in scenarios/corpus/canonical-events.json
     (pure-stdlib checks, runnable anywhere), and
  2. medre's INSTALLED schema validation when the medre package is
     importable: ``normalize_bridge_context`` (the exact trust-boundary
     validator both bridges run at ingress) plus a typed reconstruction of
     the canonical event (CanonicalEvent / EventRelation / NativeRef).

Runs INSIDE the lab-tools container for the contract checks; medre_lib.sh
pipes the same script into a container that has medre installed (the
builder's medre-relay service, falling back to the mmrelay relays, which pin
the coordinated medre contract) so the schema authority is always available.

CLI:
    python3 corpus.py validate '<event-json>'     # or '-' to read stdin
        Accepts a bare content dict, a full Matrix event ({content: ...}), or
        the mx_events.py wait wrapper ({event: ...}).
        Prints a JSON verdict; exit 0 ok, 1 violations, 3 when the medre
        schema authority is unavailable but required
        (MEDRE_CORPUS_REQUIRE_SCHEMA=1) and the contract itself passed.
    python3 corpus.py selftest [--corpus PATH]
        Validates every corpus case example (positives) and a battery of
        contract-violation mutations (negatives) - proves the validator and
        corpus agree and that every invariant class actually bites.

Corpus path resolution: --corpus flag > $CORPUS_PATH >
/scenarios/corpus/canonical-events.json > ../corpus/ relative to this file.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

DEFAULT_CORPUS_CANDIDATES = (
    "/scenarios/corpus/canonical-events.json",
    "/lab/scenarios/corpus/canonical-events.json",
)

NUMERIC_CHANNEL_RE = re.compile(r"^0$|^[1-9][0-9]*$")
PACKET_ID_RE = re.compile(r"^[0-9]{1,10}$")
FACT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Addressed-transport origins (LXMF and friends): the "channel" is the
# delivery destination hash and the message id is the transport's message
# hash - both lowercase hex, no radio channel concept at all.
ADDRESSED_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
ADDRESSED_MESSAGE_ID_RE = re.compile(r"^[0-9a-f]{32,64}$")

REQUIRED_ENVELOPE_KEYS = (
    "schema_version",
    "canonical_event_id",
    "source_adapter",
    "source_transport_id",
    "source_channel",
    "source_native_ref",
    "event_kind",
    "relation_info",
)


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _bounded_str(value: object, limit: int) -> bool:
    """Accept bounded UTF-8-encodable strings (mirrors medre's _text/_utf8)."""
    if not isinstance(value, str):
        return False
    if len(value) > limit:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_packet_id(value: object, max_id: int) -> bool:
    """Unsigned 32-bit packet id as decimal string; ZERO is valid."""
    if not isinstance(value, str) or PACKET_ID_RE.fullmatch(value) is None:
        return False
    return int(value) <= max_id


def _numeric_channel(value: object) -> bool:
    return isinstance(value, str) and NUMERIC_CHANNEL_RE.fullmatch(value) is not None


def load_corpus(
    path: str | None = None, corpus_json: str | None = None
) -> dict[str, Any]:
    if corpus_json:
        return json.loads(corpus_json)
    candidates = []
    if path:
        candidates.append(path)
    env_path = os.environ.get("CORPUS_PATH", "")
    if env_path:
        candidates.append(env_path)
    candidates.extend(DEFAULT_CORPUS_CANDIDATES)
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(here, "..", "corpus", "canonical-events.json"))
    except NameError:
        pass  # executed from stdin (python3 -): no script path
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            with open(candidate, encoding="utf-8") as handle:
                return json.load(handle)
    raise SystemExit(
        "corpus file not found (looked in: "
        + ", ".join(c for c in candidates if c)
        + ")"
    )


def extract_content(payload: Any) -> dict[str, Any] | None:
    """Accept a bare content dict, a Matrix event, or the mx_events wrapper."""
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("medre"), dict):
        return payload
    if isinstance(payload.get("event"), dict):
        event = payload["event"]
        content = extract_content(event)
        # Carry the wire event type into the content view so case matching
        # can select on the rendered event type (rendered_event_type).
        if (
            isinstance(content, dict)
            and isinstance(event.get("type"), str)
            and "__matrix_event_type" not in content
        ):
            content["__matrix_event_type"] = event["type"]
        return content
    if isinstance(payload.get("content"), dict):
        return payload["content"]
    return None


# ---------------------------------------------------------------------------
# Contract validation (stdlib only)
# ---------------------------------------------------------------------------


def _validate_native_ref(
    ref: Any,
    *,
    label: str,
    violations: list[str],
    contract: dict[str, Any],
    require_numeric_channel: bool,
    source_channel: str | None,
    radio_adapter: str | None,
    addressed: bool = False,
    allow_matrix_target: bool = True,
) -> None:
    max_id = contract["packet_id_max"]
    if not isinstance(ref, dict):
        violations.append(f"{label}: must be an object")
        return
    adapter = ref.get("adapter")
    if not _bounded_str(adapter, contract["max_bridge_string"]) or not adapter:
        violations.append(f"{label}.adapter: must be a non-empty bounded string")
    elif (
        require_numeric_channel
        and radio_adapter
        and adapter != radio_adapter
        and isinstance(ref.get("native_message_id"), str)
        and not ref["native_message_id"].startswith("$")
    ):
        violations.append(
            f"{label}.adapter ({adapter!r}) != envelope source_adapter"
            f" ({radio_adapter!r}) for a radio-native reference"
        )
    elif (
        require_numeric_channel
        and radio_adapter
        and adapter == radio_adapter
        and isinstance(ref.get("native_message_id"), str)
        and ref["native_message_id"].startswith("$")
    ):
        # The matrix-target carve-out below exempts "$" ids from the radio
        # invariants; it must not become a chimera license: a radio adapter
        # never owns Matrix event ids, so a $-prefixed reference naming the
        # radio source_adapter is incoherent in the other direction too.
        violations.append(
            f"{label}.adapter ({adapter!r}) == envelope source_adapter for a"
            " $-prefixed Matrix-native reference - a radio adapter never"
            " owns Matrix event ids"
        )
    message_id = ref.get("native_message_id")
    if ref.get("native_message_id") is not None and not isinstance(message_id, str):
        violations.append(f"{label}.native_message_id: must be a string")
    # A "$"-prefixed message id is a MATRIX-native reference (the documented
    # reply-target contract: the relation scopes to the Matrix room, not the
    # radio). Its channel is the ROOM id, never a numeric radio channel -
    # and the numeric-channel invariants below do not apply to it. The
    # carve-out applies to relation-hint TARGET refs only: a SOURCE ref on a
    # radio/addressed origin is a numeric packet id or the transport's
    # message hash, never a Matrix event id (allow_matrix_target=False),
    # which closes the chimeric class numeric-channel + $-id + room channel.
    is_matrix_target = isinstance(message_id, str) and message_id.startswith("$")
    if is_matrix_target and not allow_matrix_target:
        violations.append(
            f"{label}.native_message_id ({message_id!r}): a $-prefixed"
            " Matrix-native id is never a SOURCE reference - the"
            " matrix-target carve-out applies to relation-hint TARGET refs"
            " only"
        )
        return
    channel = ref.get("native_channel_id")
    if addressed:
        # Addressed-transport reference: the message id is the transport's
        # hex message hash; the channel concept does not exist (null/absent,
        # or a bounded restatement of the delivery destination).
        if not (
            isinstance(message_id, str)
            and ADDRESSED_MESSAGE_ID_RE.fullmatch(message_id) is not None
        ):
            violations.append(
                f"{label}.native_message_id ({message_id!r}): must be the"
                " transport's lowercase hex message hash (32-64 chars)"
            )
        if channel is not None and str(channel) != str(source_channel):
            violations.append(
                f"{label}.native_channel_id ({channel!r}): must be null or"
                f" the delivery destination hash ({source_channel!r})"
            )
        thread = ref.get("native_thread_id")
        if thread is not None and not _bounded_str(
            thread, contract["max_bridge_string"]
        ):
            violations.append(
                f"{label}.native_thread_id: must be a bounded string or null"
            )
        return
    if is_matrix_target:
        if not (isinstance(channel, str) and channel.startswith("!")):
            violations.append(
                f"{label}.native_channel_id ({channel!r}): a Matrix-native"
                " reference must scope to the room (room id starting with '!')"
                " - a numeric radio channel never carries a Matrix event id"
            )
    elif require_numeric_channel:
        if not _numeric_channel(channel):
            violations.append(
                f"{label}.native_channel_id ({channel!r}): must be a numeric"
                " radio channel id - a meshnet label never substitutes for it"
            )
        elif (
            source_channel is not None
            and channel is not None
            and channel != source_channel
        ):
            violations.append(
                f"{label}.native_channel_id ({channel!r}) != source channel"
                f" ({source_channel!r}): every native ref on one event carries"
                " the SAME numeric channel id"
            )
    elif channel is not None and not _bounded_str(
        channel, contract["max_bridge_string"]
    ):
        violations.append(f"{label}.native_channel_id: must be a bounded string")
    if (
        require_numeric_channel
        and not is_matrix_target
        and isinstance(message_id, str)
        and not _is_packet_id(message_id, max_id)
    ):
        violations.append(
            f"{label}.native_message_id ({message_id!r}): must be a decimal"
            f" packet id 0..{max_id} (zero is valid)"
        )
    thread = ref.get("native_thread_id")
    if thread is not None and not _bounded_str(thread, contract["max_bridge_string"]):
        violations.append(f"{label}.native_thread_id: must be a bounded string or null")


def _validate_native_facts(
    facts: Any,
    *,
    violations: list[str],
    contract: dict[str, Any],
    addressed: bool = False,
) -> str | None:
    """Return the meshnet label (if any) and append violations."""
    if not isinstance(facts, dict):
        if not addressed:
            violations.append(
                "bridge_context.source.native_facts: radio-origin events must"
                " carry bounded public source facts"
            )
        return None
    if set(facts.keys()) != {"namespace", "fields"}:
        violations.append(
            "native_facts: must be exactly {namespace, fields} (got"
            f" {sorted(facts.keys())})"
        )
        return None
    namespace = facts.get("namespace")
    if not addressed and namespace != "meshtastic":
        violations.append(
            f"native_facts.namespace ({namespace!r}): must be 'meshtastic'"
            " for radio-origin events"
        )
    if addressed and (not isinstance(namespace, str) or not namespace):
        violations.append(
            f"native_facts.namespace ({namespace!r}): must be a non-empty"
            " bounded transport namespace when an addressed-transport origin"
            " carries source facts"
        )
    fields = facts.get("fields")
    if not isinstance(fields, dict) or not 1 <= len(fields) <= contract["max_facts"]:
        violations.append(
            f"native_facts.fields: must hold 1..{contract['max_facts']} bounded"
            f" facts (got {len(fields) if isinstance(fields, dict) else 'non-object'})"
        )
        return None
    meshnet_label: str | None = None
    for key, item in fields.items():
        if not isinstance(key, str) or FACT_KEY_RE.fullmatch(key) is None:
            violations.append(
                f"native_facts.fields key {key!r}: must match"
                f" {contract['fact_key_re']}"
            )
            continue
        if isinstance(item, bool) or item is None:
            if item is None:
                violations.append(f"native_facts.fields.{key}: null is not a fact")
            continue
        if isinstance(item, str):
            if not _bounded_str(item, contract["max_fact_value"]):
                violations.append(
                    f"native_facts.fields.{key}: string value must be bounded"
                    f" (<= {contract['max_fact_value']}) and UTF-8 encodable"
                )
            if key == "meshnet_label" and isinstance(item, str) and item:
                meshnet_label = item
        elif _is_int(item):
            if not -(2**31) <= item < 2**31:
                violations.append(
                    f"native_facts.fields.{key}: integer value out of range"
                )
        else:
            violations.append(
                f"native_facts.fields.{key}: only bounded strings, booleans and"
                " integers may cross the transport boundary"
            )
    if meshnet_label is not None and not _bounded_str(
        meshnet_label, contract["max_fact_value"]
    ):
        violations.append(
            "native_facts.fields.meshnet_label: must be a non-empty bounded string"
        )
    return meshnet_label


def _validate_source_attribution(
    source: dict[str, Any],
    *,
    violations: list[str],
    contract: dict[str, Any],
) -> None:
    sender_id = source.get("sender_id")
    if not _bounded_str(sender_id, contract["max_bridge_string"]) or not sender_id:
        violations.append(
            f"bridge_context.source.sender_id ({sender_id!r}): sender"
            " attribution requires a non-empty bounded sender id"
        )
    labels = [name for name in contract["attribution"]["any_of"] if source.get(name)]
    if not labels:
        violations.append(
            "bridge_context.source: at least one of "
            f"{contract['attribution']['any_of']} must carry the sender label"
        )
    for name in ("sender_name", "sender_short_name", "sender_verification", "platform"):
        value = source.get(name)
        if value is not None and not _bounded_str(value, contract["max_bridge_string"]):
            violations.append(
                f"bridge_context.source.{name}: must be a bounded string or null"
            )


def _walk_label_leaks(node: Any, label: str, path: str, leaks: list[str]) -> None:
    """Find the meshnet label occupying any field outside its attribution homes."""
    if isinstance(node, dict):
        for key, item in node.items():
            child_path = f"{path}.{key}"
            if key in {"native_facts", "origin_label", "fields", "meshnet_label"}:
                # native_facts.fields.meshnet_label and source.origin_label are
                # the two legitimate attribution homes of the meshnet label.
                if key == "origin_label" and item == label:
                    continue
                _walk_label_leaks(item, label, child_path, leaks)
                continue
            if item == label:
                leaks.append(child_path)
            else:
                _walk_label_leaks(item, label, child_path, leaks)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk_label_leaks(item, label, f"{path}[{index}]", leaks)


def validate_event(
    content: dict[str, Any], corpus: dict[str, Any]
) -> tuple[list[str], str | None]:
    """Validate one wire content dict against the corpus contract.

    Returns (violations, matched_case_name).
    """
    violations: list[str] = []
    contract = corpus["contract"]

    medre = content.get("medre")
    if not isinstance(medre, dict):
        return (
            [
                "content.medre: missing - the corpus only judges canonical"
                " bridge emissions carrying the medre envelope"
            ],
            None,
        )
    envelope = medre.get("envelope")
    if not isinstance(envelope, dict):
        return (["content.medre.envelope: missing"], None)
    for key in REQUIRED_ENVELOPE_KEYS:
        if key not in envelope:
            violations.append(f"envelope.{key}: required key missing")

    schema_version = envelope.get("schema_version")
    if (
        not _is_int(schema_version)
        or schema_version != contract["envelope_schema_version"]
    ):
        violations.append(
            f"envelope.schema_version ({schema_version!r}): must be the integer"
            f" {contract['envelope_schema_version']} (booleans are not integers)"
        )

    canonical_event_id = envelope.get("canonical_event_id")
    if (
        not _bounded_str(canonical_event_id, contract["max_bridge_string"])
        or not canonical_event_id
    ):
        violations.append(
            "envelope.canonical_event_id: must be a non-empty bounded string"
        )

    source_adapter = envelope.get("source_adapter")
    if (
        not _bounded_str(source_adapter, contract["max_bridge_string"])
        or not source_adapter
    ):
        violations.append("envelope.source_adapter: must be a non-empty bounded string")
    # Adapter ids are OPERATOR-CONFIGURED (mmrelay.meshtastic vs medre's
    # radio-a/radio-b), so no name suffix is enforced; the radio-origin class
    # is pinned by the numeric channel/packet-id invariants below (a Matrix
    # adapter's room-id channels and $event ids cannot satisfy them).

    source_transport_id = envelope.get("source_transport_id")
    if (
        not _bounded_str(source_transport_id, contract["max_bridge_string"])
        or not source_transport_id
    ):
        violations.append(
            "envelope.source_transport_id: must be a non-empty bounded"
            " transport identity (native node id)"
        )

    source_channel = envelope.get("source_channel")
    # Origin class is pinned by channel SHAPE (adapter ids are
    # operator-configured): a numeric channel is a radio origin; a 32-hex
    # delivery-destination hash is an addressed-transport origin (LXMF and
    # friends). Anything else violates either class.
    addressed = (
        isinstance(source_channel, str)
        and ADDRESSED_HASH_RE.fullmatch(source_channel) is not None
    )
    if not _numeric_channel(source_channel) and not addressed:
        violations.append(
            f"envelope.source_channel ({source_channel!r}): must be a numeric"
            " radio channel id or the addressed transport's 32-hex delivery"
            " destination hash - a meshnet label never substitutes for either"
        )

    event_kind = envelope.get("event_kind")
    if event_kind not in contract["event_kinds"]:
        violations.append(
            f"envelope.event_kind ({event_kind!r}): must be one of"
            f" {contract['event_kinds']}"
        )

    relation_info = envelope.get("relation_info")
    if not isinstance(relation_info, str):
        violations.append("envelope.relation_info: must be a string")

    source_ref = envelope.get("source_native_ref")
    _validate_native_ref(
        source_ref,
        label="envelope.source_native_ref",
        violations=violations,
        contract=contract,
        require_numeric_channel=True,
        source_channel=source_channel if isinstance(source_channel, str) else None,
        radio_adapter=source_adapter if isinstance(source_adapter, str) else None,
        addressed=addressed,
        # The matrix-target carve-out is a relation-hint TARGET contract: a
        # radio/addressed origin's SOURCE ref is a numeric packet id or the
        # transport's message hash, never a $-prefixed Matrix event id.
        allow_matrix_target=False,
    )

    # --- bridge_context ------------------------------------------------------
    metadata = medre.get("metadata")
    context = metadata.get("bridge_context") if isinstance(metadata, dict) else None
    hints: list[dict[str, Any]] = []
    if not isinstance(context, dict):
        violations.append(
            "content.medre.metadata.bridge_context: missing - canonical bridge"
            " emissions carry bounded source claims and relation hints"
        )
    else:
        context_schema = context.get("schema_version")
        if not _is_int(context_schema) or context_schema != 1:
            violations.append("bridge_context.schema_version: must be the integer 1")
        source = context.get("source")
        if not isinstance(source, dict):
            violations.append("bridge_context.source: must be an object")
        else:
            # Coherence with the envelope (the checks both bridges enforce at
            # ingress before granting any peer authority).
            if source.get("adapter") != source_adapter:
                violations.append(
                    "bridge_context.source.adapter != envelope.source_adapter:"
                    " provenance incoherent"
                )
            if source.get("canonical_event_id") != canonical_event_id:
                violations.append(
                    "bridge_context.source.canonical_event_id !="
                    " envelope.canonical_event_id: provenance incoherent"
                )
            if source.get("channel") != source_channel:
                violations.append(
                    "bridge_context.source.channel != envelope.source_channel:"
                    " provenance incoherent"
                )
            if source.get("sender_id") != source_transport_id:
                violations.append(
                    "bridge_context.source.sender_id !="
                    " envelope.source_transport_id: provenance incoherent"
                )
            if source.get("native_ref") != source_ref:
                violations.append(
                    "bridge_context.source.native_ref !="
                    " envelope.source_native_ref: provenance incoherent"
                )
            _validate_source_attribution(
                source, violations=violations, contract=contract
            )
            meshnet_label = _validate_native_facts(
                source.get("native_facts"),
                violations=violations,
                contract=contract,
                addressed=addressed,
            )
            if meshnet_label:
                origin_label = source.get("origin_label")
                if origin_label is not None and not _bounded_str(
                    origin_label, contract["max_bridge_string"]
                ):
                    violations.append(
                        "bridge_context.source.origin_label: must be a bounded"
                        " string or null"
                    )
                leaks: list[str] = []
                _walk_label_leaks(envelope, meshnet_label, "envelope", leaks)
                _walk_label_leaks(context, meshnet_label, "bridge_context", leaks)
                if leaks:
                    violations.append(
                        "meshnet label leaked outside its attribution homes"
                        " (native_facts.fields.meshnet_label / source.origin_label):"
                        f" {', '.join(leaks)}"
                    )

        raw_hints = context.get("relations")
        if not isinstance(raw_hints, list):
            violations.append("bridge_context.relations: must be a list")
        elif len(raw_hints) > contract["max_relations"]:
            violations.append(
                f"bridge_context.relations: bounded to {contract['max_relations']}"
                f" (got {len(raw_hints)})"
            )
        else:
            for index, hint in enumerate(raw_hints):
                if not isinstance(hint, dict):
                    violations.append(f"relations[{index}]: must be an object")
                    continue
                kind = hint.get("relation_type")
                if kind not in contract["relation_types"]:
                    violations.append(
                        f"relations[{index}].relation_type ({kind!r}): must be"
                        f" one of {contract['relation_types']}"
                    )
                    continue
                key = hint.get("key")
                if kind == "reaction" and (
                    not _bounded_str(key, contract["max_bridge_string"]) or not key
                ):
                    violations.append(
                        f"relations[{index}]: reaction requires the exact"
                        " non-empty relation key"
                    )
                if kind == "reply" and key is not None:
                    violations.append(f"relations[{index}]: reply hints carry no key")
                _validate_native_ref(
                    hint.get("target_native_ref"),
                    label=f"relations[{index}].target_native_ref",
                    violations=violations,
                    contract=contract,
                    require_numeric_channel=True,
                    source_channel=(
                        source_channel if isinstance(source_channel, str) else None
                    ),
                    radio_adapter=(
                        source_adapter if isinstance(source_adapter, str) else None
                    ),
                    addressed=addressed,
                    allow_matrix_target=True,
                )
                hints.append(hint)

            # Coherence: relation_info <-> hints, kind <-> event_kind, and the
            # one-relation bound both bridges enforce on radio peer events.
            hint_kinds = sorted(
                {
                    h["relation_type"]
                    for h in hints
                    if isinstance(h, dict) and h.get("relation_type")
                }
            )
            if relation_info != ",".join(hint_kinds):
                violations.append(
                    f"envelope.relation_info ({relation_info!r}) != sorted hint"
                    f" kinds ({','.join(hint_kinds)!r}): provenance incoherent"
                )
            if len(hints) > 1:
                violations.append(
                    "radio peer events carry at most one reply/reaction relation"
                    f" (got {len(hints)})"
                )
            if "reaction" in hint_kinds and event_kind != "message.reacted":
                violations.append(
                    "reaction hints require envelope.event_kind message.reacted"
                )
            if "reply" in hint_kinds and event_kind != "message.created":
                violations.append(
                    "reply hints require envelope.event_kind message.created"
                    " (the wire has no message.replied kind)"
                )

    # --- rendered content ----------------------------------------------------
    msgtype = content.get("msgtype")
    event_type = content.get("__matrix_event_type")
    # Described reaction delivery (matrix adapter #53; meshtastic renderer
    # 614372e): EVERY bot-authored reaction renders as a described m.emote
    # room message - the relay attribution prefix, the exact canonical key,
    # and the quoted target preview ("an earlier message" when the target is
    # unresolvable). The native m.reaction annotation is the RETIRED shape:
    # it carried the bridge identity instead of the reacting source user and
    # Matrix annotation idempotency collapsed same-key reactions, so it is no
    # longer a licensed rendering (a bot-authored m.reaction fails the
    # non-empty-body rule below and no corpus case covers it). The wire event
    # type still reaches the validator as __matrix_event_type (injected by
    # extract_content from full Matrix events; corpus case examples carry it
    # inline) for case matching.
    is_reaction = event_kind == "message.reacted"
    is_reaction_emote = is_reaction and msgtype == "m.emote"
    # Owner ruling (2026-11-12): the reaction emote body carries EXACTLY ONE
    # intentional leading newline - mmrelay's radio->Matrix reaction emote
    # presentation template is the design reference, and two or ZERO leading
    # newlines is the defect class.
    body = content.get("body")
    if not isinstance(body, str) or not body:
        violations.append("content.body: must be a non-empty authored string")
    else:
        if len(body.encode("utf-8", "replace")) > 4096:
            violations.append(
                "content.body: implausibly large (prefix storm or fallback leak)"
            )
        if is_reaction_emote:
            if not body.startswith("\n"):
                violations.append(
                    "content.body: reaction emote bodies carry EXACTLY"
                    " ONE intentional leading newline (mmrelay emote shape);"
                    " got none"
                )
            elif body.startswith("\n\n"):
                violations.append(
                    "content.body: reaction emote bodies carry EXACTLY"
                    " ONE intentional leading newline (mmrelay emote shape);"
                    " got two or more"
                )
        elif body.startswith("\n"):
            violations.append(
                "content.body: leading newline (stray fallback-prefix shape;"
                " bodies start with the authored text)"
            )
    if is_reaction:
        reaction_key = next(
            (
                h.get("key")
                for h in hints
                if isinstance(h, dict) and h.get("relation_type") == "reaction"
            ),
            None,
        )
        if msgtype != "m.emote":
            violations.append(
                f"content.msgtype ({msgtype!r}): reactions render as described"
                " m.emote messages (described reaction delivery, adapter #53;"
                " the native m.reaction annotation is retired)"
            )
        elif isinstance(body, str):
            if reaction_key and reaction_key not in body:
                violations.append(
                    f"content.body: exact relation key {reaction_key!r} missing"
                    " from the rendered reaction body (key paraphrased or lost)"
                )
            if 'to ""' in body:
                violations.append(
                    'content.body: empty fallback quote (to "") — unresolvable'
                    ' targets quote "an earlier message" instead'
                )
    elif msgtype not in {"m.text", "m.emote"}:
        violations.append(
            f"content.msgtype ({msgtype!r}): radio-origin messages render as"
            " m.text (or the m.emote fallback)"
        )

    # --- internal field leakage ----------------------------------------------
    # PR #45 removed the retired MMRelay interop surface: flat meshtastic_*
    # wire keys are never emitted on outbound events (all radio facts travel
    # inside content.medre.metadata.bridge_context.source.native_facts).
    flat_fields = sorted(
        key for key in content if isinstance(key, str) and key.startswith("meshtastic_")
    )
    if flat_fields:
        violations.append(
            "flat meshtastic_* fields on the wire event (retired interop"
            f" surface): {', '.join(flat_fields)}"
        )
    if isinstance(body, str) and isinstance(source_ref, dict):
        packet_id = source_ref.get("native_message_id")
        transport_id = envelope.get("source_transport_id")
        leaks = []
        if "TEXT_MESSAGE_APP" in body:
            leaks.append("TEXT_MESSAGE_APP")
        if (
            isinstance(transport_id, str)
            and transport_id.startswith("!")
            and transport_id in body
        ):
            leaks.append(f"transport id {transport_id}")
        if (
            isinstance(packet_id, str)
            and packet_id
            and re.search(rf"(?<!\d){re.escape(packet_id)}(?!\d)", body)
        ):
            leaks.append(f"packet id {packet_id}")
        if leaks:
            violations.append("content.body leaks internal fields: " + ", ".join(leaks))

    # --- corpus coverage ------------------------------------------------------
    matched_case = None
    if not violations:
        for case in corpus.get("cases", []):
            match = case.get("match", {})
            if match.get("event_kind") != event_kind:
                continue
            if match.get("relation_info") != relation_info:
                continue
            if (
                match.get("channel") is not None
                and match.get("channel") != source_channel
            ):
                continue
            wanted_class = match.get("channel_class")
            if wanted_class == "addressed" and not addressed:
                continue
            if wanted_class == "radio" and addressed:
                continue
            wanted_target = match.get("hint_target")
            if wanted_target is not None:
                if not hints:
                    actual = "none"
                elif str(
                    (hints[0].get("target_native_ref") or {}).get("native_message_id")
                    or ""
                ).startswith("$"):
                    actual = "matrix"
                else:
                    actual = "radio"
                if actual != wanted_target:
                    continue
            wanted_type = match.get("rendered_event_type")
            if wanted_type is not None and event_type != wanted_type:
                continue
            matched_case = case.get("name")
            break
        if matched_case is None:
            violations.append(
                f"no corpus case covers event_kind={event_kind!r}"
                f" relation_info={relation_info!r} channel={source_channel!r}:"
                " the golden corpus does not license this shape"
            )
    return violations, matched_case


# ---------------------------------------------------------------------------
# medre installed-schema authority
# ---------------------------------------------------------------------------


def medre_schema_violations(content: dict[str, Any]) -> tuple[list[str], str | None]:
    """Run medre's installed validators over the wire event.

    Returns (violations, authority_description). violations is None when the
    medre package is not importable (authority unavailable).
    """
    try:
        import medre  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - authority probe
        return None, f"unavailable: medre import failed ({type(exc).__name__}: {exc})"

    violations: list[str] = []
    medre_block = content.get("medre")
    envelope = medre_block.get("envelope") if isinstance(medre_block, dict) else None
    metadata = medre_block.get("metadata") if isinstance(medre_block, dict) else None
    context = metadata.get("bridge_context") if isinstance(metadata, dict) else None

    checks = ["bridge_context.normalize"]
    if isinstance(context, dict):
        try:
            from medre.core.rendering.bridge_context import normalize_bridge_context

            normalized = normalize_bridge_context(context)
            if normalized is None:
                violations.append(
                    "medre normalize_bridge_context rejected the wire"
                    " bridge_context (invalid v1 shape or over-bounded claims)"
                )
        except Exception as exc:  # noqa: BLE001 - strict boundary
            violations.append(
                f"medre bridge_context validation raised"
                f" {type(exc).__name__}: {exc}"
            )

    if isinstance(envelope, dict):
        checks.append("CanonicalEvent/EventRelation/NativeRef")
        try:
            from datetime import UTC, datetime

            from medre.core.events import (
                CanonicalEvent,
                EventMetadata,
                EventRelation,
                NativeRef,
            )

            source_ref = envelope.get("source_native_ref")
            native_ref = None
            if isinstance(source_ref, dict):
                native_ref = NativeRef(
                    adapter=source_ref.get("adapter"),
                    native_channel_id=source_ref.get("native_channel_id"),
                    native_message_id=source_ref.get("native_message_id") or "",
                    native_thread_id=source_ref.get("native_thread_id"),
                )
            relations = []
            for hint in (context or {}).get("relations") or []:
                if not isinstance(hint, dict):
                    continue
                target = hint.get("target_native_ref")
                relations.append(
                    EventRelation(
                        relation_type=hint.get("relation_type"),
                        target_event_id=None,
                        target_native_ref=(
                            NativeRef(
                                adapter=target.get("adapter"),
                                native_channel_id=target.get("native_channel_id"),
                                native_message_id=target.get("native_message_id") or "",
                                native_thread_id=target.get("native_thread_id"),
                            )
                            if isinstance(target, dict)
                            else None
                        ),
                        key=hint.get("key"),
                        fallback_text=None,
                    )
                )
            CanonicalEvent(
                event_id=str(envelope.get("canonical_event_id") or ""),
                event_kind=str(envelope.get("event_kind") or ""),
                schema_version=1,
                timestamp=datetime.now(UTC),
                source_adapter=str(envelope.get("source_adapter") or ""),
                source_transport_id=str(envelope.get("source_transport_id") or ""),
                source_channel_id=(
                    envelope.get("source_channel")
                    if envelope.get("source_channel")
                    else None
                ),
                parent_event_id=None,
                lineage=(),
                relations=relations,
                payload={"body": content.get("body") or ""},
                metadata=EventMetadata(),
                source_native_ref=native_ref,
            )
        except Exception as exc:  # noqa: BLE001 - strict typed model
            violations.append(
                f"medre canonical model rejected the event"
                f" ({type(exc).__name__}: {exc})"
            )

    authority = "medre(" + "+".join(checks) + ")"
    return violations, authority


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _verdict(
    payload: Any, corpus: dict[str, Any], require_schema: bool
) -> tuple[dict[str, Any], int]:
    content = extract_content(payload)
    if content is None:
        return (
            {
                "ok": False,
                "case": None,
                "violations": ["payload is not a Matrix message content/event"],
                "schema_authority": None,
                "schema_violations": None,
            },
            1,
        )

    violations, matched_case = validate_event(content, corpus)
    schema_violations, authority = medre_schema_violations(content)
    if schema_violations is None:
        authority = authority or "unavailable"
        schema_violations = None
        if require_schema:
            exit_code = 3 if not violations else 1
        else:
            exit_code = 1 if violations else 0
    else:
        if schema_violations:
            violations = violations + [
                f"[medre-schema] {violation}" for violation in schema_violations
            ]
        exit_code = 1 if violations else 0

    medre_block = content.get("medre") or {}
    envelope = medre_block.get("envelope") or {}
    source_ref = envelope.get("source_native_ref") or {}
    verdict = {
        "ok": not violations,
        "case": matched_case,
        "violations": violations,
        "schema_authority": authority,
        "schema_violations": schema_violations,
        "summary": {
            "event_kind": envelope.get("event_kind"),
            "relation_info": envelope.get("relation_info"),
            "source_channel": envelope.get("source_channel"),
            "native_message_id": source_ref.get("native_message_id"),
            "msgtype": content.get("msgtype"),
        },
    }
    return verdict, exit_code


def _mutate(event: dict[str, Any], mutation: str) -> dict[str, Any]:
    import copy

    mutated = copy.deepcopy(event)
    medre = mutated["medre"]
    envelope = medre["envelope"]
    source = medre["metadata"]["bridge_context"]["source"]
    if mutation == "meshnet-as-channel":
        label = source["native_facts"]["fields"]["meshnet_label"]
        envelope["source_channel"] = label
        envelope["source_native_ref"]["native_channel_id"] = label
        source["channel"] = label
        source["native_ref"]["native_channel_id"] = label
    elif mutation == "origin-label-as-native-channel":
        envelope["source_native_ref"]["native_channel_id"] = source["origin_label"]
    elif mutation == "bool-schema-version":
        envelope["schema_version"] = True
    elif mutation == "no-sender-attribution":
        source["sender_id"] = ""
        source["sender_name"] = None
        source["sender_short_name"] = None
    elif mutation == "too-many-facts":
        fields = source["native_facts"]["fields"]
        for index in range(20):
            fields[f"pad{index:02d}"] = "x"
    elif mutation == "overlong-fact-value":
        source["native_facts"]["fields"]["meshnet_label"] = "A" * 257
    elif mutation == "reaction-key-dropped-from-body":
        hints = medre["metadata"]["bridge_context"]["relations"]
        key = hints[0]["key"]
        mutated["body"] = mutated["body"].replace(key, "")
    elif mutation == "reaction-degraded-empty-quote":
        # The honest-degradation defect class (renderer 614372e): an
        # unresolvable-target described reaction quoting an EMPTY string
        # instead of the honest wording.
        mutated["body"] = mutated["body"].replace("to an earlier message", 'to ""')
    elif mutation == "reaction-flat-meshtastic-field":
        mutated["meshtastic_portnum"] = 1
    elif mutation == "leading-newline-body":
        mutated["body"] = "\n " + mutated["body"]
    elif mutation == "reaction-emote-no-leading-newline":
        # Owner-ruling defect class: the reaction emote fallback body lost
        # its single intentional leading newline.
        mutated["body"] = mutated["body"].lstrip("\n")
    elif mutation == "reaction-emote-two-leading-newlines":
        # Owner-ruling defect class: a doubled leading newline on the
        # reaction emote fallback body (stray-prefix accumulation).
        mutated["body"] = "\n\n" + mutated["body"].lstrip("\n")
    elif mutation == "context-id-mismatch":
        source["canonical_event_id"] = "00000000-0000-0000-0000-000000000000"
    elif mutation == "negative-packet-id":
        envelope["source_native_ref"]["native_message_id"] = "-5"
        source["native_ref"]["native_message_id"] = "-5"
    elif mutation == "empty-packet-id":
        envelope["source_native_ref"]["native_message_id"] = ""
        source["native_ref"]["native_message_id"] = ""
    elif mutation == "relation-info-incoherent":
        envelope["relation_info"] = "reply"
        envelope["event_kind"] = "message.created"
        medre["metadata"]["bridge_context"]["relations"][0][
            "relation_type"
        ] = "reaction"
    elif mutation == "no-native-facts":
        source.pop("native_facts")
    elif mutation == "two-relations":
        hints = medre["metadata"]["bridge_context"]["relations"]
        hints.append(copy.deepcopy(hints[0]))
    elif mutation == "meshnet-label-leak":
        label = source["native_facts"]["fields"]["meshnet_label"]
        source["sender_id"] = label
    elif mutation == "portnum-fact-out-of-range":
        source["native_facts"]["fields"]["portnum"] = 2**40
    elif mutation == "lxmf-channel-not-hash":
        # An addressed-transport channel must be the 32-hex delivery hash;
        # a meshnet label under an addressed origin is the r14 class again.
        envelope["source_channel"] = "BetaLab"
        source["channel"] = "BetaLab"
        source["native_ref"]["native_channel_id"] = None
        envelope["source_native_ref"]["native_channel_id"] = None
    elif mutation == "matrix-target-radio-channel":
        # A Matrix-native target id ($-prefixed) must scope to the room; a
        # numeric radio channel under a "$" id is incoherent.
        hints = medre["metadata"]["bridge_context"]["relations"]
        target = hints[0]["target_native_ref"]
        target["native_message_id"] = "$matrixtargeteventid"
        target["native_channel_id"] = "0"
    elif mutation == "chimeric-source-ref":
        # The matrix-target carve-out ($-id + room channel) is for
        # relation-hint TARGET refs only: a radio-origin SOURCE ref carrying
        # a $ Matrix id and a room-id channel (with the numeric source
        # channel untouched) is the chimeric class the carve-out must not
        # license. bridge_context.source.native_ref is mutated in lockstep
        # (the provenance-coherence check compares the two).
        envelope["source_native_ref"]["native_message_id"] = "$chimericmatrixid"
        envelope["source_native_ref"]["native_channel_id"] = "!chimeric:lab.test"
        source["native_ref"]["native_message_id"] = "$chimericmatrixid"
        source["native_ref"]["native_channel_id"] = "!chimeric:lab.test"
    elif mutation == "matrix-target-radio-adapter":
        # A matrix-native target ($ id, room-id channel) claiming the RADIO
        # source_adapter: only a matrix-capable adapter can own $ event ids,
        # so adapter == source_adapter under a $ id is incoherent.
        hints = medre["metadata"]["bridge_context"]["relations"]
        target = hints[0]["target_native_ref"]
        target["native_message_id"] = "$matrixownedeventid"
        target["native_channel_id"] = "!matrixtarget:lab.test"
        target["adapter"] = envelope["source_adapter"]
    else:  # pragma: no cover - selftest table drift
        raise SystemExit(f"unknown mutation {mutation}")
    return mutated


NEGATIVE_MUTATIONS = (
    "meshnet-as-channel",
    "origin-label-as-native-channel",
    "bool-schema-version",
    "no-sender-attribution",
    "too-many-facts",
    "overlong-fact-value",
    "reaction-key-dropped-from-body",
    "reaction-degraded-empty-quote",
    "reaction-flat-meshtastic-field",
    "leading-newline-body",
    "reaction-emote-no-leading-newline",
    "reaction-emote-two-leading-newlines",
    "context-id-mismatch",
    "negative-packet-id",
    "empty-packet-id",
    "relation-info-incoherent",
    "no-native-facts",
    "two-relations",
    "meshnet-label-leak",
    "portnum-fact-out-of-range",
    "matrix-target-radio-channel",
    "matrix-target-radio-adapter",
    "chimeric-source-ref",
    "lxmf-channel-not-hash",
)


def selftest(corpus: dict[str, Any]) -> tuple[dict[str, Any], int]:
    positives = {}
    negative_results = {}
    violations_found = 0
    for case in corpus.get("cases", []):
        example = case.get("example")
        if not isinstance(example, dict):
            positives[case.get("name", "?")] = "missing example"
            violations_found += 1
            continue
        violations, _ = validate_event(example, corpus)
        positives[case["name"]] = violations or "ok"
        violations_found += len(violations)

    examples = [
        case["example"]
        for case in corpus.get("cases", [])
        if isinstance(case.get("example"), dict)
    ]
    if not examples:
        violations_found += 1
    addressed_examples = [
        case["example"]
        for case in corpus.get("cases", [])
        if isinstance(case.get("example"), dict)
        and case.get("match", {}).get("channel_class") == "addressed"
    ]
    reacted_examples = [
        case["example"]
        for case in corpus.get("cases", [])
        if isinstance(case.get("example"), dict)
        and case.get("match", {}).get("event_kind") == "message.reacted"
    ]
    replied_examples = [
        case["example"]
        for case in corpus.get("cases", [])
        if isinstance(case.get("example"), dict)
        and case.get("match", {}).get("relation_info") == "reply"
    ]
    for mutation in NEGATIVE_MUTATIONS:
        # Reaction/relation mutations need an example carrying relations.
        reference = examples[0]
        if mutation.startswith("lxmf-"):
            reference = addressed_examples[0] if addressed_examples else examples[0]
        elif mutation in {
            "relation-info-incoherent",
            "two-relations",
            "matrix-target-radio-channel",
            "matrix-target-radio-adapter",
        }:
            reference = replied_examples[0] if replied_examples else examples[0]
        elif mutation == "reaction-degraded-empty-quote":
            # The degraded-quote mutation needs the unobserved-target
            # example (the only body carrying the honest wording).
            reference = next(
                (
                    example
                    for example in reacted_examples
                    if isinstance(example.get("body"), str)
                    and "an earlier message" in example.get("body", "")
                ),
                reacted_examples[0] if reacted_examples else examples[0],
            )
        elif mutation.startswith("reaction-") or mutation in {"meshnet-label-leak"}:
            # Body-level reaction mutations need an emote-fallback example
            # (a rendered body); the native m.reaction example carries none.
            reference = next(
                (
                    example
                    for example in reacted_examples
                    if isinstance(example.get("body"), str)
                ),
                reacted_examples[0] if reacted_examples else examples[0],
            )
        violations, _ = validate_event(_mutate(reference, mutation), corpus)
        negative_results[mutation] = violations or "NOT CAUGHT"
        if not violations:
            violations_found += 1

    report = {
        "ok": violations_found == 0,
        "positives": positives,
        "negatives": negative_results,
    }
    return report, (0 if violations_found == 0 else 1)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("event_json", help="event JSON, or '-' for stdin")
    validate.add_argument("--corpus", default=None)
    validate.add_argument(
        "--corpus-json",
        default=None,
        help="corpus JSON inline (for environments without the corpus mount)",
    )
    selftest_parser = sub.add_parser("selftest")
    selftest_parser.add_argument("--corpus", default=None)
    selftest_parser.add_argument("--corpus-json", default=None)
    args = parser.parse_args()

    corpus = load_corpus(args.corpus, getattr(args, "corpus_json", None))
    if args.command == "selftest":
        report, code = selftest(corpus)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return code

    raw = sys.stdin.read() if args.event_json == "-" else args.event_json
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "violations": [f"invalid event JSON: {exc}"]}))
        return 1
    require_schema = os.environ.get("MEDRE_CORPUS_REQUIRE_SCHEMA", "") == "1"
    verdict, code = _verdict(payload, corpus, require_schema)
    print(json.dumps(verdict, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
