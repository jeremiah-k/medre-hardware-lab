"""Send a blank Matrix reply (empty body + native m.in_reply_to relation).

Usage: python3 mx_blank_reply.py ROOM_ID TARGET_EVENT_ID MARKER
MARKER is embedded in the sender's transaction id only; the visible body is
the empty string. Prints JSON {event_id, target} - the caller captures the
blank reply's event id (the UNTRUSTED_EVENT_ID-style exact-id anchor).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse

from lab_env import driver_token, matrix_request


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: mx_blank_reply.py ROOM_ID TARGET_EVENT_ID MARKER", file=sys.stderr)
        return 2
    room_id_value, target, marker = sys.argv[1:4]

    content = {
        "msgtype": "m.text",
        "body": "",
        "m.relates_to": {"m.in_reply_to": {"event_id": target}},
    }
    quoted_room = urllib.parse.quote(room_id_value, safe="")
    txn = f"scenario-blank-{int(time.time() * 1000)}-{abs(hash(marker)) & 0xffffff}"
    response = matrix_request(
        "PUT",
        f"/_matrix/client/v3/rooms/{quoted_room}/send/m.room.message/{txn}",
        driver_token(),
        content,
    )
    if response.status_code >= 400:
        print(
            json.dumps(
                {"error": f"blank reply send failed: {response.status_code} {response.text[:300]}"}
            )
        )
        return 1
    event_id = response.json().get("event_id")
    if not isinstance(event_id, str) or not event_id:
        print(json.dumps({"error": "blank reply send response missing event_id"}))
        return 1
    print(json.dumps({"event_id": event_id, "target": target}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
