"""Pin which wire observations authorize an early end to a wait."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from openhop_observe import (  # noqa: E402
    bridge_adapters_ready,
    command_budget,
    empty_reply_after,
    log_time,
    reconnect_after,
    wait_until,
    withheld_markers,
)

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


def test_bridge_disconnect_invalidates_an_earlier_success():
    meshcore = "MeshCoreSession openhop-a started (mode=tcp, connected=True)"
    meshtastic = "MeshtasticSession radio-c started (mode=tcp)"
    both = f"{meshcore}\n{meshtastic}\n"
    lost = both + "MeshCoreSession openhop-a: SDK disconnected\n"
    assert bridge_adapters_ready(both)
    assert not bridge_adapters_ready(lost)
    assert bridge_adapters_ready(
        lost + "MeshCoreSession openhop-a: reconnected successfully"
    )
    assert not bridge_adapters_ready(
        both + "MeshtasticSession radio-c connection lost: EOF"
    )


def test_nested_reads_consume_the_remaining_budget(monkeypatch):
    import subprocess
    import time

    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def predicate():
        now[0] += 4.0
        assert command_budget(120) == 1.0
        now[0] += 1.0
        raise subprocess.TimeoutExpired("docker", 1.0)

    assert not wait_until(predicate, 5.0, 1.0)
    assert command_budget(120) == 120


def test_only_a_complete_withheld_marker_on_the_opened_connection_counts():
    opened = '2026-01-01T00:00:01Z {"event":"request_stall_opened","connection":1}'
    wrong = '2026-01-01T00:00:02Z {"event":"request_stall_frame_withheld","connection":2,"packet_type":"0x08","marker":"p4c5-0-abcdef"}'
    withheld = wrong.replace('"connection":2', '"connection":1')
    assert withheld_markers(wrong, armed_at=0, killed_at=log_time(wrong)) == []
    assert withheld_markers(
        "\n".join((opened, wrong, withheld)),
        armed_at=log_time(opened),
        killed_at=log_time(withheld),
    ) == ["p4c5-0-abcdef"]
