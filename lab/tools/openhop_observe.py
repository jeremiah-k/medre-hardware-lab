"""Read completion evidence already emitted by the lab fault splice."""

from __future__ import annotations

import json
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
