"""Analysis verdicts for the normal-topology scenarios (scenario-normal-*.sh).

Runs INSIDE the lab-tools container via medre_lib_py. Every check is a
subcommand that takes JSON/argv values (NEVER env) and prints one JSON
verdict, so the bash scenarios stay thin and every judgment is
py_compile-verifiable. Values the scenarios interpolate are passed as
argv, never spliced into code.

Subcommands:
    nested-info TEXT
        Loop-signature detector: bracketed prefix tokens ([!hex/Label],
        [user/Label], relay prefixes) appearing more than once, or any
        known meshnet label appearing more than once, mean the text is a
        re-prefixed re-air (the r15 nested-prefix regression class).
    fanout-packets COLLECTOR_JSON MARKER EXPECTED_CHANNEL LIMIT LABEL...
        Judge one collecting sniffer's final result: exactly one marker
        packet on the expected channel (-1 = any channel), no nested
        prefixes, every marker packet within the radio byte limit.
    room-scan SCAN_JSON MARKER
        Classify a room scan for the marker: the single human origin vs
        bot-authored echoes (loop signature); bot echoes must be
        nested-prefix-free and carry the marker exactly once.
    hint-summary EVENT_JSON
        Extract the relation surfaces of one canonical event (Matrix
        m.in_reply_to + envelope bridge_context relation hints) as
        evidence for reply-target resolution.
    reaction-verdict MATCH_JSON MEDRE_BODY KEY LIMIT
        r15 revalidation verdict for a far-relay re-bridge of a peer
        reaction: exact key present, NO re-prefixed body (text equals the
        authored body, or is its radio-truncation), within the byte
        limit; reports the transport shape (plain send vs tapback). The
        medre reacted event must carry an authored body (described
        reaction delivery, matrix adapter #53): the retired native
        m.reaction shape (no body) fails the verdict.
    reaction-adoption EVENTS_JSON KEY
        Judge the medre bridge's stored handling of a trusted peer
        reacted event: the exact key must appear in an authoritative
        relation row or the stored unverified-observation namespace;
        authoritative identity surfaces must not carry a foreign key.
    medre-claim-verdict EVENTS_JSON CLAIM...
        Judge medre storage of a forged event: authoritative surfaces
        (identity columns, payload, body, native refs, relations) carry
        no forged claim; claims confined to the unverified-observations
        namespace are the documented contract.
    lxmf-ref EVENTS_JSON ADAPTER
        Report whether a canonical event carries an outbound native ref
        on the given (lxmf) adapter.
    parity-agree EVENT_A_JSON EVENT_B_JSON
        Metadata agreement for two canonical events claiming the SAME
        native packet: channel, packet id, sender attribution.
    queue-parse  (stdin: log lines)
        Parse "Queued message (N/500)" lines -> {count, max_pos}.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

_BRACKET_TOKEN = re.compile(r"\[([^\[\]]+)\]")
_LIMIT_TOKENS = 4  # bounded evidence in verdicts


def _load(json_or_dash: str) -> Any:
    """Parse a JSON argument; '-' reads the piped stdin instead."""
    if json_or_dash == "-":
        return json.loads(sys.stdin.read())
    return json.loads(json_or_dash)


def _events(payload: Any) -> list[dict[str, Any]]:
    """Accept medre_maps.py output ({events: [...]}) or a bare list."""
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    if isinstance(payload, dict):
        events = payload.get("events")
        if isinstance(events, list):
            return [event for event in events if isinstance(event, dict)]
    return []


def _hints(content: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = content.get("medre")
    if not isinstance(metadata, dict):
        return []
    md = metadata.get("metadata")
    if not isinstance(md, dict):
        return []
    context = md.get("bridge_context")
    if not isinstance(context, dict):
        return []
    relations = context.get("relations")
    return [hint for hint in relations if isinstance(hint, dict)] if isinstance(
        relations, list
    ) else []


def _nested_problems(text: str, labels: tuple[str, ...]) -> list[str]:
    """The r15 nested-prefix signature: a body re-prefixed by a relay."""
    problems: list[str] = []
    tokens = _BRACKET_TOKEN.findall(text)
    repeated = sorted({token for token in tokens if tokens.count(token) > 1})
    if repeated:
        problems.append(f"repeated bracketed prefix tokens: {repeated[:_LIMIT_TOKENS]}")
    if len(tokens) > 1:
        problems.append(f"multiple bracketed prefix tokens: {tokens[:_LIMIT_TOKENS]}")
    for label in labels:
        if label and text.count(label) > 1:
            problems.append(f"meshnet label {label!r} appears {text.count(label)}x")
    return problems


def cmd_nested_info(text: str) -> dict[str, Any]:
    tokens = _BRACKET_TOKEN.findall(text)
    return {"tokens": tokens[:_LIMIT_TOKENS], "nested": bool(_nested_problems(text, ()))}


def cmd_fanout_packets(
    collector_json: str, marker: str, expected_channel: int, limit: int
) -> dict[str, Any]:
    payload = _load(collector_json)
    packets = [p for p in payload.get("packets", []) if isinstance(p, dict)]
    marker_packets = [p for p in packets if marker in (p.get("text") or "")]
    problems: list[str] = []
    if expected_channel >= 0:
        wrong = [
            p for p in marker_packets
            if str(p.get("channel")) != str(expected_channel)
        ]
        if wrong:
            problems.append(
                f"{len(wrong)} marker packet(s) on channel {wrong[0].get('channel')!r},"
                f" expected {expected_channel}"
            )
    nested: list[dict[str, Any]] = []
    overlimit: list[dict[str, Any]] = []
    for packet in marker_packets:
        text = packet.get("text") or ""
        nested_problems = _nested_problems(text, ())
        if nested_problems:
            nested.append({"packet_id": packet.get("packet_id"), "problems": nested_problems})
        size = len(text.encode("utf-8"))
        if size > limit:
            overlimit.append({"packet_id": packet.get("packet_id"), "bytes": size})
    if len(marker_packets) != 1:
        problems.append(
            f"marker aired {len(marker_packets)}x on the sniffed mesh (expected exactly"
            " once); packet_ids="
            f"{[p.get('packet_id') for p in marker_packets][:10]}"
        )
    if nested:
        problems.append(f"nested-prefix re-air captured: {json.dumps(nested[:3])}")
    if overlimit:
        problems.append(f"radio byte limit exceeded: {json.dumps(overlimit[:3])}")
    return {
        "ok": not problems,
        "problems": problems,
        "count": len(marker_packets),
        "seen_total": payload.get("seen_total"),
        "packet_ids": [p.get("packet_id") for p in marker_packets][:10],
    }


def _human_mxids() -> set[str]:
    """The rig's provisioned human senders, derived from the HUMAN_* env the
    suite publishes (never hardcoded localparts/homeserver)."""
    server = os.environ.get("LAB_SERVER_NAME") or "lab.test"
    return {
        f"@{(os.environ.get('HUMAN_ALICE_LOCALPART') or 'alice')}:{server}",
        f"@{(os.environ.get('HUMAN_BOB_LOCALPART') or 'bob')}:{server}",
    }


def cmd_room_scan(scan_json: str, marker: str) -> dict[str, Any]:
    payload = _load(scan_json)
    matches = payload.get("matches", []) if isinstance(payload, dict) else []
    problems: list[str] = []
    bot_events: list[dict[str, Any]] = []
    human_mxids = _human_mxids()
    human = 0
    for match in matches:
        if not isinstance(match, dict):
            continue
        body = match.get("body") or ""
        if body.count(marker) != 1:
            problems.append(
                f"event {match.get('event_id')} carries the marker {body.count(marker)}x"
            )
        sender = match.get("sender") or ""
        if sender.startswith("@") and sender not in human_mxids:
            nested = _nested_problems(body, ())
            bot_events.append(
                {
                    "event_id": match.get("event_id"),
                    "sender": sender,
                    "nested": nested,
                }
            )
        else:
            human += 1
    for event in bot_events:
        if event["nested"]:
            problems.append(
                f"bot-authored echo {event['event_id']} from {event['sender']} is"
                f" nested-prefixed: {event['nested']}"
            )
    if human != 1:
        problems.append(
            f"expected exactly one human-origin event carrying the marker,"
            f" got {human} (a scan window with 0 loses the origin; >1 means a"
            " duplicated delivery)"
        )
    return {
        "ok": not problems,
        "problems": problems,
        "human_events": human,
        "bot_echoes": bot_events[:_LIMIT_TOKENS],
        "total": len(matches),
    }


def cmd_hint_summary(event_json: str) -> dict[str, Any]:
    payload = _load(event_json)
    event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, dict):
        event = payload if isinstance(payload, dict) else {}
    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    relates_to = content.get("m.relates_to")
    in_reply_to = None
    if isinstance(relates_to, dict):
        reply = relates_to.get("m.in_reply_to")
        if isinstance(reply, dict):
            in_reply_to = reply.get("event_id")
    hints = [
        {
            "relation_type": hint.get("relation_type"),
            "target_adapter": (hint.get("target_native_ref") or {}).get("adapter")
            if isinstance(hint.get("target_native_ref"), dict)
            else None,
            "target_channel": (hint.get("target_native_ref") or {}).get("native_channel_id")
            if isinstance(hint.get("target_native_ref"), dict)
            else None,
            "target_msg_id": (hint.get("target_native_ref") or {}).get("native_message_id")
            if isinstance(hint.get("target_native_ref"), dict)
            else None,
            "key": hint.get("key"),
        }
        for hint in _hints(content)
    ]
    return {"in_reply_to": in_reply_to, "hints": hints}


def cmd_reaction_verdict(
    match_json: str, medre_body: str, key: str, limit: int
) -> dict[str, Any]:
    payload = _load(match_json)
    view = payload.get("match") or {}
    text = view.get("text") or ""
    reply_id = view.get("reply_id")
    emoji_flag = view.get("emoji")
    problems: list[str] = []
    authored = (medre_body or "").strip()
    # Defense-in-depth (2026-11-11 review): the no-re-prefix credit below
    # compares the sniffed body against medre's AUTHORED body. If medre's
    # own renderer re-prefixed its body, wire-equality would hold and the
    # r15 regression would pass silently - so the authored body itself must
    # not repeat a prefix token. (A single token is the legitimate mesh
    # attribution; a distinct token inside the quoted preview is the tapped
    # text, not a re-prefix.)
    authored_tokens = _BRACKET_TOKEN.findall(authored)
    repeated_tokens = sorted(
        {token for token in authored_tokens if authored_tokens.count(token) > 1}
    )
    if repeated_tokens:
        problems.append(
            "medre's authored reacted body already carries the re-prefix"
            " signature (repeated prefix tokens"
            f" {repeated_tokens[:_LIMIT_TOKENS]}): wire-equality must not be"
            " credited for it"
        )
    shape = "tapback" if (reply_id is not None or emoji_flag) else "plain"
    if key not in text and not emoji_flag:
        problems.append(f"exact key {key!r} missing from the transmitted text")
    size = len(text.encode("utf-8"))
    if size > limit:
        problems.append(
            f"{size} UTF-8 bytes exceeds the radio limit {limit}"
            " (r15 byte-limit regression)"
        )
    # Described reaction delivery (matrix adapter #53) renders EVERY relayed
    # reaction as an m.emote message with an authored body, so the medre
    # reacted event always yields verbatim-comparable text. The retired
    # native-m.reaction tolerance branches (authored-empty nested-token
    # heuristics) were removed at the 2026-11-13 refresh: a reacted event
    # without an authored body is the dead shape and fails here. The authored
    # Matrix body legitimately carries the owner-ruled EXACTLY-ONE leading
    # newline (emote presentation); the wire copy may carry it verbatim or
    # compose the radio-side text without it, so the verbatim comparison is
    # whitespace-exact-aware (a re-prefix adds a distinct token, never mere
    # whitespace, and stays caught).
    wire_text = text.strip()
    if not authored:
        problems.append(
            "medre's reacted event carries no authored body (retired native"
            " m.reaction shape): described reaction delivery always renders"
            " a verbatim-comparable m.emote body"
        )
    elif shape == "plain":
        truncated_ok = (
            len(text.encode("utf-8")) <= limit
            and wire_text
            and authored.startswith(wire_text)
        )
        if wire_text != authored and not truncated_ok:
            problems.append(
                "far relay re-prefixed or altered the authored peer body"
                " (r15 no-re-prefix regression):"
                f" sniffed={text!r} medre_body={authored!r}"
            )
    else:
        # Structured reply/tapback shapes: a pure radio tapback carries the
        # exact key as payload; the described-reaction path sends the peer's
        # authored body as the reply text, which must arrive VERBATIM (the
        # r15 no-re-prefix contract applies to both transports).
        if wire_text != key:
            truncated_ok = (
                len(text.encode("utf-8")) <= limit
                and wire_text
                and authored.startswith(wire_text)
            )
            if wire_text != authored and not truncated_ok:
                problems.append(
                    "far relay re-prefixed or altered the authored peer body"
                    " (r15 no-re-prefix regression):"
                    f" sniffed={text!r} medre_body={authored!r}"
                )
    return {
        "ok": not problems,
        "problems": problems,
        "shape": shape,
        "reply_id": reply_id,
        "bytes": size,
        "sniffed": text,
        "medre_body": authored,
    }


def _authoritative_blob(event: dict[str, Any]) -> str:
    authoritative = {
        "source_adapter": event.get("source_adapter"),
        "source_transport_id": event.get("source_transport_id"),
        "source_channel_id": event.get("source_channel_id"),
        "payload": event.get("payload"),
        "body_content": event.get("body_content"),
        "native_refs": [
            {k: ref.get(k) for k in ("adapter", "native_channel_id", "native_message_id", "direction")}
            for ref in event.get("native_refs", [])
        ],
        "relations": [
            {k: rel.get(k) for k in ("relation_type", "target_native_adapter", "target_native_message_id", "key")}
            for rel in event.get("relations", [])
        ],
    }
    return json.dumps(authoritative, ensure_ascii=False)


def cmd_reaction_adoption(events_json: str, key: str) -> dict[str, Any]:
    events = _events(_load(events_json))
    problems: list[str] = []
    relations_found: list[dict[str, Any]] = []
    observation_found = False
    if not events:
        return {
            "ok": False,
            "problems": ["medre state holds no canonical event for the peer reacted event"],
            "count": 0,
        }
    for event in events:
        event_id = event.get("event_id")
        for relation in event.get("relations", []):
            if relation.get("key") == key:
                relations_found.append({"event": event_id, "key": relation.get("key")})
        blob = json.dumps(event.get("payload"), ensure_ascii=False) + json.dumps(
            event.get("body_content"), ensure_ascii=False
        )
        if key in blob:
            observation_found = True
        authoritative = _authoritative_blob(event)
        # The key itself is legitimate in relations; nothing else about the
        # reaction may be adopted beyond the key and the real sender.
        for relation in event.get("relations", []):
            if relation.get("relation_type") == "reaction" and relation.get("key") != key:
                problems.append(
                    f"event {event_id}: authoritative reaction relation carries key"
                    f" {relation.get('key')!r} != {key!r}"
                )
        if "reacted" in authoritative and key not in authoritative and not relations_found:
            problems.append(
                f"event {event_id}: authoritative surfaces render a reaction without"
                f" the exact key {key!r}"
            )
    ok = bool(relations_found) or observation_found
    if not ok:
        problems.append(
            f"exact key {key!r} found in neither authoritative relation rows nor"
            " stored observations (no adoption of the trusted peer reaction)"
        )
    return {
        "ok": not problems,
        "problems": problems,
        "relations": relations_found[:_LIMIT_TOKENS],
        "observation_found": observation_found,
        "count": len(events),
    }


def cmd_medre_claim_verdict(events_json: str, claims: list[str]) -> dict[str, Any]:
    events = _events(_load(events_json))
    problems: list[str] = []
    for event in events:
        event_id = event.get("event_id")
        blob = _authoritative_blob(event)
        for claim in claims:
            if claim in blob:
                problems.append(
                    f"event {event_id} carries forged claim {claim} in an"
                    " authoritative surface"
                )
        if event.get("source_transport_id") not in ("@untrusted:lab.test", None, ""):
            problems.append(
                f"event {event_id}: source_transport_id={event.get('source_transport_id')!r}"
                " - real sender not preserved"
            )
    return {"ok": not problems, "problems": problems, "count": len(events)}


def cmd_lxmf_ref(events_json: str, adapter: str) -> dict[str, Any]:
    events = _events(_load(events_json))
    found: list[dict[str, Any]] = []
    adapters: list[str] = []
    for event in events:
        for ref in event.get("native_refs", []):
            ref_adapter = ref.get("adapter")
            if ref_adapter and ref_adapter not in adapters:
                adapters.append(ref_adapter)
            if ref.get("direction") == "outbound" and ref_adapter == adapter:
                found.append(ref)
    return {"found": bool(found), "refs": found[:_LIMIT_TOKENS], "ref_adapters": adapters}


def _event_core(payload: Any) -> dict[str, Any]:
    event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, dict):
        event = payload if isinstance(payload, dict) else {}
    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    medre = content.get("medre")
    envelope = medre.get("envelope") if isinstance(medre, dict) else {}
    metadata = medre.get("metadata") if isinstance(medre, dict) else None
    context = metadata.get("bridge_context") if isinstance(metadata, dict) else None
    source = context.get("source") if isinstance(context, dict) else {}
    ref = envelope.get("source_native_ref") if isinstance(envelope, dict) else {}
    return {
        "event_id": event.get("event_id"),
        "channel": envelope.get("source_channel"),
        "packet_id": ref.get("native_message_id") if isinstance(ref, dict) else None,
        "sender_id": source.get("sender_id"),
        "origin_label": source.get("origin_label"),
        "meshnet_label": (source.get("native_facts") or {}).get("fields", {}).get("meshnet_label")
        if isinstance(source.get("native_facts"), dict)
        else None,
    }


def cmd_parity_agree(event_a_json: str, event_b_json: str) -> dict[str, Any]:
    a = _event_core(_load(event_a_json))
    b = _event_core(_load(event_b_json))
    problems: list[str] = []
    if str(a.get("channel")) != str(b.get("channel")):
        problems.append(f"channel disagreement: {a.get('channel')!r} != {b.get('channel')!r}")
    if str(a.get("packet_id")) != str(b.get("packet_id")):
        problems.append(
            f"packet id disagreement: {a.get('packet_id')!r} != {b.get('packet_id')!r}"
        )
    if str(a.get("sender_id")) != str(b.get("sender_id")):
        problems.append(
            f"sender attribution disagreement: {a.get('sender_id')!r} != {b.get('sender_id')!r}"
        )
    return {"ok": not problems, "problems": problems, "a": a, "b": b}


def cmd_queue_parse() -> dict[str, Any]:
    queued = re.compile(r"Queued message \((\d+)/\d+\)")
    count = 0
    max_pos = 0
    for line in sys.stdin:
        match = queued.search(line)
        if match:
            count += 1
            max_pos = max(max_pos, int(match.group(1)))
    return {"count": count, "max_pos": max_pos}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("nested-info"); p.add_argument("text")
    p = sub.add_parser("fanout-packets")
    p.add_argument("collector_json")
    p.add_argument("marker")
    p.add_argument("expected_channel", type=int)
    p.add_argument("limit", type=int)
    p = sub.add_parser("room-scan"); p.add_argument("scan_json"); p.add_argument("marker")
    p = sub.add_parser("hint-summary"); p.add_argument("event_json")
    p = sub.add_parser("reaction-verdict")
    p.add_argument("match_json"); p.add_argument("medre_body"); p.add_argument("key")
    p.add_argument("limit", type=int)
    p = sub.add_parser("reaction-adoption"); p.add_argument("events_json"); p.add_argument("key")
    p = sub.add_parser("medre-claim-verdict")
    p.add_argument("events_json"); p.add_argument("claims", nargs="+")
    p = sub.add_parser("lxmf-ref"); p.add_argument("events_json"); p.add_argument("adapter")
    p = sub.add_parser("parity-agree"); p.add_argument("event_a_json"); p.add_argument("event_b_json")
    sub.add_parser("queue-parse")

    args = parser.parse_args()
    if args.command == "nested-info":
        print(json.dumps(cmd_nested_info(args.text), ensure_ascii=False))
        return 0
    if args.command == "fanout-packets":
        print(json.dumps(cmd_fanout_packets(args.collector_json, args.marker, args.expected_channel, args.limit), ensure_ascii=False))
        return 0
    if args.command == "room-scan":
        print(json.dumps(cmd_room_scan(args.scan_json, args.marker), ensure_ascii=False))
        return 0
    if args.command == "hint-summary":
        print(json.dumps(cmd_hint_summary(args.event_json), ensure_ascii=False))
        return 0
    if args.command == "reaction-verdict":
        print(json.dumps(cmd_reaction_verdict(args.match_json, args.medre_body, args.key, args.limit), ensure_ascii=False))
        return 0
    if args.command == "reaction-adoption":
        print(json.dumps(cmd_reaction_adoption(args.events_json, args.key), ensure_ascii=False))
        return 0
    if args.command == "medre-claim-verdict":
        print(json.dumps(cmd_medre_claim_verdict(args.events_json, args.claims), ensure_ascii=False))
        return 0
    if args.command == "lxmf-ref":
        print(json.dumps(cmd_lxmf_ref(args.events_json, args.adapter), ensure_ascii=False))
        return 0
    if args.command == "parity-agree":
        print(json.dumps(cmd_parity_agree(args.event_a_json, args.event_b_json), ensure_ascii=False))
        return 0
    if args.command == "queue-parse":
        print(json.dumps(cmd_queue_parse()))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
