"""Provision the interop-lab Synapse: accounts, rooms, runtime.json.

Idempotent: existing accounts are logged into (password-based), existing
room aliases are reused, memberships are ensured. Lifted from the upstream
CI provisioning pattern (shared-secret registration + createRoom + invite/join),
adapted for stable lab aliases.

Accounts (contract):
  admin     (server admin)    password lab-admin-pass
  relay-a   (bot A)           password lab-relay-a-pass
  relay-b   (bot B)           password lab-relay-b-pass
  relay-c   (bot C, parked)   password lab-relay-c-pass
  untrusted (human)           password lab-untrusted-pass
  alice     (human)           password lab-alice-pass
  bob       (human)           password lab-bob-pass
  medre     (medre bridge bot, medre profile only)
                              password lab-medre-pass

The medre bot is provisioned ONLY when LAB_MEDRE_PROFILE=1 is set in the
environment (lab/up.sh sets it under the `medre` compose profile). Without
the variable, provisioning is byte-identical to the pre-medre contract: no
medre user, no extra room memberships, no runtime.json changes.

Rooms (owner topology: one shared room per origin-meshnet x channel):
  #lab-alpha-ch0:lab.test    Alpha mesh channel 0 traffic
  #lab-alpha-ch2:lab.test    Alpha mesh channel 2 traffic
  #lab-beta-ch0:lab.test     Beta mesh channel 0 traffic
  #lab-beta-ch2:lab.test     Beta mesh channel 2 traffic
  #lab-bridge:lab.test       SINGLE shared surface, channel 0 (normal profile)
  #lab-bridge-ch2:lab.test   SINGLE shared surface, channel 2 (normal profile)
  #lab-bridge-e2ee:lab.test  encrypted stretch room (mapped nowhere by default)
  #lab-other:lab.test        mapped NOWHERE (clean cross-room-rejection)
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import sys
import time
from typing import Any

import requests
from lab_lib import (
    SERVER_NAME,
    SYNAPSE_URL,
    login,
    matrix_request,
    resolve_alias,
    runtime_path,
)

SHARED_SECRET = "medre-lab-shared-secret"

USERS: list[tuple[str, str, bool]] = [
    ("admin", "lab-admin-pass", True),
    ("relay-a", "lab-relay-a-pass", False),
    ("relay-b", "lab-relay-b-pass", False),
    ("relay-c", "lab-relay-c-pass", False),
    ("untrusted", "lab-untrusted-pass", False),
    ("alice", "lab-alice-pass", False),
    ("bob", "lab-bob-pass", False),
]

ROOMS: list[tuple[str, str, bool]] = [
    ("lab-alpha-ch0", "Interop Lab Alpha Mesh Ch0", False),
    ("lab-alpha-ch2", "Interop Lab Alpha Mesh Ch2", False),
    ("lab-beta-ch0", "Interop Lab Beta Mesh Ch0", False),
    ("lab-beta-ch2", "Interop Lab Beta Mesh Ch2", False),
    ("lab-bridge-e2ee", "Interop Lab Bridge E2EE", True),
    ("lab-other", "Interop Lab Other Room", False),
]

# MEDRE bridge bot (compose profiles `medre` + `normal`; up.sh sets
# LAB_MEDRE_PROFILE=1 whenever a medre instance is being brought up). Gated
# on the environment so without-profile provisioning stays byte-identical to
# the contract above.
MEDRE_PROFILE = os.environ.get("LAB_MEDRE_PROFILE", "") == "1"
# NORMAL-deployment profile (compose profile `normal`). Gates the two shared
# bridge rooms: they are created ONLY on --normal bring-ups so the stress and
# default bring-ups stay byte-identical to their recorded contract. The rooms
# persist on the homeserver once created, so switching back and forth is safe.
NORMAL_PROFILE = os.environ.get("LAB_NORMAL_PROFILE", "") == "1"
if MEDRE_PROFILE:
    USERS.append(("medre", "lab-medre-pass", False))
if NORMAL_PROFILE:
    ROOMS = ROOMS + [
        ("lab-bridge", "Interop Lab Shared Bridge Ch0", False),
        ("lab-bridge-ch2", "Interop Lab Shared Bridge Ch2", False),
    ]

# Membership per room (admin creates and never needs inviting).
BRIDGED_LOCALPARTS = ["relay-a", "relay-b", "relay-c", "untrusted", "alice", "bob"]
E2EE_LOCALPARTS = ["relay-a", "relay-b", "untrusted"]
OTHER_LOCALPARTS = ["relay-a", "relay-b", "relay-c", "untrusted", "alice", "bob"]
# Bridge rooms (normal profile): every bridge + the humans, NOT the parked
# relay-c. The medre bot joins the bridge rooms only here — under the stress
# `medre` profile it sits in the four per-meshnet rooms instead.
BRIDGE_LOCALPARTS = ["relay-a", "relay-b", "untrusted", "alice", "bob"]
if MEDRE_PROFILE and NORMAL_PROFILE:
    BRIDGE_LOCALPARTS = BRIDGE_LOCALPARTS + ["medre"]
if MEDRE_PROFILE and not NORMAL_PROFILE:
    # The medre bot joins the four bridged rooms (auto-join on startup via
    # its route-derived auto_join_rooms); it has no reason to sit in the
    # negative-control (#lab-other) or E2EE stretch rooms.
    BRIDGED_LOCALPARTS = BRIDGED_LOCALPARTS + ["medre"]
ROOM_MEMBERS: dict[str, list[str]] = {
    "lab-alpha-ch0": BRIDGED_LOCALPARTS,
    "lab-alpha-ch2": BRIDGED_LOCALPARTS,
    "lab-beta-ch0": BRIDGED_LOCALPARTS,
    "lab-beta-ch2": BRIDGED_LOCALPARTS,
    "lab-bridge": BRIDGE_LOCALPARTS,
    "lab-bridge-ch2": BRIDGE_LOCALPARTS,
    "lab-bridge-e2ee": E2EE_LOCALPARTS,
    "lab-other": OTHER_LOCALPARTS,
}


def shared_secret_mac(nonce: str, localpart: str, password: str, admin: bool) -> str:
    """Synapse shared-secret registration MAC (see register_new_matrix_user)."""
    mac = hmac.new(key=SHARED_SECRET.encode("utf-8"), digestmod=hashlib.sha1)
    mac.update(nonce.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(localpart.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(password.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(b"admin" if admin else b"notadmin")
    return mac.hexdigest()


def shared_secret_register(
    localpart: str, password: str, admin: bool
) -> dict[str, str] | None:
    """Register via the Synapse shared-secret admin API; None if user exists."""
    nonce_resp = requests.get(f"{SYNAPSE_URL}/_synapse/admin/v1/register", timeout=20)
    if nonce_resp.status_code >= 400:
        raise RuntimeError(
            f"nonce fetch failed: {nonce_resp.status_code} {nonce_resp.text}"
        )
    nonce = nonce_resp.json()["nonce"]
    response = requests.post(
        f"{SYNAPSE_URL}/_synapse/admin/v1/register",
        json={
            "nonce": nonce,
            "username": localpart,
            "password": password,
            "admin": admin,
            "mac": shared_secret_mac(nonce, localpart, password, admin),
        },
        timeout=20,
    )
    if response.status_code == 400 and "M_USER_IN_USE" in response.text:
        return None
    if response.status_code >= 400:
        raise RuntimeError(
            f"register({localpart}) failed: {response.status_code} {response.text}"
        )
    body = response.json()
    return {
        "access_token": body["access_token"],
        "user_id": body["user_id"],
        "device_id": body["device_id"],
    }


def ensure_account(localpart: str, password: str, admin: bool) -> dict[str, str]:
    registered = shared_secret_register(localpart, password, admin)
    if registered is not None:
        print(f"  registered {localpart} (admin={admin})")
        return registered
    print(f"  {localpart} exists, logging in")
    return login(localpart, password)


def create_room(
    admin_token: str, alias_localpart: str, name: str, encrypted: bool
) -> str:
    alias = f"#{alias_localpart}:{SERVER_NAME}"
    payload: dict[str, Any] = {
        "preset": "private_chat",
        "name": name,
        "topic": f"Interop lab room {alias_localpart}",
        "room_alias_name": alias_localpart,
        "visibility": "private",
    }
    if encrypted:
        payload["initial_state"] = [
            {
                "type": "m.room.encryption",
                "state_key": "",
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            }
        ]
    response = matrix_request(
        "POST", "/_matrix/client/v3/createRoom", admin_token, payload
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"createRoom({alias}) failed: {response.status_code} {response.text}"
        )
    room_id = response.json()["room_id"]
    print(f"  created {alias} -> {room_id}")
    return room_id


def request_with_429_retry(
    method: str,
    path: str,
    token: str | None,
    payload: dict[str, Any],
    attempts: int = 6,
) -> requests.Response:
    """Send a Matrix request, honoring M_LIMIT_EXCEEDED retry_after_ms.

    Synapse keeps counting rejected requests against the limiter, so retries
    honor retry_after_ms (capped) to let the penalty actually decay.
    """
    response = None
    for attempt in range(attempts):
        response = matrix_request(method, path, token, payload)
        if response.status_code == 429:
            retry_after = response.json().get("retry_after_ms", 2000)
            time.sleep(min(retry_after, 60_000) / 1000.0)
            continue
        return response
    return response


def ensure_membership(admin_token: str, room_id: str, account: dict[str, str]) -> None:
    quoted = room_id.replace("!", "%21").replace(":", "%3A")
    invite = request_with_429_retry(
        "POST",
        f"/_matrix/client/v3/rooms/{quoted}/invite",
        admin_token,
        {"user_id": account["user_id"]},
    )
    if invite.status_code >= 400 and "already" not in invite.text:
        raise RuntimeError(
            f"invite({account['user_id']} -> {room_id}) failed: "
            f"{invite.status_code} {invite.text}"
        )
    join = request_with_429_retry(
        "POST", f"/_matrix/client/v3/rooms/{quoted}/join", account["access_token"], {}
    )
    if join.status_code >= 400 and "already" not in join.text.lower():
        raise RuntimeError(
            f"join({account['user_id']} -> {room_id}) failed: {join.status_code} {join.text}"
        )


def main() -> int:
    print(f"Provisioning Synapse at {SYNAPSE_URL} (server_name {SERVER_NAME})")

    accounts: dict[str, dict[str, str]] = {}
    for localpart, password, admin in USERS:
        accounts[localpart] = ensure_account(localpart, password, admin)

    admin_token = accounts["admin"]["access_token"]
    rooms: dict[str, dict[str, str]] = {}
    for alias_localpart, name, encrypted in ROOMS:
        alias = f"#{alias_localpart}:{SERVER_NAME}"
        room_id = resolve_alias(alias, admin_token)
        if room_id is None:
            room_id = create_room(admin_token, alias_localpart, name, encrypted)
        else:
            print(f"  reused {alias} -> {room_id}")
        for localpart in ROOM_MEMBERS[alias_localpart]:
            ensure_membership(admin_token, room_id, accounts[localpart])
            time.sleep(0.3)
        rooms[alias_localpart] = {"alias": alias, "room_id": room_id}

    runtime = {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "homeserver_url": SYNAPSE_URL,
        "server_name": SERVER_NAME,
        "users": {
            localpart: {
                "user_id": accounts[localpart]["user_id"],
                "access_token": accounts[localpart]["access_token"],
                "device_id": accounts[localpart]["device_id"],
                "password": password,
                "admin": admin,
            }
            for localpart, password, admin in USERS
        },
        "rooms": rooms,
    }
    with open(runtime_path(), "w", encoding="utf-8") as handle:
        json.dump(runtime, handle, indent=2)
        handle.write("\n")
    print(f"runtime written to {runtime_path()}")
    print("PROVISION OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
