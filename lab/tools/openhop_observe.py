"""Read completion evidence already emitted by the lab fault splice."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime


def log_time(line: str) -> float | None:
    stamp, separator, _ = line.partition(" ")
    if not separator or "T" not in stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def empty_reply_after(log: str, since: float) -> bool:
    """Observe NO_MORE_MSGS on the latest connection with no newer fetch."""
    connection = 0
    empty = False
    for line in log.splitlines():
        _, separator, message = line.partition(" ")
        if not separator or not message.startswith("{"):
            continue
        at = log_time(line)
        if at is None:
            continue
        try:
            frame = json.loads(message)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue
        number = frame.get("connection")
        if type(number) is not int or number < connection:
            continue
        if number > connection:
            connection = number
            empty = False
        if frame.get("event") == "c2s_frame" and frame.get("command") == "0x0a":
            empty = False
        elif (
            frame.get("event") == "frame_forwarded"
            and frame.get("packet_type") == "0x0a"
        ):
            empty = at >= since
        elif frame.get("event") in {
            "closed_by_fault",
            "closed_by_fault_window",
            "connection_close",
        }:
            empty = False
    return empty


def reconnect_after(log: str, since: float) -> bool:
    """Recognize successful recovery after the scoped fault ends."""
    return scoped_log_line(log, "reconnected successfully", since)


def scoped_log_line(log: str, text: str, since: float) -> bool:
    """Match protocol evidence emitted at or after an arm time."""
    return any(
        text in line and (at := log_time(line)) is not None and at >= since
        for line in log.splitlines()
    )


_deadline: ContextVar[float | None] = ContextVar("observation_deadline", default=None)


def command_budget(limit: float) -> float:
    """Keep nested Docker reads inside the active observation window."""
    deadline = _deadline.get()
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("observation deadline", limit)
    return min(limit, remaining)


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float) -> bool:
    previous = _deadline.get()
    deadline = time.monotonic() + timeout
    if previous is not None:
        deadline = min(previous, deadline)
    token = _deadline.set(deadline)
    try:
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return time.monotonic() <= deadline
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    return False
                raise
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        return False
    finally:
        _deadline.reset(token)


def bridge_adapters_ready(log: str) -> bool:
    """Read each adapter's latest connection transition in process log order."""
    meshcore = meshtastic = False
    for line in log.splitlines():
        if "MeshCoreSession openhop-a" in line:
            if any(
                word in line
                for word in (
                    "SDK disconnected",
                    "retiring connection",
                    "reconnect attempt",
                    "connected=False",
                )
            ):
                meshcore = False
            elif "connected=True)" in line or "reconnected successfully" in line:
                meshcore = True
        if "MeshtasticSession radio-c " in line:
            if "connection lost" in line or "reconnect attempt" in line:
                meshtastic = False
            elif any(
                word in line
                for word in (
                    "started (mode=tcp)",
                    "reconnected after",
                    "initial connection became ready",
                )
            ):
                meshtastic = True
    return meshcore and meshtastic


def withheld_markers(log: str, *, armed_at: float, killed_at: float) -> list[str]:
    """Return markers in fully withheld replies on a request-stalled connection."""
    opened: set[int] = set()
    markers: list[str] = []
    for line in log.splitlines():
        at = log_time(line)
        if at is None or not armed_at <= at <= killed_at:
            continue
        try:
            frame = json.loads(line.partition(" ")[2])
        except ValueError:
            continue
        if not isinstance(frame, dict) or type(frame.get("connection")) is not int:
            continue
        connection = frame["connection"]
        kind = frame.get("event")
        if kind == "request_stall_opened":
            opened.add(connection)
        elif kind == "connection_close":
            opened.discard(connection)
        elif (
            kind == "request_stall_frame_withheld"
            and connection in opened
            and frame.get("packet_type") in {"0x07", "0x08", "0x10", "0x11"}
            and isinstance(frame.get("marker"), str)
        ):
            markers.append(frame["marker"])
    return markers
