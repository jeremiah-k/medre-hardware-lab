"""Custom Matrix event sends for the r26 qualification probes.

Runs INSIDE the lab-tools container (plain REST via lab_env.matrix_request,
same contract as mx_user.py / mx_events.py). mx_user.py covers plain text,
replies, and reactions; the r26 probes need the event shapes those helpers
do not build:

    python3 mx_probe.py edit LOCALPART ROOM_ID TARGET NEW_BODY
        An m.replace EDIT of TARGET (m.new_content + m.relates_to), the
        standard Matrix edit wire shape.

    python3 mx_probe.py redact LOCALPART ROOM_ID TARGET [REASON]
        An m.room.redaction (Matrix DELETE) of TARGET.

    python3 mx_probe.py authored-react LOCALPART ROOM_ID TARGET KEY BODY
        An AUTHORED m.room.message (m.text, body=BODY) that CARRIES a
        reaction context (m.relates_to annotation KEY on TARGET) - the
        "authored message carrying reaction context" input shape.

    python3 mx_probe.py raw LOCALPART ROOM_ID EVENT_TYPE CONTENT_JSON
        Generic escape hatch: send any event type/content as LOCALPART.

Prints JSON {event_id, sender, kind} on success (kind names the probe shape).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from typing import Any

from lab_env import matrix_request
from mx_user import user_token


def _send(
    token: str,
    room_id: str,
    event_type: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    txn = f"probe-{time.time_ns()}"
    path = (
        "/_matrix/client/v3/rooms/"
        + urllib.parse.quote(room_id, safe="")
        + "/send/"
        + urllib.parse.quote(event_type, safe="")
        + "/"
        + txn
    )
    response = matrix_request("PUT", path, token, payload=content)
    if response.status_code // 100 != 2:
        print(
            json.dumps(
                {"error": "send failed", "status": response.status_code,
                 "body": response.text[:500]}
            )
        )
        sys.exit(1)
    event_id = response.json().get("event_id")
    return {"event_id": event_id, "kind": event_type, "content_keys": sorted(content)}


def _redact(
    token: str,
    room_id: str,
    target: str,
    reason: str,
) -> dict[str, Any]:
    """Matrix redactions go through the dedicated /redact endpoint."""
    txn = f"probe-{time.time_ns()}"
    path = (
        "/_matrix/client/v3/rooms/"
        + urllib.parse.quote(room_id, safe="")
        + "/redact/"
        + urllib.parse.quote(target, safe="")
        + "/"
        + txn
    )
    payload: dict[str, Any] = {"reason": reason} if reason else {}
    response = matrix_request("PUT", path, token, payload=payload)
    if response.status_code // 100 != 2:
        print(
            json.dumps(
                {"error": "redact failed", "status": response.status_code,
                 "body": response.text[:500]}
            )
        )
        sys.exit(1)
    event_id = response.json().get("event_id")
    return {
        "event_id": event_id,
        "kind": "m.room.redaction",
        "probe": "redact",
        "target": target,
        "content_keys": ["reason"] if reason else [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("edit", "redact", "authored-react", "raw"):
        p = sub.add_parser(name)
        p.add_argument("localpart")
        p.add_argument("room_id")
        if name == "edit":
            p.add_argument("target")
            p.add_argument("new_body")
        elif name == "redact":
            p.add_argument("target")
            p.add_argument("reason", nargs="?", default="")
        elif name == "authored-react":
            p.add_argument("target")
            p.add_argument("key")
            p.add_argument("body")
        else:
            p.add_argument("event_type")
            p.add_argument("content_json")

    args = parser.parse_args()
    token = user_token(args.localpart)

    if args.cmd == "edit":
        content = {
            "msgtype": "m.text",
            "body": f"* {args.new_body}",
            "m.new_content": {"msgtype": "m.text", "body": args.new_body},
            "m.relates_to": {"rel_type": "m.replace", "event_id": args.target},
        }
        result = _send(token, args.room_id, "m.room.message", content)
        result["probe"] = "edit"
        result["target"] = args.target
    elif args.cmd == "redact":
        result = _redact(token, args.room_id, args.target, args.reason)
    elif args.cmd == "authored-react":
        content = {
            "msgtype": "m.text",
            "body": args.body,
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": args.target,
                "key": args.key,
            },
        }
        result = _send(token, args.room_id, "m.room.message", content)
        result["probe"] = "authored-react"
        result["target"] = args.target
        result["key"] = args.key
    else:
        content = json.loads(args.content_json)
        result = _send(token, args.room_id, args.event_type, content)
        result["probe"] = "raw"

    print(json.dumps(result))


if __name__ == "__main__":
    main()
