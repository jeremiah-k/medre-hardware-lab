"""Shared helpers for interop-lab tooling (runs inside lab-tools)."""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import Any

import requests

SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "http://synapse:8008")
LAB_STATE_DIR = os.environ.get("LAB_STATE_DIR", "/lab-state")
LAB_LOGS_DIR = os.environ.get("LAB_LOGS_DIR", "/lab-logs")
SERVER_NAME = os.environ.get("LAB_SERVER_NAME", "lab.test")


def runtime_path() -> str:
    return os.path.join(LAB_STATE_DIR, "runtime.json")


def load_runtime() -> dict[str, Any]:
    with open(runtime_path(), encoding="utf-8") as handle:
        return json.load(handle)


def matrix_request(
    method: str,
    path: str,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = 20,
) -> requests.Response:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(
        method,
        f"{SYNAPSE_URL}{path}",
        headers=headers,
        json=payload,
        params=params,
        timeout=timeout,
    )


def login(localpart: str, password: str) -> dict[str, str]:
    response = matrix_request(
        "POST",
        "/_matrix/client/v3/login",
        payload={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": localpart},
            "password": password,
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"login({localpart}) failed: {response.status_code} {response.text}"
        )
    body = response.json()
    return {
        "access_token": body["access_token"],
        "user_id": body["user_id"],
        "device_id": body["device_id"],
    }


def resolve_alias(alias: str, token: str) -> str | None:
    quoted = urllib.parse.quote(alias, safe="")
    response = matrix_request(
        "GET", f"/_matrix/client/v3/directory/room/{quoted}", token=token
    )
    if response.status_code == 200:
        return response.json().get("room_id")
    if response.status_code == 404:
        return None
    raise RuntimeError(
        f"alias resolve failed for {alias}: {response.status_code} {response.text}"
    )


def wait_for_matrix_event(
    token: str,
    room_id: str,
    timeout_seconds: float,
    sender: str | None = None,
    event_type: str | None = None,
    body_contains: str | None = None,
    since: str | None = None,
) -> tuple[dict[str, Any], str] | None:
    """Poll /sync for a matching timeline event.

    Returns (event, next_batch) on match, None on timeout. Lifted from the
    upstream CI matrix_wait_event pattern (requests-based, incremental since).
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        params: dict[str, Any] = {"timeout": 5000}
        if since:
            params["since"] = since
        response = matrix_request(
            "GET", "/_matrix/client/v3/sync", token=token, params=params, timeout=30
        )
        if response.status_code >= 400:
            time.sleep(1)
            continue
        payload = response.json()
        since = payload.get("next_batch", since)
        events = (
            payload.get("rooms", {})
            .get("join", {})
            .get(room_id, {})
            .get("timeline", {})
            .get("events", [])
        )
        for event in events:
            if event_type and event.get("type") != event_type:
                continue
            if sender and event.get("sender") != sender:
                continue
            content = event.get("content", {})
            if not isinstance(content, dict):
                continue
            if body_contains:
                body = content.get("body", "")
                if not isinstance(body, str) or body_contains not in body:
                    continue
            return event, since
    return None


def connect_sqlite_ro(db_path: str):
    """Open a relay SQLite database read-only (WAL-aware)."""
    import sqlite3

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
