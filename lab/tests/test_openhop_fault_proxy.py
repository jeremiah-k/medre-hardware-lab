"""Offline tests for the fault splice's request-opened reply stall.

The splice is protocol-blind: its request trigger is the EXISTENCE of
client->server traffic, never its meaning.  These tests pin the latch
rules the killed-with-one-in-flight backlog case depends on:

* the first client->server chunk observed while the flag is armed opens
  the stall;
* requests keep passing (observing a request never withholds anything by
  itself);
* replies are withheld only while the flag is still armed and the latch
  is open;
* clearing the flag ends the stall and resets the latch, so disarm and
  re-arm starts a new stall.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "openhop-radio" / "openhop_fault_proxy.py"
)
spec = importlib.util.spec_from_file_location("openhop_fault_proxy", MODULE_PATH)
assert spec and spec.loader
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


@pytest.fixture(autouse=True)
def reset_splice_stats():
    """STATS is module-level state; each test starts from zero."""
    for key in (
        "request_stall_opens",
        "request_stall_chunks_withheld",
        "message_frames_dropped",
        "frame_resyncs",
    ):
        proxy.STATS[key] = 0
    yield


def write_fault(path: Path, fault: dict) -> None:
    path.write_text(json.dumps(fault) + "\n", encoding="utf-8")


def make_stall(tmp_path: Path) -> tuple[proxy.RequestStall, Path]:
    fault_file = tmp_path / "fault.json"
    write_fault(fault_file, {})
    return proxy.RequestStall(str(fault_file)), fault_file


def test_disarmed_stall_ignores_requests_and_forwards_replies(tmp_path):
    stall, fault_file = make_stall(tmp_path)
    stall.observe_request(connection=1)
    assert stall.withholds_replies() is False
    assert proxy.STATS["request_stall_opens"] == 0


def test_armed_stall_opens_on_the_first_request(tmp_path):
    stall, fault_file = make_stall(tmp_path)
    write_fault(fault_file, {"stall_server_to_client_on_request": True})
    stall.observe_request(connection=1)
    assert proxy.STATS["request_stall_opens"] == 1
    assert stall.withholds_replies() is True


def test_second_request_and_repeated_polls_do_not_reopen(tmp_path):
    stall, fault_file = make_stall(tmp_path)
    write_fault(fault_file, {"stall_server_to_client_on_request": True})
    stall.observe_request(connection=1)
    stall.observe_request(connection=1)
    assert proxy.STATS["request_stall_opens"] == 1
    assert stall.withholds_replies() is True
    assert stall.withholds_replies() is True


def test_requests_pass_and_only_replies_are_withheld(tmp_path):
    """Observing a request never withholds anything by itself."""
    stall, fault_file = make_stall(tmp_path)
    write_fault(fault_file, {"stall_server_to_client_on_request": True})
    assert stall.withholds_replies() is False
    stall.observe_request(connection=1)
    assert stall.withholds_replies() is True


def test_clearing_the_flag_ends_the_stall_and_resets_the_latch(tmp_path):
    stall, fault_file = make_stall(tmp_path)
    write_fault(fault_file, {"stall_server_to_client_on_request": True})
    stall.observe_request(connection=1)
    assert stall.withholds_replies() is True
    write_fault(fault_file, {})
    assert stall.withholds_replies() is False
    write_fault(fault_file, {"stall_server_to_client_on_request": True})
    assert stall.withholds_replies() is False
    stall.observe_request(connection=2)
    assert stall.withholds_replies() is True
    assert proxy.STATS["request_stall_opens"] == 2


def test_unreadable_fault_file_never_opens_the_stall(tmp_path):
    stall, fault_file = make_stall(tmp_path)
    fault_file.unlink()
    stall.observe_request(connection=1)
    assert stall.withholds_replies() is False


def test_new_request_stall_generation_resets_latch_without_intermediate_poll(tmp_path):
    stall, fault_file = make_stall(tmp_path)
    write_fault(
        fault_file,
        {"stall_server_to_client_on_request": True, "request_stall_generation": "one"},
    )
    stall.observe_request(connection=1)
    assert stall.withholds_replies()

    write_fault(fault_file, {})
    write_fault(
        fault_file,
        {"stall_server_to_client_on_request": True, "request_stall_generation": "two"},
    )
    assert not stall.withholds_replies()
    stall.observe_request(connection=2)
    assert stall.withholds_replies()


def test_blackhole_discards_chunks_until_cleared(tmp_path):
    path = tmp_path / "fault.json"
    fault = proxy.Blackhole(str(path))
    flag = "stall_client_to_server"
    counter = "stalled_client_to_server"
    before = proxy.STATS[counter]
    write_fault(path, {flag: True})
    assert fault.drops(flag, 1, counter)
    assert proxy.STATS[counter] == before + 1
    write_fault(path, {})
    assert not fault.drops(flag, 1, counter)
    assert proxy.STATS[counter] == before + 1


# ---------------------------------------------------------------------------
# The content-triggered message-frame drop (drop_message_frames).
#
# Frame format (server -> client): ">" (0x3E) + 2-byte LE length + data,
# data[0] = packet type (the pinned PacketType enum).  The message frames
# are the replies to the client's 0x0a sync command - CONTACT_MSG_RECV
# 0x07, CHANNEL_MSG_RECV 0x08, their V3 forms 0x10/0x11, and the empty
# drain's NO_MORE_MSGS 0x0a; the handshake reply is SELF_INFO 0x05, the
# idle-link probe reply is DEVICE_INFO 0x0d, and MESSAGES_WAITING is the
# 0x83 push.
# ---------------------------------------------------------------------------


def frame(data: bytes) -> bytes:
    """Build one companion frame as the splice's server side emits it."""
    return b">" + len(data).to_bytes(2, "little") + data


HANDSHAKE_REPLY = frame(b"\x05" + b"\x00" * 8)
PROBE_REPLY = frame(b"\x0d" + b"\x00" * 8)
MESSAGE_FRAMES = (
    frame(b"\x07" + b"\x00" * 8)
    + frame(b"\x08" + b"\x00" * 8)
    + frame(b"\x10" + b"\x00" * 8)
    + frame(b"\x11" + b"\x00" * 8)
)
NO_MORE_MSGS_REPLY = frame(b"\x0a" + b"")
MESSAGES_WAITING_PUSH = frame(b"\x83" + b"\x00" * 4)


def make_dropper(tmp_path: Path, fault: dict | None = None):
    path = tmp_path / "fault.json"
    write_fault(
        path,
        fault if fault is not None else {proxy.DROP_MESSAGE_FRAMES_KEY: True},
    )
    return proxy.MessageFrameDropper(str(path)), path


def test_dropper_is_inactive_without_the_fault_key(tmp_path):
    dropper, path = make_dropper(tmp_path, {})
    assert dropper.active() is False
    write_fault(path, {proxy.DROP_MESSAGE_FRAMES_KEY: True})
    assert dropper.active() is True


def test_dropper_forwards_probes_and_handshake_drops_only_message_frames(
    tmp_path, capsys
):
    dropper, _ = make_dropper(tmp_path)
    out = dropper.feed(
        HANDSHAKE_REPLY
        + MESSAGE_FRAMES
        + NO_MORE_MSGS_REPLY
        + PROBE_REPLY
        + MESSAGES_WAITING_PUSH,
        connection=1,
    )
    assert HANDSHAKE_REPLY in out
    assert PROBE_REPLY in out
    assert MESSAGES_WAITING_PUSH not in out
    for message_frame in (
        frame(b"\x07" + b"\x00" * 8),
        frame(b"\x08" + b"\x00" * 8),
        frame(b"\x10" + b"\x00" * 8),
        frame(b"\x11" + b"\x00" * 8),
        NO_MORE_MSGS_REPLY,
    ):
        assert message_frame not in out
    assert proxy.STATS["message_frames_dropped"] == 6
    assert proxy.STATS["frame_resyncs"] == 0
    # Every dropped frame logs its connection identity for the verdict's
    # per-connection attribution.
    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    assert [e["event"] for e in events].count("message_frame_dropped") == 6
    assert {e["connection"] for e in events} == {1}


def test_dropper_buffers_partial_frames_until_complete(tmp_path):
    dropper, _ = make_dropper(tmp_path)
    whole = MESSAGE_FRAMES[: 3 + 9] + PROBE_REPLY  # one V1 message frame + probe
    first = dropper.feed(whole[:5], connection=1)
    assert first == b""
    second = dropper.feed(whole[5:], connection=1)
    assert second == PROBE_REPLY
    assert proxy.STATS["message_frames_dropped"] == 1


def test_dropper_resyncs_past_bytes_that_cannot_start_a_frame(tmp_path):
    dropper, _ = make_dropper(tmp_path)
    before = proxy.STATS["frame_resyncs"]
    out = dropper.feed(b"\x00\xff" + PROBE_REPLY, connection=1)
    assert out == PROBE_REPLY
    assert proxy.STATS["frame_resyncs"] == before + 2


def test_dropper_logs_forwarded_probe_replies_per_connection(tmp_path, capsys):
    """The link's proof evidence: forwarded probe replies are recorded."""
    dropper, _ = make_dropper(tmp_path)
    before = proxy.STATS["probe_replies_forwarded"]
    out = dropper.feed(PROBE_REPLY + MESSAGE_FRAMES[:12], connection=3)
    assert out == PROBE_REPLY  # the probe reply is forwarded, the frame dropped
    assert proxy.STATS["probe_replies_forwarded"] == before + 1
    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    forwarded = [e for e in events if e["event"] == "probe_reply_forwarded"]
    assert forwarded and forwarded[0]["connection"] == 3
    assert forwarded[0]["packet_type"] == proxy.PROBE_REPLY_TYPE


def test_dropper_keeps_dropping_across_chunks(tmp_path):
    dropper, _ = make_dropper(tmp_path)
    assert dropper.feed(MESSAGE_FRAMES[:12] + PROBE_REPLY, connection=1) == (
        PROBE_REPLY
    )
    assert dropper.feed(MESSAGE_FRAMES[:12] + HANDSHAKE_REPLY, connection=1) == (
        HANDSHAKE_REPLY
    )
    assert proxy.STATS["message_frames_dropped"] == 2


def test_dropper_holds_a_tail_that_could_still_grow(tmp_path):
    """A trailing 0x3E with no length yet must not be forwarded early."""
    dropper, _ = make_dropper(tmp_path)
    assert dropper.feed(PROBE_REPLY + b">", connection=1) == PROBE_REPLY
    out = dropper.feed((3).to_bytes(2, "little") + b"\x07abc", connection=1)
    assert out == b""
    assert proxy.STATS["message_frames_dropped"] == 1


# ---------------------------------------------------------------------------
# The OPENHOP_FAULT_PROXY_TRACE forwarded-frame trace.  Both lab drivers
# start the splice with the variable set, so the trace is always on in
# rig runs; these tests pin the mechanism and the record shapes.
# ---------------------------------------------------------------------------


def test_trace_disabled_by_default():
    import os

    saved = os.environ.get(proxy.TRACE_ENV)
    try:
        os.environ.pop(proxy.TRACE_ENV, None)
        assert proxy.trace_enabled() is False
    finally:
        if saved is not None:
            os.environ[proxy.TRACE_ENV] = saved


def test_trace_enabled_when_env_is_1():
    import os

    saved = os.environ.get(proxy.TRACE_ENV)
    try:
        os.environ[proxy.TRACE_ENV] = "1"
        assert proxy.trace_enabled() is True
    finally:
        if saved is not None:
            os.environ[proxy.TRACE_ENV] = saved
        else:
            os.environ.pop(proxy.TRACE_ENV, None)


def test_trace_forward_records_describe_each_frame():
    record_a = frame(b"\x05" + b"\x00" * 8)  # SELF_INFO reply
    message_frame = frame(b"\x07" + b"p4qd-0-deadbeef01")
    records = proxy.trace_forward_records(
        2, record_a + message_frame + b"\x3e"  # trailing partial ignored
    )
    assert len(records) == 2
    assert records[0]["event"] == "frame_forwarded"
    assert records[0]["connection"] == 2
    assert records[0]["packet_type"] == "0x05"
    assert records[0]["marker"] is None
    assert records[1]["packet_type"] == "0x07"
    assert records[1]["marker"] == "p4qd-0-deadbeef01"


def test_trace_forward_records_on_empty_and_garbage():
    assert proxy.trace_forward_records(1, b"") == []
    records = proxy.trace_forward_records(1, b"\x00\x01\x02")
    assert records == []


def test_trace_c2s_records_describe_command_frames():
    app_start = b"<" + (8).to_bytes(2, "little") + b"\x01" + b"\x00" * 7
    sync = b"<" + (1).to_bytes(2, "little") + b"\x0a"
    records = proxy.trace_c2s_records(4, app_start + sync)
    assert [r["command"] for r in records] == ["0x01", "0x0a"]
    assert records[1]["marker"] is None


def test_trace_c2s_records_ignores_foreign_starts_and_partials():
    assert proxy.trace_c2s_records(1, b"\x3e\x01\x00") == []
    assert proxy.trace_c2s_records(1, b"<\x05\x00") == []  # length 5, only 2 bytes


# ---------------------------------------------------------------------------
# The drop event's marker: a swallowed message frame is a consumed
# message, so its event names the lab marker the frame carried (null
# when it carries none) with the same extraction the forwarded-frame
# trace uses.
# ---------------------------------------------------------------------------


def dropper_events(capsys) -> list[dict]:
    return [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]


def test_a_dropped_message_frame_event_carries_its_marker(tmp_path, capsys):
    dropper, _ = make_dropper(tmp_path)
    dropped = dropper.feed(
        frame(b"\x07" + b"p4qd-3-c0ffee12 and more text"), connection=2
    )
    assert dropped == b""
    events = dropper_events(capsys)
    dropped_events = [e for e in events if e["event"] == "message_frame_dropped"]
    assert len(dropped_events) == 1
    assert dropped_events[0]["marker"] == "p4qd-3-c0ffee12"
    assert dropped_events[0]["connection"] == 2


def test_a_dropped_frame_without_a_marker_logs_marker_null(tmp_path, capsys):
    dropper, _ = make_dropper(tmp_path)
    dropper.feed(frame(b"\x07" + b"\x00" * 8), connection=1)
    dropper.feed(NO_MORE_MSGS_REPLY, connection=1)
    dropper.feed(MESSAGES_WAITING_PUSH, connection=1)
    dropped_events = [
        e for e in dropper_events(capsys) if e["event"] == "message_frame_dropped"
    ]
    assert len(dropped_events) == 3
    assert all(event["marker"] is None for event in dropped_events)


def test_the_drop_event_marker_uses_the_same_extraction_as_the_trace():
    """One extraction rule: the trace record and the drop event of the
    same frame must name the same marker."""
    payload = b"\x08" + b"tail p4qs-11-abc123def "
    assert proxy._marker_in(payload) == "p4qs-11-abc123def"
    records = proxy.trace_forward_records(1, frame(payload))
    assert records[0]["marker"] == "p4qs-11-abc123def"
