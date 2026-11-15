"""Shared environment resolution for scenario helper scripts.

Runs INSIDE the lab-tools container. Every scenario helper imports this
module first: it resolves the provisioned runtime (accounts, tokens, rooms)
and the contract configuration supplied through the environment (populated
from scenarios/config.env by the runner).

Print nothing on import; helpers return structured values and the callers
print the evidence they checked.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "http://synapse:8008")
SERVER_NAME = os.environ.get("LAB_SERVER_NAME", "lab.test")
RUNTIME_JSON = os.environ.get("LAB_RUNTIME_JSON", "/lab-state/runtime.json")

BOT_A_MXID = os.environ.get("BOT_A_MXID", "@relay-a:lab.test")
BOT_B_MXID = os.environ.get("BOT_B_MXID", "@relay-b:lab.test")
BOT_C_MXID = os.environ.get("BOT_C_MXID", "@relay-c:lab.test")
UNTRUSTED_LOCALPART = os.environ.get("UNTRUSTED_LOCALPART", "untrusted")
DRIVER_LOCALPART = os.environ.get("DRIVER_LOCALPART", "admin")

MESHNET_A = os.environ.get("MESHNET_A", "AlphaLab")
MESHNET_B = os.environ.get("MESHNET_B", "BetaLab")

MAP_DB_A = os.environ.get("MAP_DB_A", "/state/mmrelay-a/database/meshtastic.sqlite")
MAP_DB_B = os.environ.get("MAP_DB_B", "/state/mmrelay-b/database/meshtastic.sqlite")
MAP_DB_C = os.environ.get("MAP_DB_C", "/state/mmrelay-c/database/meshtastic.sqlite")

PLUGIN_OBS_A = os.environ.get("PLUGIN_OBS_A", "")
PLUGIN_OBS_B = os.environ.get("PLUGIN_OBS_B", "")
PLUGIN_OBS_C = os.environ.get("PLUGIN_OBS_C", "")

MATRIX_WAIT = float(os.environ.get("MATRIX_WAIT_SECONDS", "90"))
RADIO_WAIT = float(os.environ.get("RADIO_WAIT_SECONDS", "90"))


def _runtime() -> dict[str, Any]:
    with open(RUNTIME_JSON, encoding="utf-8") as handle:
        return json.load(handle)


def user_account(localpart: str) -> dict[str, Any]:
    """Return one provisioned account (user_id, access_token, ...)."""
    runtime = _runtime()
    account = runtime.get("users", {}).get(localpart)
    if not account or not account.get("access_token"):
        raise SystemExit(
            f"runtime.json has no usable account for {localpart!r}; "
            "run lab/tools/provision.py first"
        )
    return account


def driver_token() -> str:
    return str(user_account(DRIVER_LOCALPART)["access_token"])


def untrusted_token() -> str:
    return str(user_account(UNTRUSTED_LOCALPART)["access_token"])


def room_id(alias_localpart: str) -> str:
    """Resolve a room by its alias localpart (e.g. 'lab-bridge')."""
    runtime = _runtime()
    room = runtime.get("rooms", {}).get(alias_localpart)
    if room and room.get("room_id"):
        return str(room["room_id"])
    alias = f"#{alias_localpart}:{SERVER_NAME}"
    import urllib.parse

    quoted = urllib.parse.quote(alias, safe="")
    response = requests.get(
        f"{SYNAPSE_URL}/_matrix/client/v3/directory/room/{quoted}",
        headers={"Authorization": f"Bearer {driver_token()}"},
        timeout=20,
    )
    if response.status_code == 200:
        room_id_value = response.json().get("room_id")
        if isinstance(room_id_value, str):
            return room_id_value
    raise SystemExit(f"cannot resolve room alias {alias}")


def matrix_request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"}
    return requests.request(
        method,
        f"{SYNAPSE_URL}{path}",
        headers=headers,
        json=payload,
        params=params,
        timeout=timeout,
    )


def send_message(
    token: str,
    room_id_value: str,
    body: str,
    *,
    msgtype: str = "m.text",
    reply_to: str | None = None,
) -> str:
    """Send one m.room.message and return the created event id."""
    import time
    import urllib.parse

    content: dict[str, Any] = {"msgtype": msgtype, "body": body}
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    quoted_room = urllib.parse.quote(room_id_value, safe="")
    txn = f"scenario-{int(time.time() * 1000)}-{os.getpid()}"
    response = matrix_request(
        "PUT",
        f"/_matrix/client/v3/rooms/{quoted_room}/send/m.room.message/{txn}",
        token,
        content,
    )
    if response.status_code >= 400:
        raise SystemExit(f"send_message failed: {response.status_code} {response.text}")
    event_id = response.json().get("event_id")
    if not isinstance(event_id, str):
        raise SystemExit("send_message: response missing event_id")
    return event_id


def send_reaction(token: str, room_id_value: str, target: str, key: str) -> str:
    """Send one m.reaction annotation and return the created event id."""
    import time
    import urllib.parse

    content = {
        "m.relates_to": {
            "rel_type": "m.annotation",
            "event_id": target,
            "key": key,
        }
    }
    quoted_room = urllib.parse.quote(room_id_value, safe="")
    txn = f"scenario-react-{int(time.time() * 1000)}-{os.getpid()}"
    response = matrix_request(
        "PUT",
        f"/_matrix/client/v3/rooms/{quoted_room}/send/m.reaction/{txn}",
        token,
        content,
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"send_reaction failed: {response.status_code} {response.text}"
        )
    event_id = response.json().get("event_id")
    if not isinstance(event_id, str):
        raise SystemExit("send_reaction: response missing event_id")
    return event_id
