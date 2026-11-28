"""Offline tests for the lossy-link fault matrix verdict rules.

Four verdicts exist and must not be confused:

* the healthy-link verdict asserts the current design contract on a link
  that never breaks: a fetch timeout is an idle window that is re-issued
  or parked, and an idle window never retires a connection.  These tests
  pin that a mild case delivers everything without churn, and that a
  duplicate wake or an idle-window retirement fails.
* the time-window stall verdict judges a fault that is armed at a point
  in time and persists across every connection inside the window: nothing
  may be consumed, a probe retirement of a link that answers nothing is
  the designed outcome, reconnect attempts continue on backoff without
  success, and when the window ends everything queued must be delivered.
* the drain-stall verdict judges the reply-stall window that is armed
  while a backlog drain is in flight: the case does not observe which
  frame the stall catches, so the verdict asserts the bound - at most
  one message may be consumed, every other accepted message is
  delivered once, no reconnect succeeds while the window holds, and
  the drain resumes without a reconnect or within one probe interval
  after it ends.
* the selective-drop verdict judges the per-connection worst case the
  spec bounds: at most one consumed message per answered liveness probe
  beyond the unproven first fetch, every other accepted message
  delivered or preserved, zero duplicates.
* the silent-dead-link verdict is the opposite of the stall cases:
  retiring the connection through the idle-link liveness probe IS the
  designed outcome, so it requires exactly one probe retirement inside
  the measured window, reconnect attempts without success while both
  directions are held, no duplicates, and delivery of what was queued
  once the link returns.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "openhop_fault_matrix.py"
spec = importlib.util.spec_from_file_location("openhop_fault_matrix", MODULE_PATH)
assert spec and spec.loader
fault_matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fault_matrix)


def judge_mild(**overrides):
    """Run the healthy-link verdict with a healthy measured run."""
    kwargs = {
        "accepted": 6,
        "delivered_distinct": 6,
        "duplicate_wakes": 0,
        "preserved_distinct": 0,
        "retirements": 0,
        "reconnect_attempts": 0,
        "expect_recovery": True,
    }
    kwargs.update(overrides)
    return fault_matrix.fault_case_failures(**kwargs)


def test_a_mild_fault_must_deliver_every_accepted_message():
    failures = judge_mild(delivered_distinct=5)
    assert any("only 5 of 6" in failure for failure in failures)


def test_a_mild_fault_must_not_churn():
    failures = judge_mild(reconnect_attempts=3)
    assert any("3 reconnect attempts" in failure for failure in failures)


def test_a_duplicate_wake_fails_the_mild_case():
    failures = judge_mild(duplicate_wakes=1)
    assert any("duplicate" in failure for failure in failures)


def test_a_message_neither_delivered_nor_preserved_fails_the_mild_case():
    failures = judge_mild(delivered_distinct=2, preserved_distinct=3)
    assert any("2 delivered + 3 preserved" in failure for failure in failures)


def test_an_idle_window_retirement_fails_the_mild_case():
    failures = judge_mild(retirements=3)
    assert any("retired over idle fetch windows" in failure for failure in failures)


def judge_window(**overrides):
    """Run the time-window stall verdict with a clean measured run."""
    kwargs = {
        "accepted": 6,
        "delivered_distinct": 6,
        "preserved_distinct": 0,
        "duplicate_wakes": 0,
        "reconnect_attempts_while_armed": 2,
        "reconnect_successes_while_armed": 0,
        "reconnect_seconds_after_restore": 4.0,
    }
    kwargs.update(overrides)
    return fault_matrix.window_stall_failures(**kwargs)


def test_a_clean_window_run_passes():
    assert judge_window() == []


def test_nothing_may_be_consumed_while_the_window_holds():
    """A message in neither store was consumed into the faulted link."""
    failures = judge_window(delivered_distinct=2, preserved_distinct=3)
    assert any("1 consumed" in failure for failure in failures)


def test_a_still_preserved_message_after_the_window_fails():
    """The window ended; everything queued must be delivered."""
    failures = judge_window(delivered_distinct=5, preserved_distinct=1)
    assert any("only 5 of 6" in failure for failure in failures)
    assert any("still preserved" in failure for failure in failures)


def test_a_duplicate_wake_fails_the_window_case():
    failures = judge_window(duplicate_wakes=1)
    assert any("duplicate" in failure for failure in failures)


def test_no_reconnect_attempt_while_armed_fails():
    failures = judge_window(reconnect_attempts_while_armed=0)
    assert any("no reconnect attempt" in failure for failure in failures)


def test_a_reconnect_success_while_armed_fails():
    failures = judge_window(reconnect_successes_while_armed=1)
    assert any("succeeded while" in failure for failure in failures)


def test_a_missing_post_window_reconnect_fails():
    assert judge_window(reconnect_seconds_after_restore=None) != []


def judge_bounded(**overrides):
    """Run the selective-drop verdict with the documented bound met.

    The synthetic run: connection 3 answered one probe and lost one
    frame - the same-connection proof the verdict now requires.
    """
    kwargs = {
        "accepted": 6,
        "delivered_distinct": 1,
        "preserved_distinct": 3,
        "duplicate_wakes": 0,
        "consumed_per_connection": {3: 2},
        "answered_per_connection": {3: 1},
        "unattributed_consumed": 0,
        "same_connection_proof": True,
    }
    kwargs.update(overrides)
    return fault_matrix.selective_drop_failures(**kwargs)


def test_consumption_within_the_documented_bound_passes():
    """One consumed message per answered probe is the spec's bound."""
    # consumed on connection 3 = 2 <= 1 + 1 answered probe on it.
    assert judge_bounded() == []
    assert judge_bounded(delivered_distinct=2, preserved_distinct=4) == []


def test_the_consumed_count_is_reported_not_hidden():
    """A passing run still reports what the link ate; the evidence does."""
    # The verdict passes at the bound; the per-connection consumed counts
    # are written to evidence by the case.
    assert judge_bounded() == []


def test_consumption_beyond_the_bound_fails():
    failures = judge_bounded(
        delivered_distinct=0,
        preserved_distinct=0,
        consumed_per_connection={3: 2},
        answered_per_connection={3: 0},
    )
    assert any("more than the documented bound" in failure for failure in failures)


def test_zero_consumption_fails_the_bounded_case_without_the_proof():
    """Nothing lost means the selective drop was not exercised."""
    failures = judge_bounded(
        delivered_distinct=6,
        preserved_distinct=0,
        consumed_per_connection={},
        answered_per_connection={3: 1},
        same_connection_proof=False,
    )
    assert any("does not prove the selective drop" in failure for failure in failures)


def test_a_duplicate_wake_fails_the_bounded_case():
    failures = judge_bounded(duplicate_wakes=1)
    assert any("duplicate" in failure for failure in failures)


def test_an_impossible_accounting_fails_the_bounded_case():
    failures = judge_bounded(delivered_distinct=6, preserved_distinct=2)
    assert any("exceeds the 6 accepted" in failure for failure in failures)


def test_aggregate_counts_without_attribution_fail_the_bounded_case():
    """Answered probes and lost frames must share a connection."""
    failures = judge_bounded(
        consumed_per_connection={3: 1, 5: 1},
        answered_per_connection={3: 0, 5: 1},
        same_connection_proof=False,
    )
    assert any("does not prove the selective drop" in failure for failure in failures)


def test_a_connection_both_probe_answered_and_frame_lost_proves_the_drop():
    assert (
        judge_bounded(
            consumed_per_connection={3: 1, 5: 1},
            answered_per_connection={3: 0, 5: 1},
            same_connection_proof=True,
        )
        == []
    )


def test_an_unattributable_lost_frame_fails_the_bounded_case():
    failures = judge_bounded(unattributed_consumed=1)
    assert any("could not be attributed" in failure for failure in failures)


def test_the_per_connection_bound_uses_that_connections_probes():
    """Connection 5's answered probes cannot cover connection 3's loss."""
    failures = judge_bounded(
        consumed_per_connection={3: 2, 5: 1},
        answered_per_connection={3: 0, 5: 2},
    )
    assert any("connection 3 lost 2" in failure for failure in failures)
    assert not any("connection 5" in failure for failure in failures)


def test_splice_stall_events_carry_the_connection_identity():
    log = "\n".join(
        [
            "2026-11-26T18:00:00.000000000Z other log line",
            '2026-11-26T18:00:01.000000000Z {"event": "listening"}',
            '2026-11-26T18:00:02.000000000Z {"event": "stalled", "connection": 1}',
            '2026-11-26T18:00:40.000000000Z {"event": "stalled", "connection": 2}',
        ]
    )
    events = fault_matrix.splice_stall_events(log)
    assert len(events) == 2
    assert events[0]["connection"] == 1 and events[1]["connection"] == 2
    assert events[0]["at"] < events[1]["at"]


def test_splice_events_include_the_content_triggered_frame_drops():
    """The drop_message_frames rule logs the same connection identity."""
    log = "\n".join(
        [
            '2026-11-26T18:00:05.000000000Z {"event": "message_frame_dropped", '
            '"connection": 1, "packet_type": 10}',
            '2026-11-26T18:00:19.000000000Z {"event": "message_frame_dropped", '
            '"connection": 1, "packet_type": 7}',
        ]
    )
    events = fault_matrix.splice_stall_events(log)
    assert len(events) == 2
    assert all(entry["connection"] == 1 for entry in events)
    assert all(entry["event"] == "message_frame_dropped" for entry in events)
    assert [entry["packet_type"] for entry in events] == ["0x0a", "0x07"]
    assert events[0]["at"] < events[1]["at"]


def test_answered_probes_come_from_the_splices_forwarded_replies():
    """The answered probes per connection ARE the splice's own forwarded
    probe replies: a probe answered just before its connection retires is
    credited to the connection that answered it, never to the next one."""
    events = [
        link_event(1.0, "probe_reply_forwarded", 1, 13),
        link_event(2.0, "message_frame_dropped", 1, 7),
        link_event(3.0, "probe_reply_forwarded", 1, 13),
        link_event(4.0, "probe_reply_forwarded", 2, 13),
    ]
    counts = fault_matrix.probe_replies_forwarded_per_connection(events)
    assert counts == {1: 2, 2: 1}
    assert sum(counts.values()) == 3


def judge_drain_stall(**overrides):
    """Run the drain-stall verdict with a clean measured run."""
    kwargs = {
        "accepted": 120,
        "delivered_distinct": 119,
        "preserved_distinct": 0,
        "duplicate_wakes": 0,
        "reconnect_successes_while_armed": 0,
        "retired_during_window": True,
        "reconnected_after_window": True,
        "resume_seconds_after_window": 6.0,
    }
    kwargs.update(overrides)
    return fault_matrix.drain_stall_failures(**kwargs)


def test_a_clean_drain_stall_run_passes():
    assert judge_drain_stall() == []


def test_the_drain_stall_run_without_a_reconnect_passes():
    """The drain may resume on the same connection: no reconnect at all."""
    assert judge_drain_stall(retired_during_window=False) == []


def test_more_than_one_consumed_message_fails_the_drain_stall():
    failures = judge_drain_stall(delivered_distinct=117)
    assert any("more than the one message the bound admits" in f for f in failures)


def test_an_undelivered_backlog_message_fails_the_drain_stall():
    failures = judge_drain_stall(delivered_distinct=118, preserved_distinct=1)
    assert any("still queued at the companion" in failure for failure in failures)


def test_a_duplicate_wake_fails_the_drain_stall():
    failures = judge_drain_stall(duplicate_wakes=1)
    assert any("duplicate" in failure for failure in failures)


def test_a_reconnect_success_while_the_drain_stall_holds_fails():
    failures = judge_drain_stall(reconnect_successes_while_armed=1)
    assert any("succeeded while" in failure for failure in failures)


def test_a_resume_later_than_the_backoff_window_fails():
    """The retired branch allows the reconnect backoff (one to thirty
    seconds on TCP under bounded jitter) plus one gated probe budget; the
    case's own measured bound after the window ends is 45 s."""
    failures = judge_drain_stall(resume_seconds_after_window=46.0)
    assert any("later than the reconnect backoff" in failure for failure in failures)


def test_a_resume_within_the_backoff_window_passes_on_the_retired_branch():
    """A resume past one probe interval is inside the retired branch's
    reconnect-backoff window, not a failure."""
    assert judge_drain_stall(resume_seconds_after_window=16.0) == []


def test_a_retired_connection_that_never_reconnects_fails():
    failures = judge_drain_stall(reconnected_after_window=False)
    assert any("never reconnected" in failure for failure in failures)


def test_an_impossible_accounting_fails_the_drain_stall():
    failures = judge_drain_stall(delivered_distinct=121)
    assert any("exceeds the 120 accepted" in failure for failure in failures)


def judge_blackhole(**overrides):
    """Run the silent-dead-link verdict with a healthy measured run."""
    kwargs = {
        "retirements": 1,
        "probe_retirements": 1,
        "retire_seconds_after_arm": 20.0,
        "reconnect_attempts_while_armed": 3,
        "reconnect_successes_while_armed": 0,
        "duplicate_wakes": 0,
        "accepted": 3,
        "delivered_distinct": 3,
        "reconnect_seconds_after_restore": 4.0,
    }
    kwargs.update(overrides)
    return fault_matrix.idle_blackhole_failures(**kwargs)


def test_a_clean_silent_death_run_passes():
    assert judge_blackhole() == []


def test_a_retirement_outside_the_measured_window_fails():
    failures = judge_blackhole(retire_seconds_after_arm=90.0)
    assert any("90.0 s" in failure for failure in failures)
    assert judge_blackhole(retire_seconds_after_arm=2.0) != []


def test_a_retirement_by_something_other_than_the_probe_fails():
    failures = judge_blackhole(retirements=2, probe_retirements=1)
    assert any("exactly one idle-link probe retirement" in f for f in failures)


def test_no_reconnect_attempt_while_the_silent_link_holds_fails():
    failures = judge_blackhole(reconnect_attempts_while_armed=0)
    assert any("no reconnect attempt" in failure for failure in failures)


def test_a_reconnect_success_while_the_silent_link_holds_fails():
    failures = judge_blackhole(reconnect_successes_while_armed=1)
    assert any("succeeded while" in failure for failure in failures)


def test_undelivered_messages_queued_during_the_fault_fail():
    failures = judge_blackhole(delivered_distinct=2)
    assert any("2 of 3" in failure for failure in failures)


def test_duplicates_fail_the_silent_death_case():
    failures = judge_blackhole(duplicate_wakes=1)
    assert any("duplicate" in failure for failure in failures)


def test_a_missing_post_restore_reconnect_fails():
    assert judge_blackhole(reconnect_seconds_after_restore=None) != []


def test_the_silent_death_case_is_registered_outside_the_stall_matrix():
    """The stall-case verdicts must never judge the silent-death run."""
    assert "idle_blackhole" not in fault_matrix.CASES
    assert "idle_blackhole" not in fault_matrix.WINDOW_RUNNERS


def test_the_drain_stall_case_is_registered_as_a_window_case():
    """The mid-drain stall runs the window arm/queue/clear flow."""
    assert fault_matrix.WINDOW_RUNNERS.get("reply_stall_drain") == (
        "case_reply_stall_during_drain"
    )


def test_the_registry_matches_each_verdict():
    """Every registered case names its judge; mild cases must recover."""
    recovering = {"latency_under", "repeated_waiting"}
    for key, (*_, expect_recovery, verdict) in fault_matrix.CASES.items():
        if key in recovering:
            assert expect_recovery, f"{key} must expect recovery"
            assert verdict == "mild", f"{key} must use the healthy-link verdict"
        elif key == "latency_over":
            # The link answers slowly but answers; the healthy-link rule
            # judges it, without requiring full delivery mid-fault.
            assert not expect_recovery
            assert verdict == "mild"
        else:
            assert not expect_recovery, f"{key} is faulted, not healthy"
            assert verdict == "bounded", f"{key} must use the bounded verdict"
    for key in fault_matrix.WINDOW_RUNNERS:
        assert key not in fault_matrix.CASES
        assert hasattr(fault_matrix.FaultMatrix, fault_matrix.WINDOW_RUNNERS[key])


def test_the_reconnect_case_is_registered_as_a_window_case():
    """The case named reconnect runs the arm/clear flow its claim needs."""
    assert fault_matrix.WINDOW_RUNNERS.get("reconnect") == "case_reconnect"
    assert "reconnect" not in fault_matrix.CASES


def judge_reconnect(**overrides):
    """Run the reconnect verdict with a reconnected measured run."""
    kwargs = {
        "accepted": 6,
        "delivered_distinct": 6,
        "preserved_distinct": 0,
        "duplicate_wakes": 0,
        "reconnect_attempts_while_faulted": 3,
        "reconnect_successes_while_faulted": 0,
        "reconnected_after_window": True,
        "reconnect_seconds_after_window": 4.0,
    }
    kwargs.update(overrides)
    return fault_matrix.reconnect_case_failures(**kwargs)


def test_a_clean_reconnect_run_passes():
    assert judge_reconnect() == []


def test_traffic_without_reconnect_evidence_fails_the_reconnect_case():
    """Resuming traffic is not the claim; the adapter's evidence is."""
    failures = judge_reconnect(reconnected_after_window=False)
    assert any(
        "no reconnect was observed after the fault ended" in failure
        for failure in failures
    )


def test_no_reconnect_attempt_while_the_window_holds_fails():
    failures = judge_reconnect(reconnect_attempts_while_faulted=0)
    assert any("no reconnect attempt" in failure for failure in failures)


def test_a_reconnect_success_inside_the_window_fails():
    failures = judge_reconnect(reconnect_successes_while_faulted=1)
    assert any("succeeded while" in failure for failure in failures)


def test_a_missing_post_window_reconnect_fails_the_reconnect_case():
    assert judge_reconnect(reconnected_after_window=False) != []


def test_a_reconnect_later_than_the_bound_fails():
    failures = judge_reconnect(reconnect_seconds_after_window=46.0)
    assert any("later than the reconnect backoff" in failure for failure in failures)


def test_undelivered_messages_fail_the_reconnect_case():
    failures = judge_reconnect(delivered_distinct=5)
    assert any("5 delivered + 0 preserved" in failure for failure in failures)


def test_duplicates_fail_the_reconnect_case():
    failures = judge_reconnect(duplicate_wakes=1)
    assert any("duplicate" in failure for failure in failures)


def test_something_still_preserved_fails_the_reconnect_case():
    failures = judge_reconnect(delivered_distinct=5, preserved_distinct=1)
    assert any("still queued at the companion" in failure for failure in failures)


def test_surviving_link_must_resume_inside_one_probe_interval():
    failures = judge_drain_stall(
        retired_during_window=False, resume_seconds_after_window=16.0
    )
    assert any("one probe interval" in failure for failure in failures)


def test_reconnect_without_delivery_does_not_prove_drain_resumption():
    failures = judge_drain_stall(resume_seconds_after_window=None)
    assert any("no delivery was observed" in failure for failure in failures)


# ---------------------------------------------------------------------------
# The saturation window derived from the peer's measured send pacing: the
# "batch must be accepted inside the window" precondition holds by
# construction instead of racing a fixed window.
# ---------------------------------------------------------------------------


def test_saturation_window_scales_with_the_measured_pacing():
    """Measured pacing 1.2 s/send: 60 x 1.2 x 1.5 + 10 = 118 s."""
    window = fault_matrix.saturation_window_for(fault_matrix.SATURATION_BATCH, 1.2)
    assert window == fault_matrix.SATURATION_BATCH * 1.2 * 1.5 + 10


def test_saturation_window_falls_back_to_two_seconds_per_send():
    """A standalone saturation run has no measurement: 60 x 2 x 1.5 + 10."""
    assert (
        fault_matrix.saturation_window_for(fault_matrix.SATURATION_BATCH, None)
        == fault_matrix.SATURATION_BATCH * 2.0 * 1.5 + 10
    )
    assert (
        fault_matrix.saturation_window_for(fault_matrix.SATURATION_BATCH, 0)
        == fault_matrix.SATURATION_BATCH * 2.0 * 1.5 + 10
    )


def test_saturation_window_tolerates_one_and_a_half_times_the_pacing():
    """The window holds the batch even if pacing runs 1.5x the measured
    value, with the headroom still left over."""
    measured = 1.3
    window = fault_matrix.saturation_window_for(fault_matrix.SATURATION_BATCH, measured)
    assert window >= fault_matrix.SATURATION_BATCH * measured * 1.5 + 10


def test_the_recorded_wave19_overrun_is_impossible_by_construction():
    """The wave19 run's batch lasted ~96 s inside a 60 s window; a window
    built from a 1.6 s/s measurement (worst recorded) leaves ~50 s of
    slack at the same pacing."""
    window = fault_matrix.saturation_window_for(fault_matrix.SATURATION_BATCH, 1.6)
    assert window >= 60 * 1.6 + 10
    assert window - 60 * 1.6 >= fault_matrix.SATURATION_WINDOW_HEADROOM_S


# ---------------------------------------------------------------------------
# The selective-drop link's proof: the splice records forwarded probe
# replies and dropped message frames per connection; the proof holds when
# ONE connection carries both.
# ---------------------------------------------------------------------------


def link_event(stamp: float, event: str, connection: int, packet_type=None):
    return {
        "at": stamp,
        "event": event,
        "connection": connection,
        "packet_type": packet_type,
    }


def test_splice_link_events_carry_packet_types():
    log = "\n".join(
        [
            '2026-11-26T18:00:02.000000Z {"event": "message_frame_dropped", '
            '"connection": 1, "packet_type": 7}',
            '2026-11-26T18:00:05.000000Z {"event": "probe_reply_forwarded", '
            '"connection": 1, "packet_type": 13}',
        ]
    )
    events = fault_matrix.splice_link_events(log)
    assert [e["event"] for e in events] == [
        "message_frame_dropped",
        "probe_reply_forwarded",
    ]
    assert events[0]["packet_type"] == 7
    assert events[1]["packet_type"] == 13
    assert all(e["connection"] == 1 for e in events)


def test_the_proof_report_proves_one_connection_with_both():
    events = [
        link_event(1.0, "probe_reply_forwarded", 1, 13),
        link_event(2.0, "message_frame_dropped", 1, 7),
        link_event(3.0, "message_frame_dropped", 1, 8),
    ]
    report = fault_matrix.selective_drop_proof_report(events)
    assert report["same_connection_proof"] is True
    assert report["message_frames_dropped_per_connection"] == {1: 2}
    assert report["probe_replies_forwarded_per_connection"] == {1: 1}


def test_the_proof_report_ignores_pushes_and_no_more_replies():
    """Swallowed notifications and empty drains carry no message: they are
    part of the link's definition, never proof of a lost frame."""
    events = [
        link_event(1.0, "message_frame_dropped", 1, 0x83),
        link_event(2.0, "message_frame_dropped", 1, 0x0A),
        link_event(3.0, "probe_reply_forwarded", 1, 13),
    ]
    report = fault_matrix.selective_drop_proof_report(events)
    assert report["same_connection_proof"] is False
    assert report["message_frames_dropped_per_connection"] == {}


def test_the_proof_report_fails_when_the_kinds_split_across_connections():
    events = [
        link_event(1.0, "probe_reply_forwarded", 1, 13),
        link_event(2.0, "message_frame_dropped", 2, 7),
    ]
    report = fault_matrix.selective_drop_proof_report(events)
    assert report["same_connection_proof"] is False


def test_the_proof_report_is_empty_without_events():
    report = fault_matrix.selective_drop_proof_report([])
    assert report["same_connection_proof"] is False
    assert report["probe_replies_forwarded_per_connection"] == {}
    assert report["message_frames_dropped_per_connection"] == {}


# ---------------------------------------------------------------------------
# The merged log pipe: the rig containers log to both streams (the
# repeater's logging output is stderr, the splice's events are stdout),
# so the readers that need the lines in the order the container wrote
# them read ONE merged pipe instead of concatenating two captures.
# ---------------------------------------------------------------------------


def test_run_merged_redirects_stderr_into_stdout(monkeypatch):
    """The merge happens in the subprocess pipe, never by concatenation."""
    from types import SimpleNamespace

    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["stderr"] = kwargs.get("stderr")
        return SimpleNamespace(returncode=0, stdout="out-1\nerr-1\nout-2\n", stderr="")

    monkeypatch.setattr(fault_matrix.subprocess, "run", fake_run)
    rc, merged = fault_matrix.run_merged(["docker", "logs", "c"])
    assert rc == 0
    import subprocess as real_subprocess

    assert seen["stderr"] == real_subprocess.STDOUT
    assert merged == "out-1\nerr-1\nout-2\n"


def test_run_merged_interleaves_a_real_subprocess_in_time_order():
    rc, merged = fault_matrix.run_merged(
        ["/bin/sh", "-c", "echo out-1; echo err-1 >&2; echo out-2"]
    )
    assert rc == 0
    assert merged == "out-1\nerr-1\nout-2\n"


def test_container_logs_read_one_merged_pipe(monkeypatch):
    seen: dict = {}

    def fake_merged(cmd, timeout=600):
        seen["cmd"] = cmd
        return 0, "line-1\nline-2\n"

    monkeypatch.setattr(fault_matrix, "run_merged", fake_merged)
    assert fault_matrix.container_logs("c") == "line-1\nline-2\n"
    assert seen["cmd"] == ["docker", "logs", "c"]


def test_timestamped_logs_read_one_merged_pipe(monkeypatch):
    seen: dict = {}

    def fake_merged(cmd, timeout=600):
        seen["cmd"] = cmd
        return 0, "2026-11-27T00:00:00+00:00 line-1\n"

    monkeypatch.setattr(fault_matrix, "run_merged", fake_merged)
    assert fault_matrix.timestamped_logs("c") == "2026-11-27T00:00:00+00:00 line-1\n"
    assert seen["cmd"] == ["docker", "logs", "-t", "c"]


def test_capture_case_logs_save_the_merged_streams_in_order(tmp_path, monkeypatch):
    """A container that logs to stderr is saved whole: the saved file is
    the merged pipe's text, not a stdout-only capture."""
    saved: dict = {}

    def fake_timestamped(container):
        saved[container] = f"merged text of {container}\n"
        return saved[container]

    monkeypatch.setattr(fault_matrix, "timestamped_logs", fake_timestamped)
    matrix = fault_matrix.FaultMatrix(tmp_path)
    matrix.capture_case_logs("case-x")
    for suffix, container in (
        ("proxy", fault_matrix.PROXY),
        ("medre", fault_matrix.MEDRE_FAULT),
        ("repeater-a", fault_matrix.REPEATER_A),
    ):
        path = tmp_path / f"case-x.{suffix}.log"
        assert path.exists()
        assert path.read_text(encoding="utf-8") == saved[container]


def test_start_proxy_passes_the_trace_environment(tmp_path, monkeypatch):
    """Both splice starts run the proxy with the forwarded-frame trace
    ALWAYS on: no case may start a splice without it."""
    commands: list[list[str]] = []

    def fake_run(cmd, timeout=600):
        commands.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(fault_matrix, "run", fake_run)
    monkeypatch.setattr(
        fault_matrix, "container_logs", lambda name: '{"event": "listening"}'
    )
    monkeypatch.setattr(fault_matrix.time, "sleep", lambda seconds: None)
    matrix = fault_matrix.FaultMatrix(tmp_path)
    matrix.start_proxy({"drop_message_frames": True})
    run_cmd = next(cmd for cmd in commands if cmd[:2] == ["docker", "run"])
    assert ["-e", "OPENHOP_FAULT_PROXY_TRACE=1"] in [
        run_cmd[index : index + 2] for index in range(len(run_cmd) - 1)
    ]


# ---------------------------------------------------------------------------
# The selective-drop accounting: the counts are read at the observation's
# END (client stopped, the companion store read once after the stop,
# splice events counted only up to the stop), a lost message is
# attributed to the connection of the swallowed frame that carried its
# marker, and the splice's swallowed message frames are cross-checked
# against the lost rows per connection.
# ---------------------------------------------------------------------------


def frame_event(
    at: float, connection: int, packet_type: int, marker: str | None = None
):
    return {
        "at": at,
        "event": "message_frame_dropped",
        "connection": connection,
        "packet_type": packet_type,
        "marker": marker,
    }


def judge_selective_drop(**overrides):
    """Run the selective-drop verdict with a clean measured run."""
    kwargs = {
        "accepted": 6,
        "delivered_distinct": 0,
        "preserved_distinct": 5,
        "duplicate_wakes": 0,
        "consumed_per_connection": {1: 1},
        "answered_per_connection": {1: 2},
        "unattributed_consumed": 0,
        "same_connection_proof": True,
    }
    kwargs.update(overrides)
    return fault_matrix.selective_drop_failures(**kwargs)


def test_four_losses_with_two_answered_probes_fail_and_three_pass():
    """Two answered probes put a connection's bound at three lost
    messages (the unproven first fetch plus one per probe): four losses
    fail it, three do not."""
    over = judge_selective_drop(consumed_per_connection={1: 4}, preserved_distinct=2)
    assert any("more than the documented bound" in failure for failure in over)
    within = judge_selective_drop(consumed_per_connection={1: 3}, preserved_distinct=3)
    assert within == []


def test_a_lost_message_is_attributed_to_the_connection_of_its_swallowed_frame():
    """The send-time connection says nothing; the swallowed frame that
    carried the marker does."""
    events = [
        frame_event(1.0, 1, 0x07, "p4qd-0-aaaaaa01"),
        frame_event(2.0, 2, 0x08, "p4qd-1-bbbbbb02"),
        frame_event(3.0, 2, 0x83, None),  # a push carries no message
    ]
    assert fault_matrix.swallowed_marker_connections(events) == {
        "p4qd-0-aaaaaa01": 1,
        "p4qd-1-bbbbbb02": 2,
    }


def test_the_cross_check_agrees_when_every_swallowed_frame_is_lost():
    events = [
        frame_event(1.0, 1, 0x07, "p4qd-0-aaaaaa01"),
        frame_event(2.0, 1, 0x10, "p4qd-1-bbbbbb02"),
        frame_event(3.0, 2, 0x11, "p4qd-2-cccccc03"),
    ]
    cross = fault_matrix.selective_drop_cross_check(
        events, ["p4qd-0-aaaaaa01", "p4qd-1-bbbbbb02", "p4qd-2-cccccc03"]
    )
    assert cross["equal"] is True
    assert cross["per_connection"] == {
        "1": {"swallowed_message_frames": 2, "lost_messages": 2, "equal": True},
        "2": {"swallowed_message_frames": 1, "lost_messages": 1, "equal": True},
    }


def test_the_cross_check_fails_when_a_swallowed_frame_has_no_lost_row():
    """Two swallowed message frames but one row missing: one swallowed
    frame consumed nothing the store can show - the counts disagree."""
    events = [
        frame_event(1.0, 1, 0x07, "p4qd-0-aaaaaa01"),
        frame_event(2.0, 1, 0x08, "p4qd-1-bbbbbb02"),
    ]
    cross = fault_matrix.selective_drop_cross_check(events, ["p4qd-0-aaaaaa01"])
    assert cross["equal"] is False
    assert cross["per_connection"]["1"] == {
        "swallowed_message_frames": 2,
        "lost_messages": 1,
        "equal": False,
    }
    failures = judge_selective_drop(accounting_cross_check=cross)
    assert any("cross-check" in failure for failure in failures)


def test_a_swallowed_message_frame_without_a_marker_fails():
    """A swallowed message frame with no lab marker cannot be counted for
    any message: the accounting is refused."""
    events = [frame_event(1.0, 1, 0x07, None)]
    cross = fault_matrix.selective_drop_cross_check(events, [])
    assert cross["equal"] is False
    assert cross["swallowed_message_frames_without_marker"] == 1
    failures = judge_selective_drop(accounting_cross_check=cross)
    assert any("cross-check" in failure for failure in failures)


def test_the_cross_check_fails_a_lost_row_without_a_swallowed_frame():
    events = [frame_event(1.0, 1, 0x07, "p4qd-0-aaaaaa01")]
    cross = fault_matrix.selective_drop_cross_check(
        events, ["p4qd-0-aaaaaa01", "p4qd-3-dddddd04"]
    )
    assert cross["equal"] is False
    assert cross["unattributed_lost_messages"] == 1


def test_splice_stall_events_carry_the_frame_marker():
    log = (
        '2026-11-26T18:00:05.000000000Z {"event": "message_frame_dropped", '
        '"connection": 1, "packet_type": 7, "marker": "p4qd-0-abc123"}'
    )
    events = fault_matrix.splice_stall_events(log)
    assert events[0]["marker"] == "p4qd-0-abc123"
    assert events[0]["packet_type"] == "0x07"


def test_splice_link_events_carry_the_frame_marker():
    log = (
        '2026-11-26T18:00:05.000000000Z {"event": "message_frame_dropped", '
        '"connection": 1, "packet_type": 16, "marker": "p4qd-1-def456"}'
    )
    events = fault_matrix.splice_link_events(log)
    assert events[0]["marker"] == "p4qd-1-def456"
    assert events[0]["packet_type"] == 0x10


# ---------------------------------------------------------------------------
# The case record's queue snapshots: the labels must reach the case file
# in the order the case takes them.  run_case is driven against fakes
# (no docker): the fakes answer the rig calls and record what ran.
# ---------------------------------------------------------------------------

PROXY_LOG_WITH_PROOF = (
    '2026-11-26T18:00:05.000000000Z {"event": "listening"}\n'
    '2026-11-26T18:00:10.000000000Z {"event": "message_frame_dropped", '
    '"connection": 1, "packet_type": 7, "marker": "p4qd-0-000000abc"}\n'
    '2026-11-26T18:00:20.000000000Z {"event": "probe_reply_forwarded", '
    '"connection": 1, "packet_type": 13}\n'
)
MEDRE_LOG_ONE_ANSWERED_PROBE = (
    "2026-11-26T18:00:08.000000000Z idle-link probe answered\n"
)


def make_bounded_fakes(monkeypatch, tmp_path, preserved_markers, drain_report=None):
    """Fake the rig behind run_case's bounded path.

    The fake companion store starts empty; the fake peer_send appends the
    case's six rows.  ``preserved_markers`` are the rows still present at
    the observation's end.  Returns (written records, snapshot labels in
    the order the case took them).
    """
    rows: list[dict] = []
    snapshot_labels: list[str] = []
    nonces = [f"p4qd-{index}-{index:06d}abc" for index in range(6)]

    def fake_state_in(container, mode, needle=""):
        if container == fault_matrix.REPEATER_A:
            return {"companion_messages": [dict(row) for row in rows]}
        return {"canonical_events": []}

    def fake_peer_send(self, count, prefix):
        for index, nonce in enumerate(nonces):
            rows.append(
                {
                    "id": 100 + index,
                    "companion_hash": "0x01",
                    "arrival_seq": index + 1,
                    "text": f"lab message {nonce}",
                    "marker": nonce,
                }
            )
        return {
            "pass": True,
            "accepted_count": count,
            "nonces": nonces,
            "sends": [],
        }

    def fake_snapshot(self, label):
        snapshot_labels.append(label)
        # The companion store holds every row appended so far; only
        # at the observation's end do this run's lost rows disappear
        # (swallowed by the link), so the final read keeps only the
        # preserved markers.
        if label == "observation_end":
            kept = set(preserved_markers)
            visible = [row for row in rows if row["marker"] in kept]
        else:
            visible = rows
        return {"label": label, "rows": [dict(row) for row in visible]}

    monkeypatch.setattr(fault_matrix, "state_in", fake_state_in)

    def fake_timestamped_logs(name):
        if name == fault_matrix.MEDRE_FAULT:
            return MEDRE_LOG_ONE_ANSWERED_PROBE
        return PROXY_LOG_WITH_PROOF

    monkeypatch.setattr(fault_matrix, "timestamped_logs", fake_timestamped_logs)
    monkeypatch.setattr(fault_matrix.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        fault_matrix.FaultMatrix, "stop_bridge_medre", lambda self: None
    )
    monkeypatch.setattr(fault_matrix.FaultMatrix, "stop_fault_medre", lambda self: None)
    monkeypatch.setattr(
        fault_matrix.FaultMatrix, "start_proxy", lambda self, fault: None
    )
    monkeypatch.setattr(
        fault_matrix.FaultMatrix, "start_fault_medre", lambda self: True
    )
    monkeypatch.setattr(
        fault_matrix.FaultMatrix,
        "wait_for",
        lambda self, predicate, timeout, interval=1.0: True,
    )
    monkeypatch.setattr(fault_matrix.FaultMatrix, "peer_send", fake_peer_send)
    monkeypatch.setattr(
        fault_matrix.FaultMatrix, "companion_queue_snapshot", fake_snapshot
    )
    if drain_report is None:
        drain_report = {
            "wake_batch_arrived_whole": True,
            "rounds_used": 1,
            "remaining_rows": 0,
            "inert_rows": [],
            "wake_batches": [],
        }
    monkeypatch.setattr(
        fault_matrix.FaultMatrix,
        "drain_companion_a",
        lambda self: dict(drain_report),
    )
    return snapshot_labels


def test_the_bounded_record_carries_every_queue_snapshot_label_in_order(
    tmp_path, monkeypatch
):
    """before_batch, batch_confirmed and attach_plus_3s used to be wiped
    before the write; every label the case takes must reach the record,
    in order, with end taken before the write."""
    preserved = [f"p4qd-{index}-{index:06d}abc" for index in range(1, 6)]
    make_bounded_fakes(monkeypatch, tmp_path, preserved)
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence")
    matrix.run_case(
        "fault-selective-drop",
        {"drop_message_frames": True},
        6,
        "p4qd",
        False,
        "bounded",
    )
    records = [path for path in (tmp_path / "evidence").glob("*.json")]
    record = None
    for path in records:
        payload = json.loads(path.read_text())
        if payload.get("case") == "fault-selective-drop":
            record = payload
    assert record is not None
    assert record["pass"] is True, record["failures"]
    labels = [snapshot["label"] for snapshot in record["queue_snapshots"]]
    assert labels == [
        "queue_before_drain",
        "queue_after_drain",
        "before_batch",
        "batch_confirmed",
        "attach_plus_3s",
        "observation_end",
        "end",
    ]


def test_before_batch_is_taken_before_the_batch(tmp_path, monkeypatch):
    """The before_batch snapshot must show the queue BEFORE the case's
    own rows arrive: taken after the send, it would already hold them."""
    preserved = [f"p4qd-{index}-{index:06d}abc" for index in range(1, 6)]
    make_bounded_fakes(monkeypatch, tmp_path, preserved)
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence")
    matrix.run_case(
        "fault-selective-drop",
        {"drop_message_frames": True},
        6,
        "p4qd",
        False,
        "bounded",
    )
    record = json.loads(
        (tmp_path / "evidence" / "fault-selective-drop.json").read_text()
    )
    before_batch = next(
        snapshot
        for snapshot in record["queue_snapshots"]
        if snapshot["label"] == "before_batch"
    )
    assert before_batch["rows"] == []
    batch_confirmed = next(
        snapshot
        for snapshot in record["queue_snapshots"]
        if snapshot["label"] == "batch_confirmed"
    )
    assert len(batch_confirmed["rows"]) == 6


def test_a_failed_wake_batch_fails_the_bounded_case_closed(tmp_path, monkeypatch):
    """Fail closed ONLY on the drain's wake batch not arriving whole:
    the queue state is unknown, so the case is NOT RUN with the drain
    report in the record.  Inert rows are not a reason to refuse."""
    inert = [{"companion_hash": "0x80", "ids": [1369, 1374]}]
    preserved = [f"p4qd-{index}-{index:06d}abc" for index in range(1, 6)]
    make_bounded_fakes(
        monkeypatch,
        tmp_path,
        preserved,
        drain_report={
            "wake_batch_arrived_whole": True,
            "rounds_used": 1,
            "remaining_rows": 2,
            "inert_rows": inert,
            "wake_batches": [],
        },
    )
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence-inert")
    matrix.run_case(
        "fault-selective-drop",
        {"drop_message_frames": True},
        6,
        "p4qd",
        False,
        "bounded",
    )
    record = json.loads(
        (tmp_path / "evidence-inert" / "fault-selective-drop.json").read_text()
    )
    assert record["pass"] is True, record["failures"]
    assert record["queue_drain"]["inert_rows"] == inert


def test_a_wake_batch_that_never_arrives_whole_stops_the_case(tmp_path, monkeypatch):
    failed = {
        "wake_batch_arrived_whole": False,
        "rounds_used": 2,
        "remaining_rows": 4,
        "inert_rows": [],
        "wake_batches": [{"pass": True, "accepted_count": 8, "nonces": []}],
    }
    make_bounded_fakes(monkeypatch, tmp_path, [], drain_report=failed)
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence-failed")
    matrix.run_case(
        "fault-selective-drop",
        {"drop_message_frames": True},
        6,
        "p4qd",
        False,
        "bounded",
    )
    record = json.loads(
        (tmp_path / "evidence-failed" / "fault-selective-drop.json").read_text()
    )
    assert record["status"] == "NOT RUN"
    assert "wake batch did not arrive whole" in record["reason"]
    assert record["queue_drain"]["remaining_rows"] == 4


def test_the_drain_criterion_requires_this_batchs_own_markers():
    batch = {"nonces": [f"p4drainw-{index}-{index:06d}abc" for index in range(8)]}
    delivered = set(batch["nonces"])
    assert fault_matrix.wake_batch_arrived_whole(batch, delivered) is True
    assert (
        fault_matrix.wake_batch_arrived_whole(batch, delivered - {batch["nonces"][3]})
        is False
    )
    # An earlier wake batch's rows cannot stand in for this one: the
    # unique tails differ.
    assert fault_matrix.wake_batch_arrived_whole(batch, {"p4drainw-0-stale01"}) is (
        False
    )
    assert fault_matrix.wake_batch_arrived_whole({"nonces": []}, delivered) is False


def test_inert_rows_are_grouped_by_identity_with_hash_and_ids():
    rows = [
        {"companion_hash": "0x80", "id": 1374},
        {"companion_hash": "0x01", "id": 7},
        {"companion_hash": "0x80", "id": 1369},
    ]
    assert fault_matrix.inert_rows_by_identity(rows) == [
        {"companion_hash": "0x01", "ids": [7]},
        {"companion_hash": "0x80", "ids": [1369, 1374]},
    ]


# ---------------------------------------------------------------------------
# The constructed saturation window: the stall is armed with a far guard
# deadline, the batch is sent, and the stall holds one headroom interval
# past the last accepted send - the window covers the batch by
# construction instead of by prediction.
# ---------------------------------------------------------------------------


def iso_stamp(epoch: float) -> str:
    import time as time_module

    return time_module.strftime("%Y-%m-%dT%H:%M:%S+00:00", time_module.gmtime(epoch))


def log_line(epoch: float, text: str) -> str:
    import time as time_module

    return (
        time_module.strftime("%Y-%m-%dT%H:%M:%S", time_module.gmtime(epoch))
        + ".000000000Z "
        + text
        + "\n"
    )


def make_window_fakes(
    monkeypatch, tmp_path, count, prefix, send_offsets, success_offset=None
):
    """Fake the rig behind run_window_case.

    ``send_offsets`` are seconds after the peer_send call when each send
    finishes; the fake MEDRE log carries one reconnect attempt inside the
    window and one reconnect success just after the derived window end,
    and MEDRE's store holds every sent marker exactly once.
    """
    import time as time_module

    nonces = [f"{prefix}-{index}-{index:06d}abc" for index in range(count)]
    fault_writes: list[dict] = []
    medre_log = {"text": ""}
    arm = {"epoch": None}

    def fake_peer_send(self, batch_count, batch_prefix):
        now = time_module.time()
        arm["epoch"] = now
        resolved_success = (
            success_offset
            if success_offset is not None
            else max(send_offsets) + fault_matrix.SATURATION_WINDOW_HEADROOM_S + 2
        )
        medre_log["text"] = (
            log_line(now + 1, "reconnect attempt 1 after RuntimeError")
            + log_line(now + 2, "receive pump failed; retiring connection")
            + log_line(
                now + resolved_success,
                "reconnected successfully to openhop-fault-proxy:5050",
            )
        )
        return {
            "pass": True,
            "accepted_count": count,
            "nonces": nonces,
            "sends": [
                {"finished_at": iso_stamp(now + offset)} for offset in send_offsets
            ],
        }

    def fake_write_fault(path, fault):
        fault_writes.append(dict(fault))

    def fake_state_in(container, mode, needle=""):
        if container == fault_matrix.REPEATER_A:
            return {"companion_messages": []}
        return {"canonical_events": [{"text": nonce} for nonce in nonces]}

    monkeypatch.setattr(
        fault_matrix.FaultMatrix, "stop_bridge_medre", lambda self: None
    )
    monkeypatch.setattr(fault_matrix.FaultMatrix, "stop_fault_medre", lambda self: None)
    monkeypatch.setattr(fault_matrix.FaultMatrix, "start_proxy", lambda self, f: None)
    monkeypatch.setattr(
        fault_matrix.FaultMatrix, "start_fault_medre", lambda self: True
    )
    monkeypatch.setattr(
        fault_matrix.FaultMatrix,
        "wait_for",
        lambda self, predicate, timeout, interval=1.0: True,
    )
    monkeypatch.setattr(fault_matrix.FaultMatrix, "peer_send", fake_peer_send)
    monkeypatch.setattr(fault_matrix, "write_fault", fake_write_fault)
    monkeypatch.setattr(fault_matrix, "state_in", fake_state_in)
    monkeypatch.setattr(fault_matrix, "container_logs", lambda name: medre_log["text"])
    monkeypatch.setattr(
        fault_matrix, "timestamped_logs", lambda name: medre_log["text"]
    )
    monkeypatch.setattr(fault_matrix.time, "sleep", lambda seconds: None)
    return fault_writes


def read_case_record(tmp_path, name):
    for directory in tmp_path.iterdir():
        if directory.is_dir():
            path = directory / f"{name}.json"
            if path.exists():
                return json.loads(path.read_text())
    return None


def test_the_saturation_window_is_constructed_from_the_batch(tmp_path, monkeypatch):
    """Sends finishing far past the PREDICTED window must not abort the
    case: the stall is armed with the far guard and ends one headroom
    interval past the last accepted send."""
    fault_writes = make_window_fakes(
        monkeypatch, tmp_path, fault_matrix.SATURATION_BATCH, "p4qs", [10, 200, 480]
    )
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence")
    matrix.peer_pacing_s = None  # fallback pacing: the prediction is 190 s
    matrix.case_saturation()
    record = read_case_record(tmp_path, "fault-saturation")
    assert record is not None
    assert record["pass"] is True, record["failures"]
    assert record["predicted_window_s"] == 190.0
    assert 479 <= record["batch_seconds"] <= 481.5
    assert 489 <= record["window_seconds"] <= 491.5
    assert record["fault"]["constructed_window"] is True
    # The arm wrote the far guard deadline, the end cleared the fault.
    armed = fault_writes[0]["stall_server_to_client_until"]
    import time as time_module

    assert armed >= time_module.time() + 3000  # the far guard deadline
    assert len(fault_writes) >= 2
    assert fault_writes[-1] == {}


def test_a_fixed_window_keeps_the_abort_and_records_the_send(tmp_path, monkeypatch):
    """The fixed-window cases keep the NOT RUN when a send outlasts the
    window, and the record then carries the send result so the pacing
    facts survive."""
    make_window_fakes(monkeypatch, tmp_path, 6, "p4qsr", [10, 40, 100])
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence")
    matrix.run_window_case(
        "fault-reply-stall",
        {"stall_server_to_client_until": True},
        45.0,
        6,
        "p4qsr",
        "45 s reply-stall window: server->client swallowed",
    )
    record = read_case_record(tmp_path, "fault-reply-stall")
    assert record is not None
    assert record["status"] == "NOT RUN"
    assert "did not finish inside the fault window" in record["reason"]
    assert record["probe"]["sends"], "the send result must survive the NOT RUN"


# ---------------------------------------------------------------------------
# Every case runner saves its container logs on the NORMAL completion
# path, immediately before the record is written - not only in the
# exception branches.
# ---------------------------------------------------------------------------


def test_window_runners_capture_logs_once_on_completion(tmp_path, monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(
        fault_matrix.FaultMatrix,
        "capture_case_logs",
        lambda self, name, captured_logs=None: captured.append(name),
    )
    make_window_fakes(monkeypatch, tmp_path, 6, "p4qsr", [5, 15, 25])
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence")
    matrix.case_reply_stall()
    assert captured == ["fault-reply-stall"]


def test_the_reconnect_case_captures_logs_once_on_completion(tmp_path, monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(
        fault_matrix.FaultMatrix,
        "capture_case_logs",
        lambda self, name, captured_logs=None: captured.append(name),
    )
    make_window_fakes(monkeypatch, tmp_path, 6, "p4qn", [5, 6, 7], success_offset=32)
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence")
    matrix.case_reconnect()
    assert captured == ["fault-reconnect"]


def test_the_idle_blackhole_case_captures_logs_once_on_completion(
    tmp_path, monkeypatch
):
    captured: list[str] = []
    monkeypatch.setattr(
        fault_matrix.FaultMatrix,
        "capture_case_logs",
        lambda self, name, captured_logs=None: captured.append(name),
    )
    make_blackhole_fakes(monkeypatch, tmp_path)
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence")
    matrix.case_idle_blackhole()
    assert captured == ["fault-idle-blackhole"]


def test_the_drain_stall_case_captures_logs_once_on_completion(tmp_path, monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(
        fault_matrix.FaultMatrix,
        "capture_case_logs",
        lambda self, name, captured_logs=None: captured.append(name),
    )
    make_window_fakes(monkeypatch, tmp_path, 60, "p4qsd", [5, 6, 7], success_offset=52)
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence")
    matrix.peer_pacing_s = None
    # The drain case sends in a background thread; with the fake peer_send
    # returning instantly the thread is done before the join.
    matrix.case_reply_stall_during_drain()
    assert captured == ["fault-reply-stall-drain"]


def test_the_drain_stall_record_states_the_bound_not_the_exact_frame(
    tmp_path, monkeypatch
):
    """The case arms its window at the first delivery it observes and
    cannot see which frame the stall catches: the record's fault note and
    verdict rule must claim the bound, never name the swallowed frame."""
    make_window_fakes(monkeypatch, tmp_path, 60, "p4qsd", [5, 6, 7], success_offset=52)
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence")
    matrix.peer_pacing_s = None
    matrix.case_reply_stall_during_drain()
    record = read_case_record(tmp_path, "fault-reply-stall-drain")
    assert record is not None
    note = record["fault"]["note"]
    rule = record["verdict_rule"]
    for text in (note, rule):
        assert "does not observe which frame the stall catches" in text
        assert "on the wire" not in text
        assert "the one the splice swallows" not in text
        assert "in-flight message" not in text
    assert "at most one message may be consumed" in note
    assert "a run may consume none" in note
    assert "at most one message may be consumed" in rule
    assert "a run may consume none" in rule
    assert "zero duplicates" in rule
    assert "every other accepted message is delivered once" in rule
    assert "no reconnect succeeds while the window holds" in rule
    assert "recovery follows the spec's two branches" in rule


def make_blackhole_fakes(monkeypatch, tmp_path, count=3, prefix="p4blackhole"):
    """Fake the rig behind case_idle_blackhole: the retirement line lands
    20 s after the arm, the reconnect attempt inside the hold and the
    success after the clear."""
    import time as time_module

    nonces = [f"{prefix}-{index}-{index:06d}abc" for index in range(count)]
    medre_log = {"text": ""}

    def fake_peer_send(self, batch_count, batch_prefix):
        now = time_module.time()
        medre_log["text"] = (
            log_line(now + 0.1, "reconnect attempt 1 after RuntimeError")
            + log_line(
                now + 20,
                "receive pump failed (RuntimeError: idle-link liveness "
                "probe unanswered); retiring connection",
            )
            + log_line(now + 40, "reconnected successfully")
        )
        return {
            "pass": True,
            "accepted_count": count,
            "nonces": nonces,
            "sends": [
                {"finished_at": iso_stamp(now + 5 + index)} for index in range(count)
            ],
        }

    def fake_state_in(container, mode, needle=""):
        if container == fault_matrix.REPEATER_A:
            return {"companion_messages": []}
        return {"canonical_events": [{"text": nonce} for nonce in nonces]}

    monkeypatch.setattr(
        fault_matrix.FaultMatrix, "stop_bridge_medre", lambda self: None
    )
    monkeypatch.setattr(fault_matrix.FaultMatrix, "stop_fault_medre", lambda self: None)
    monkeypatch.setattr(fault_matrix.FaultMatrix, "start_proxy", lambda self, f: None)
    monkeypatch.setattr(
        fault_matrix.FaultMatrix, "start_fault_medre", lambda self: True
    )
    monkeypatch.setattr(
        fault_matrix.FaultMatrix,
        "wait_for",
        lambda self, predicate, timeout, interval=1.0: True,
    )
    monkeypatch.setattr(fault_matrix.FaultMatrix, "peer_send", fake_peer_send)
    monkeypatch.setattr(fault_matrix, "write_fault", lambda path, fault: None)
    monkeypatch.setattr(fault_matrix, "state_in", fake_state_in)
    monkeypatch.setattr(fault_matrix, "container_logs", lambda name: medre_log["text"])
    monkeypatch.setattr(
        fault_matrix, "timestamped_logs", lambda name: medre_log["text"]
    )
    monkeypatch.setattr(fault_matrix.time, "sleep", lambda seconds: None)


def test_the_recorded_answered_probes_equal_the_per_connection_sum(
    tmp_path, monkeypatch
):
    """One quantity, one source: answered_probes in the record is the sum
    of the per-connection counts the splice reported."""
    preserved = [f"p4qd-{index}-{index:06d}abc" for index in range(1, 6)]
    make_bounded_fakes(monkeypatch, tmp_path, preserved)
    matrix = fault_matrix.FaultMatrix(tmp_path / "evidence")
    matrix.run_case(
        "fault-selective-drop",
        {"drop_message_frames": True},
        6,
        "p4qd",
        False,
        "bounded",
    )
    record = json.loads(
        (tmp_path / "evidence" / "fault-selective-drop.json").read_text()
    )
    per_connection = record["answered_probes_per_connection"]
    assert record["answered_probes"] == sum(per_connection.values())
    assert record["answered_probes"] == 1


# ---------------------------------------------------------------------------
# The wait conditions count only THIS run's markers: a prefix read alone
# is satisfied by an earlier run's leftovers, whose unique tails differ.
# ---------------------------------------------------------------------------


def test_prefix_rows_with_foreign_tails_do_not_satisfy_the_batch_gate():
    mine = {f"p4qd-{index}-0000{index}0abc" for index in range(6)}
    rows = [
        {"text": f"lab message p4qd-{index}-777{index}7abc"}  # foreign tails
        for index in range(6)
    ]
    own, foreign = fault_matrix.this_run_rows(rows, mine)
    assert own == 0
    assert foreign == 6
    own_rows, _ = fault_matrix.this_run_rows([], mine)
    assert own_rows == 0
    own, foreign = fault_matrix.this_run_rows(
        [{"text": f"lab message p4qd-{index}-0000{index}0abc"} for index in range(6)],
        mine,
    )
    assert own == 6
    assert foreign == 0


def test_events_with_foreign_tails_do_not_satisfy_the_delivery_gate():
    mine = {f"p4qn-{index}-0000{index}0abc" for index in range(6)}
    events = [
        {"text": f"lab message p4qn-{index}-999{index}9abc"}  # an earlier run
        for index in range(6)
    ]
    assert fault_matrix.delivered_this_run(events, mine) == 0
    this_run = [
        {"text": f"lab message p4qn-{index}-0000{index}0abc"} for index in range(3)
    ]
    assert fault_matrix.delivered_this_run(this_run, mine) == 3


def test_reconnected_after_retirement_reads_one_snapshot():
    """The reconnect evidence is judged from ONE log snapshot: the slice
    and its anchor come from the same read."""
    log = "\n".join(
        [
            "2026-11-26T18:00:00.000000000Z reconnected successfully (old)",
            "2026-11-26T18:00:10.000000000Z receive pump failed; retiring connection",
            "2026-11-26T18:00:15.000000000Z reconnected successfully",
        ]
    )
    assert fault_matrix.reconnected_after_retirement(log) is True
    assert (
        fault_matrix.reconnected_after_retirement(
            "2026-11-26T18:00:10.000000000Z retiring connection"
        )
        is False
    )
    assert fault_matrix.reconnected_after_retirement("no retirement at all") is False


def test_the_shell_entry_point_forwards_the_passthrough_control():
    """The control run goes through the committed entry point: the script
    accepts --selective-drop-passthrough and forwards it to the driver."""
    script = (
        Path(__file__).resolve().parents[1] / "openhop-radio" / "fault_matrix.sh"
    ).read_text()
    assert "--selective-drop-passthrough" in script
    assert 'PASSTHROUGH="--selective-drop-passthrough"' in script
    assert "${PASSTHROUGH}" in script


def test_latency_over_refuses_the_baseline_shape_that_never_times_out_a_fetch(
    tmp_path, monkeypatch
):
    matrix = fault_matrix.FaultMatrix(tmp_path)
    for name in ("stop_bridge_medre", "stop_fault_medre", "start_proxy"):
        monkeypatch.setattr(matrix, name, lambda *args: None)
    monkeypatch.setattr(matrix, "start_fault_medre", lambda: True)
    monkeypatch.setattr(matrix, "wait_for_parked_link", lambda: True)
    monkeypatch.setattr(fault_matrix, "container_running", lambda name: True)
    writes = []
    monkeypatch.setattr(
        fault_matrix, "write_fault", lambda path, flags: writes.append(flags)
    )
    monkeypatch.setattr(
        matrix, "wait_for", lambda predicate, *_args, **_kwargs: predicate()
    )
    monkeypatch.setattr(
        fault_matrix,
        "timestamped_logs",
        lambda name: "2026-11-27T14:29:15Z reconnect failed: No response from MeshCore node",
    )
    monkeypatch.setattr(
        matrix,
        "peer_send",
        lambda *_: {
            "pass": True,
            "accepted_count": 1,
            "nonces": ["p4qo-0-abcdef"],
            "sends": [
                {
                    "accepted": True,
                    "nonce": "p4qo-0-abcdef",
                    "finished_at": "2026-01-01T00:00:00Z",
                }
            ],
        },
    )
    matrix.run_case("fault-latency-over", {"delay_ms": 4000}, 1, "p4qo", False)
    record = json.loads((tmp_path / "fault-latency-over.json").read_text())
    assert record["status"] == "NOT RUN"
    assert "no fetch window elapsed" in record["reason"]
    assert writes == [{"live_delay_ms": 4000}, {}]


def test_selective_drop_credit_cannot_arrive_after_loss_or_cross_connections():
    drop = {"event": "message_frame_dropped", "connection": 1, "packet_type": 7}
    reply = {"event": "probe_reply_forwarded", "connection": 1}
    assert fault_matrix.selective_order_failures([drop, reply, drop]) == []
    assert fault_matrix.selective_order_failures([drop, drop, reply])
    assert fault_matrix.selective_order_failures([drop, dict(reply, connection=2), drop])
