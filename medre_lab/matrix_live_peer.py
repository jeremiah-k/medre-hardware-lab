"""Independent Matrix peers for the mixed-transport continuity campaign.

These peers are the campaign's independent observation surface: they hold
their own Matrix sessions (campaign-local accounts on the lab synapse) and
never share endpoints with the MEDRE owner process. Unlike the native radio
peers there is no exclusive local endpoint to own, so the listener runs
in-process and exposes the same context-manager plus ``packets_until``
contract the continuity machinery already consumes.

Time discipline: observations record monotonic offsets relative to the
listener's arm time, never time-of-day values.
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid

from .native.live_peer_common import poll_packets_until

__all__ = ["MatrixPeerListener", "run_matrix_send", "matrix_request"]

_SYNC_SLICE_MS = 15000


def matrix_request(
    homeserver: str,
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """One bounded Matrix client-server API call returning parsed JSON.

    Only http(s) homeservers are permitted: the campaign talks to the local
    lab synapse, and a non-http scheme in the private config is a hard error
    rather than an unexpected urllib capability.
    """
    if not homeserver.startswith(("http://", "https://")):
        raise ValueError(
            f"matrix homeserver must be an http(s) URL, got {homeserver!r}"
        )
    request = urllib.request.Request(
        homeserver + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    # Scheme guarded above to http(s) only; the campaign homeserver is the
    # local lab synapse from the private config.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        return json.loads(response.read() or b"{}")


class MatrixPeerListener:
    """Independent recipient: collects room messages across long-poll syncs.

    ``packets_until`` matches the native peers' contract: bounded by the
    remaining deadline, returns the complete collected list either way so
    absence evidence keeps the same shape as positive evidence. The initial
    sync at arm time pins the stream cursor BEFORE any declared window, so
    pre-window traffic can never satisfy a later observation.
    """

    def __init__(
        self,
        homeserver: str,
        token: str,
        room_ids: tuple[str, ...],
        user_id: str,
        lifetime: float = 600.0,
    ):
        self._homeserver = homeserver.rstrip("/")
        self._token = token
        self._room_ids = frozenset(room_ids)
        self._user_id = user_id
        self._lifetime = lifetime
        self._since: str | None = None
        self._packets: list[dict] = []
        self._seen_event_ids: set[str] = set()
        self._arm_monotonic: float | None = None
        self._closed = False
        # Observability parity with the native peers: no owned child process.
        self._owner = None

    def __enter__(self) -> "MatrixPeerListener":
        self._arm_monotonic = time.monotonic()
        body = matrix_request(
            self._homeserver,
            "GET",
            "/_matrix/client/v3/sync?timeout=0&filter=%7B%22room%22%3A%7B%22timeline%22%3A%7B%22limit%22%3A1%7D%7D%7D",
            token=self._token,
        )
        self._since = body.get("next_batch")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._closed = True
        return False

    def _collect(self, body: dict) -> None:
        join = (body.get("rooms") or {}).get("join") or {}
        for room_id, room in join.items():
            if room_id not in self._room_ids:
                continue
            for event in (room.get("timeline") or {}).get("events") or []:
                event_id = event.get("event_id")
                if (
                    event.get("type") != "m.room.message"
                    or not event_id
                    or event_id in self._seen_event_ids
                ):
                    continue
                content = event.get("content") or {}
                text = content.get("body")
                if not isinstance(text, str):
                    continue
                self._seen_event_ids.add(event_id)
                self._packets.append(
                    {
                        "text": text,
                        "sender": event.get("sender"),
                        "room_id": room_id,
                        "event_id": event_id,
                        "origin_server_ts": event.get("origin_server_ts"),
                        "observed_offset": round(
                            time.monotonic()
                            - (self._arm_monotonic or time.monotonic()),
                            3,
                        ),
                    }
                )

    def packets_until(self, predicate, timeout: float) -> list[dict]:
        if self._closed:
            raise AssertionError("matrix peer listener observed after teardown")
        deadline = time.monotonic() + timeout

        def read_packets() -> list[dict]:
            remaining = deadline - time.monotonic()
            slice_ms = max(0, min(_SYNC_SLICE_MS, int(remaining * 1000)))
            body = matrix_request(
                self._homeserver,
                "GET",
                f"/_matrix/client/v3/sync?since={self._since}&timeout={slice_ms}",
                token=self._token,
                timeout=slice_ms / 1000.0 + 15.0,
            )
            self._since = body.get("next_batch", self._since)
            self._collect(body)
            return list(self._packets)

        return poll_packets_until(read_packets, predicate, timeout)


def run_matrix_send(
    homeserver: str, token: str, room_id: str, nonce: str, timeout: float = 30.0
) -> dict:
    """Post one nonce text into the campaign room as the independent sender."""
    started = time.monotonic()
    txn = uuid.uuid4().hex
    body = matrix_request(
        homeserver,
        "PUT",
        f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
        {"msgtype": "m.text", "body": nonce},
        token=token,
        timeout=timeout,
    )
    return {
        "nonce": nonce,
        "event_id": body.get("event_id"),
        "room_id": room_id,
        "send_seconds": round(time.monotonic() - started, 3),
    }
