"""Human-user Matrix actions for the human-interactions scenario.

Runs INSIDE the lab-tools container. Plain REST against the client-server
API (no SDK). Tokens resolve once per call from the environment overrides
published by the rig (LAB_TOKEN_ALICE / LAB_TOKEN_BOB via
lab/state/scenario.env) or from runtime.json users.{localpart}.

Subcommands:
    token LOCALPART                       print the account's access token
    send LOCALPART ROOM_ID BODY [--emote]  send m.text (or m.emote)
    reply LOCALPART ROOM_ID TARGET BODY   send with m.in_reply_to relation
    react LOCALPART ROOM_ID TARGET KEY    send an m.annotation reaction
Prints JSON {event_id, sender, kind} on success.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse

from lab_env import matrix_request


def user_token(localpart: str) -> str:
    env_key = f"LAB_TOKEN_{localpart.upper()}"
    token = os.environ.get(env_key, "")
    if token:
        return token
    try:
        with open(
            os.environ.get("LAB_RUNTIME_JSON", "/lab-state/runtime.json"),
            encoding="utf-8",
        ) as handle:
            account = json.load(handle).get("users", {}).get(localpart, {})
    except FileNotFoundError:
        account = {}
    token = account.get("access_token") if isinstance(account, dict) else None
    if isinstance(token, str) and token:
        return token
    raise SystemExit(
        f"no token for human user {localpart!r}: set {env_key} in "
        "lab/state/scenario.env or provision the account into runtime.json"
    )


def _send(token: str, room_id: str, content: dict, kind: str) -> dict:
    quoted_room = urllib.parse.quote(room_id, safe="")
    txn = f"scenario-human-{kind}-{int(time.time() * 1000)}-{os.getpid()}"
    response = matrix_request(
        "PUT",
        f"/_matrix/client/v3/rooms/{quoted_room}/send/m.room.message/{txn}",
        token,
        content,
    )
    if response.status_code >= 400:
        print(
            json.dumps(
                {
                    "error": f"{kind} send failed: {response.status_code} {response.text[:300]}"
                }
            )
        )
        raise SystemExit(1)
    event_id = response.json().get("event_id")
    if not isinstance(event_id, str) or not event_id:
        print(json.dumps({"error": f"{kind} send response missing event_id"}))
        raise SystemExit(1)
    who = matrix_request("GET", "/_matrix/client/v3/account/whoami", token)
    sender = who.json().get("user_id") if who.status_code == 200 else None
    return {"event_id": event_id, "sender": sender, "kind": kind}


def send_message(token: str, room_id: str, body: str, emote: bool) -> dict:
    content = {"msgtype": "m.emote" if emote else "m.text", "body": body}
    return _send(token, room_id, content, "emote" if emote else "text")


def send_reply(token: str, room_id: str, target: str, body: str) -> dict:
    content = {
        "msgtype": "m.text",
        "body": body,
        "m.relates_to": {"m.in_reply_to": {"event_id": target}},
    }
    return _send(token, room_id, content, "reply")


def send_reaction(token: str, room_id: str, target: str, key: str) -> dict:
    content = {
        "m.relates_to": {"rel_type": "m.annotation", "event_id": target, "key": key}
    }
    quoted_room = urllib.parse.quote(room_id, safe="")
    txn = f"scenario-human-react-{int(time.time() * 1000)}-{os.getpid()}"
    response = matrix_request(
        "PUT",
        f"/_matrix/client/v3/rooms/{quoted_room}/send/m.reaction/{txn}",
        token,
        content,
    )
    if response.status_code >= 400:
        print(
            json.dumps(
                {
                    "error": f"reaction send failed: {response.status_code} {response.text[:300]}"
                }
            )
        )
        raise SystemExit(1)
    event_id = response.json().get("event_id")
    if not isinstance(event_id, str) or not event_id:
        print(json.dumps({"error": "reaction send response missing event_id"}))
        raise SystemExit(1)
    return {"event_id": event_id, "kind": "reaction"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("token")
    p.add_argument("localpart")
    p = sub.add_parser("send")
    p.add_argument("localpart")
    p.add_argument("room_id")
    p.add_argument("body")
    p.add_argument("--emote", action="store_true")
    p = sub.add_parser("reply")
    p.add_argument("localpart")
    p.add_argument("room_id")
    p.add_argument("target")
    p.add_argument("body")
    p = sub.add_parser("react")
    p.add_argument("localpart")
    p.add_argument("room_id")
    p.add_argument("target")
    p.add_argument("key")

    args = parser.parse_args()
    if args.command == "token":
        print(user_token(args.localpart))
        return 0

    token = user_token(args.localpart)
    if args.command == "send":
        print(
            json.dumps(
                send_message(token, args.room_id, args.body, args.emote),
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "reply":
        print(
            json.dumps(
                send_reply(token, args.room_id, args.target, args.body),
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "react":
        print(
            json.dumps(
                send_reaction(token, args.room_id, args.target, args.key),
                ensure_ascii=False,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
