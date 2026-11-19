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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlsplit
import uuid

__all__ = ["MatrixPeerListener", "run_matrix_send", "matrix_request"]

_SYNC_SLICE_MS = 1000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A campaign token never follows redirects, even on the same origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_once(
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
    origin = urlsplit(homeserver)
    if (
        origin.scheme not in ("http", "https")
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin.query
        or origin.fragment
        or origin.path not in ("", "/")
        or not path.startswith("/_matrix/")
    ):
        raise ValueError("matrix request must use HTTP(S) and a Matrix client path")
    request = urllib.request.Request(
        homeserver.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    # Scheme guarded above to http(s) only; the campaign homeserver is the
    # local lab synapse from the private config.
    with urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect()
    ).open(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def matrix_request(
    homeserver: str,
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """Enforce a total request deadline, including DNS, headers and body reads.

    urllib's socket timeout is not a whole-request deadline. An owned one-shot
    child is reaped on timeout; the token is passed only through stdin, never
    command arguments or diagnostics. The lab does not depend on another HTTP SDK.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "medre_lab.matrix_live_peer", "--request"],
            input=json.dumps(
                {
                    "homeserver": homeserver,
                    "method": method,
                    "path": path,
                    "body": body,
                    "token": token,
                    "timeout": timeout,
                }
            ),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("Matrix request deadline elapsed") from None
    if proc.returncode == 124:
        raise TimeoutError("Matrix request deadline elapsed")
    if proc.returncode:
        raise RuntimeError("Matrix request failed; no observation credited")
    result = json.loads(proc.stdout)
    if not isinstance(result, dict):
        raise ValueError("Matrix response must be a JSON object")
    return result


def _request_main() -> None:
    try:
        print(json.dumps(_request_once(**json.load(sys.stdin))))
    except TimeoutError:
        raise SystemExit(124) from None
    except urllib.error.URLError as exc:
        raise SystemExit(124 if isinstance(exc.reason, TimeoutError) else 1) from None
    except Exception:
        # Never print credentials, URL-bearing exceptions or response bodies.
        raise SystemExit(1) from None


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
        self.ready_monotonic: float | None = None
        self.last_sync_monotonic: float | None = None
        # Observability parity with the native peers: no owned child process.
        self._owner = None

    def __enter__(self) -> "MatrixPeerListener":
        self._arm_monotonic = time.monotonic()
        who = matrix_request(
            self._homeserver,
            "GET",
            "/_matrix/client/v3/account/whoami",
            token=self._token,
        )
        if who.get("user_id") != self._user_id:
            raise ValueError(
                "Matrix witness token does not belong to configured account"
            )
        body = matrix_request(
            self._homeserver,
            "GET",
            "/_matrix/client/v3/sync?timeout=0&filter=%7B%22room%22%3A%7B%22timeline%22%3A%7B%22limit%22%3A1%7D%7D%7D",
            token=self._token,
        )
        self._since = body.get("next_batch")
        if not isinstance(self._since, str) or not self._since:
            raise ValueError("Matrix initial sync lacks a cursor")
        self.ready_monotonic = self.last_sync_monotonic = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._closed = True
        return False

    def _collect(self, body: dict) -> None:
        observed = time.monotonic()
        join = (body.get("rooms") or {}).get("join") or {}
        for room_id, room in join.items():
            if room_id not in self._room_ids:
                continue
            timeline = room.get("timeline") or {}
            if timeline.get("limited") is True:
                raise AssertionError("Matrix witness timeline gap in campaign room")
            for event in timeline.get("events") or []:
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
                        "observed_monotonic": observed,
                        "observed_offset": observed - self._arm_monotonic,
                    }
                )

    def packets_until(self, predicate, timeout: float) -> list[dict]:
        if self._closed:
            raise AssertionError("matrix peer listener observed after teardown")
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            # Reserve time for response parsing rather than giving each
            # long poll a fresh timeout past the observation deadline.
            slice_ms = max(0, min(_SYNC_SLICE_MS, int(remaining * 500)))
            try:
                body = matrix_request(
                    self._homeserver,
                    "GET",
                    "/_matrix/client/v3/sync?"
                    + urlencode({"since": self._since, "timeout": slice_ms}),
                    token=self._token,
                    timeout=remaining,
                )
            except TimeoutError:
                break
            if time.monotonic() > deadline:
                break  # do not advance a cursor beyond the observation budget
            cursor = body.get("next_batch")
            if not isinstance(cursor, str) or not cursor:
                raise ValueError("Matrix incremental sync lacks a cursor")
            self._collect(body)
            self._since = cursor
            self.last_sync_monotonic = time.monotonic()
            if predicate(self._packets):
                break
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return list(self._packets)


def run_matrix_send(
    homeserver: str, token: str, room_id: str, nonce: str, timeout: float = 30.0
) -> dict:
    """Post one nonce text into the campaign room as the independent sender."""
    started = time.monotonic()
    txn = uuid.uuid4().hex
    body = matrix_request(
        homeserver,
        "PUT",
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/m.room.message/{txn}",
        {"msgtype": "m.text", "body": nonce},
        token=token,
        timeout=timeout,
    )
    if not isinstance(body.get("event_id"), str) or not body["event_id"]:
        raise ValueError("Matrix send lacks an event ID")
    return {
        "nonce": nonce,
        "event_id": body.get("event_id"),
        "room_id": room_id,
        "send_seconds": round(time.monotonic() - started, 3),
    }


if __name__ == "__main__":
    if sys.argv[1:] != ["--request"]:
        raise SystemExit("expected --request")
    _request_main()
