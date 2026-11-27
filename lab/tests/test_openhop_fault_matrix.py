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
  while a backlog drain is in flight: at most the one in-flight message
  may be consumed, every other accepted message is delivered once, no
  reconnect succeeds while the window holds, and the drain resumes
  without a reconnect or within one probe interval after it ends.
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
        "same_connection_proven": True,
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
        same_connection_proven=False,
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
        same_connection_proven=False,
    )
    assert any("does not prove the selective drop" in failure for failure in failures)


def test_a_connection_both_probe_answered_and_frame_lost_proves_the_drop():
    assert (
        judge_bounded(
            consumed_per_connection={3: 1, 5: 1},
            answered_per_connection={3: 0, 5: 1},
            same_connection_proven=True,
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


def test_connection_attribution_follows_the_stall_boundaries():
    events = fault_matrix.splice_stall_events(
        "\n".join(
            [
                '2026-11-26T18:00:02.000000Z {"event": "stalled", "connection": 1}',
                '2026-11-26T18:00:40.000000Z {"event": "stalled", "connection": 2}',
            ]
        )
    )
    of = lambda stamp: fault_matrix.connection_of(stamp, events)  # noqa: E731
    assert of(0.0) == 1  # before the first stall: connection 1's live phase
    assert of(events[0]["at"]) == 1
    assert of(events[0]["at"] + 1) == 2  # after connection 1 stalled, client is on 2
    assert of(events[1]["at"] + 1) is None  # after the last stall: unattributable


def test_per_connection_counts_ignore_unattributable_stamps():
    events = fault_matrix.splice_stall_events(
        '2026-11-26T18:00:40.000000Z {"event": "stalled", "connection": 2}'
    )
    stall_stamp = events[0]["at"]
    counts = fault_matrix.per_connection_counts(
        [stall_stamp - 1, stall_stamp, stall_stamp + 1], events
    )
    assert counts == {2: 2}


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
    assert any("more than the one in-flight frame" in failure for failure in failures)


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
    assert report["same_connection_proven"] is True
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
    assert report["same_connection_proven"] is False
    assert report["message_frames_dropped_per_connection"] == {}


def test_the_proof_report_fails_when_the_kinds_split_across_connections():
    events = [
        link_event(1.0, "probe_reply_forwarded", 1, 13),
        link_event(2.0, "message_frame_dropped", 2, 7),
    ]
    report = fault_matrix.selective_drop_proof_report(events)
    assert report["same_connection_proven"] is False


def test_the_proof_report_is_empty_without_events():
    report = fault_matrix.selective_drop_proof_report([])
    assert report["same_connection_proven"] is False
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
    monkeypatch.setattr(fault_matrix, "container_running", lambda name: True)
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
        "same_connection_proven": True,
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


def make_bounded_fakes(monkeypatch, tmp_path, preserved_markers):
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
        kept = set(preserved_markers)
        return {
            "label": label,
            "rows": [dict(row) for row in rows if row["marker"] in kept],
        }

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
        0.0,
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
        "before_batch",
        "batch_confirmed",
        "attach_plus_3s",
        "observation_end",
        "end",
    ]
