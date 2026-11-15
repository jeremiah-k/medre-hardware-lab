"""Matrix event polling for scenario assertions.

Runs INSIDE the lab-tools container. Polls the client-server /messages
endpoint (stateless, no sync token juggling) and applies contract-aware
filters, including MEDRE envelope inspection:

    python3 mx_events.py wait ROOM_ID --sender '@relay-a:lab.test' \
        --body-contains MARKER [--envelope-kind message.created] \
        [--relation-info reply] [--relates-to '$abc'] \
        [--native-ref-msg-id 1234] [--native-ref-channel '0'] \
        [--msgtype m.text] [--timeout 60] [--since-ms 1690000000000]

Prints the matched event JSON on success; exits 1 with a JSON error listing
the most recent candidate events on timeout.

    python3 mx_events.py fetch ROOM_ID EVENT_ID   # one-shot GET by id
    python3 mx_events.py scan ROOM_ID --since-ms T --sender S --body-contains X
        [--limit 500]                            # list all matches (no wait)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from typing import Any

from lab_env import matrix_request, driver_token


def _envelope(content: dict[str, Any]) -> dict[str, Any]:
    medre = content.get("medre")
    if not isinstance(medre, dict):
        return {}
    envelope = medre.get("envelope")
    return envelope if isinstance(envelope, dict) else {}


def _bridge_context(content: dict[str, Any]) -> dict[str, Any]:
    medre = content.get("medre")
    if not isinstance(medre, dict):
        return {}
    metadata = medre.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    context = metadata.get("bridge_context")
    return context if isinstance(context, dict) else {}


def _relation_hints(content: dict[str, Any]) -> list[dict[str, Any]]:
    context = _bridge_context(content)
    hints = context.get("relations")
    if not isinstance(hints, list):
        return []
    return [hint for hint in hints if isinstance(hint, dict)]


def event_matches(event: dict[str, Any], filters: dict[str, Any]) -> bool:
    if filters.get("sender") and event.get("sender") != filters["sender"]:
        return False
    if filters.get("event_type") and event.get("type") != filters["event_type"]:
        return False
    content = event.get("content")
    if not isinstance(content, dict):
        return False

    body = content.get("body")
    if filters.get("body_contains") is not None:
        if not isinstance(body, str) or filters["body_contains"] not in body:
            return False
    if filters.get("body_equals") is not None and body != filters["body_equals"]:
        return False
    if filters.get("msgtype") and content.get("msgtype") != filters["msgtype"]:
        return False

    relates_to = content.get("m.relates_to")
    if filters.get("relates_to") is not None:
        target = None
        if isinstance(relates_to, dict):
            reply = relates_to.get("m.in_reply_to")
            if isinstance(reply, dict) and isinstance(reply.get("event_id"), str):
                target = reply["event_id"]
            elif isinstance(relates_to.get("event_id"), str):
                target = relates_to["event_id"]
        if target != filters["relates_to"]:
            return False
    if filters.get("no_relation") and isinstance(relates_to, dict) and relates_to:
        return False

    envelope = _envelope(content)
    if filters.get("envelope_kind") and envelope.get("event_kind") != filters["envelope_kind"]:
        return False
    if filters.get("relation_info") is not None and envelope.get("relation_info") != filters["relation_info"]:
        return False
    if filters.get("source_channel") is not None and envelope.get("source_channel") != filters["source_channel"]:
        return False
    if filters.get("require_envelope"):
        if not envelope:
            return False
        source_ref = envelope.get("source_native_ref")
        if filters.get("native_ref_msg_id") is not None:
            if not isinstance(source_ref, dict) or str(source_ref.get("native_message_id")) != str(
                filters["native_ref_msg_id"]
            ):
                return False
        if filters.get("native_ref_channel") is not None:
            if not isinstance(source_ref, dict) or str(
                source_ref.get("native_channel_id")
            ) != str(filters["native_ref_channel"]):
                return False
        if filters.get("native_ref_is_numeric"):
            if not isinstance(source_ref, dict):
                return False
            channel = source_ref.get("native_channel_id")
            message_id = source_ref.get("native_message_id")
            if not (isinstance(channel, str) and channel.isdigit()):
                return False
            if not (isinstance(message_id, str) and message_id.isdigit()):
                return False
    if filters.get("hint_target_msg_id") is not None:
        wanted = str(filters["hint_target_msg_id"])
        hints = _relation_hints(content)
        if not any(
            isinstance(hint.get("target_native_ref"), dict)
            and str(hint["target_native_ref"].get("native_message_id")) == wanted
            for hint in hints
        ):
            return False
    if filters.get("hint_key_equals") is not None:
        hints = _relation_hints(content)
        if not any(hint.get("key") == filters["hint_key_equals"] for hint in hints):
            return False
    return True


def _recent_events(token: str, room_id: str, limit: int) -> list[dict[str, Any]]:
    quoted = urllib.parse.quote(room_id, safe="")
    response = matrix_request(
        "GET",
        f"/_matrix/client/v3/rooms/{quoted}/messages",
        token,
        params={"dir": "b", "limit": str(limit)},
        timeout=30,
    )
    if response.status_code >= 400:
        raise SystemExit(f"/messages failed: {response.status_code} {response.text[:300]}")
    chunk = response.json().get("chunk", [])
    return [event for event in chunk if isinstance(event, dict)]


def _summarize(event: dict[str, Any]) -> dict[str, Any]:
    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    envelope = _envelope(content)
    source_ref = envelope.get("source_native_ref")
    return {
        "event_id": event.get("event_id"),
        "sender": event.get("sender"),
        "type": event.get("type"),
        "msgtype": content.get("msgtype"),
        "body": (content.get("body") or "")[:120],
        "envelope_kind": envelope.get("event_kind"),
        "source_channel": envelope.get("source_channel"),
        "relation_info": envelope.get("relation_info"),
        "source_native_ref_native_message_id": (
            source_ref.get("native_message_id") if isinstance(source_ref, dict) else None
        ),
        "source_native_ref_native_channel_id": (
            source_ref.get("native_channel_id") if isinstance(source_ref, dict) else None
        ),
        "origin_server_ts": event.get("origin_server_ts"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_filters(sub_parser: argparse.ArgumentParser) -> None:
        sub_parser.add_argument("--sender")
        sub_parser.add_argument("--event-type")
        sub_parser.add_argument("--body-contains")
        sub_parser.add_argument("--body-equals")
        sub_parser.add_argument("--msgtype")
        sub_parser.add_argument("--relates-to")
        sub_parser.add_argument("--no-relation", action="store_true")
        sub_parser.add_argument("--envelope-kind")
        sub_parser.add_argument("--relation-info")
        sub_parser.add_argument("--source-channel")
        sub_parser.add_argument("--require-envelope", action="store_true")
        sub_parser.add_argument("--native-ref-msg-id")
        sub_parser.add_argument("--native-ref-channel")
        sub_parser.add_argument("--native-ref-is-numeric", action="store_true")
        sub_parser.add_argument("--hint-target-msg-id")
        sub_parser.add_argument("--hint-key-equals")

    wait = sub.add_parser("wait")
    wait.add_argument("room_id")
    wait.add_argument("--timeout", type=float, default=float(__import__("os").environ.get("MATRIX_WAIT_SECONDS", "90")))
    wait.add_argument("--since-ms", type=int, default=0)
    _add_filters(wait)

    scan = sub.add_parser("scan")
    scan.add_argument("room_id")
    scan.add_argument("--limit", type=int, default=500)
    scan.add_argument("--since-ms", type=int, default=0)
    _add_filters(scan)

    fetch = sub.add_parser("fetch")
    fetch.add_argument("room_id")
    fetch.add_argument("event_id")
    fetch.add_argument("--timeout", type=float, default=30)

    args = parser.parse_args()
    token = driver_token()

    if args.command == "fetch":
        deadline = time.monotonic() + args.timeout
        quoted_room = urllib.parse.quote(args.room_id, safe="")
        quoted_event = urllib.parse.quote(args.event_id, safe="")
        last = ""
        while time.monotonic() < deadline:
            response = matrix_request(
                "GET",
                f"/_matrix/client/v3/rooms/{quoted_room}/event/{quoted_event}",
                token,
                timeout=20,
            )
            if response.status_code == 200:
                print(json.dumps(response.json()))
                return 0
            last = f"{response.status_code} {response.text[:200]}"
            time.sleep(1)
        print(json.dumps({"error": f"event {args.event_id} not found", "last": last}))
        return 1

    filters = {
        key: value
        for key, value in vars(args).items()
        if key not in {"command", "room_id", "timeout", "since_ms", "limit"}
    }

    if args.command == "wait":
        deadline = time.monotonic() + args.timeout
        recent: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            for event in _recent_events(token, args.room_id, 200):
                if args.since_ms and (event.get("origin_server_ts") or 0) < args.since_ms:
                    continue
                summary = _summarize(event)
                if summary["event_id"] and all(
                    candidate.get("event_id") != summary["event_id"] for candidate in recent
                ):
                    recent.append(summary)
                    recent = recent[-12:]
                if event_matches(event, filters):
                    print(json.dumps({"event": event, "candidates": recent[-6:]}))
                    return 0
            time.sleep(1.5)
        print(
            json.dumps(
                {
                    "error": "timeout waiting for Matrix event",
                    "filters": {k: v for k, v in filters.items() if v},
                    "recent": recent,
                },
                ensure_ascii=False,
            )
        )
        return 1

    # scan
    matches = []
    for event in _recent_events(token, args.room_id, args.limit):
        if args.since_ms and (event.get("origin_server_ts") or 0) < args.since_ms:
            continue
        if event_matches(event, filters):
            matches.append(_summarize(event))
    print(json.dumps({"matches": matches}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
