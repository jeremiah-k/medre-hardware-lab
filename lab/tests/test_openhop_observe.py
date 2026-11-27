"""Pin which wire observations authorize an early end to a wait."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from openhop_observe import empty_reply_after, log_time, reconnect_after  # noqa: E402

EMPTY = '2026-01-01T00:00:01Z {"event":"frame_forwarded","connection":1,"packet_type":"0x0a"}'
FETCH = '2026-01-01T00:00:02Z {"event":"c2s_frame","connection":1,"command":"0x0a"}'
DISCONNECT = '2026-01-01T00:00:03Z {"event":"closed_by_fault_window","connection":1}'
NEW_LINK = '2026-01-01T00:00:04Z {"event":"c2s_frame","connection":2,"command":"0x01"}'


def test_only_a_current_empty_reply_closes_the_drain():
    since = log_time(EMPTY)
    assert empty_reply_after(EMPTY, since)
    assert not empty_reply_after(EMPTY, since + 1)
    assert not empty_reply_after("\n".join((EMPTY, FETCH)), since)
    assert not empty_reply_after("\n".join((EMPTY, DISCONNECT)), since)
    assert not empty_reply_after("\n".join((EMPTY, NEW_LINK, EMPTY)), since)


def test_close_window_needs_a_scoped_reconnect_without_a_retirement_log():
    log = "\n".join(
        (
            "2026-01-01T00:00:01Z reconnected successfully",
            "2026-01-01T00:00:02Z SDK disconnected",
            "2026-01-01T00:00:03Z reconnected successfully",
        )
    )
    assert reconnect_after(log, log_time(log.splitlines()[1]))
    assert not reconnect_after(log, log_time(log.splitlines()[2]) + 1)
