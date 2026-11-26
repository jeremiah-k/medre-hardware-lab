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
    """Run the selective-drop verdict with the documented bound met."""
    kwargs = {
        "accepted": 6,
        "delivered_distinct": 1,
        "preserved_distinct": 3,
        "duplicate_wakes": 0,
        "answered_probes": 1,
    }
    kwargs.update(overrides)
    return fault_matrix.selective_drop_failures(**kwargs)


def test_consumption_within_the_documented_bound_passes():
    """One consumed message per answered probe is the spec's bound."""
    # consumed = 6 - 1 - 3 = 2 <= 1 + 1 answered probe.
    assert judge_bounded() == []
    assert judge_bounded(delivered_distinct=2, preserved_distinct=4) == []


def test_the_consumed_count_is_reported_not_hidden():
    """A passing run still reports what the link ate; the evidence does."""
    # The verdict passes at the bound; the consumed count is computed by
    # the case as accepted - delivered - preserved and written to evidence.
    assert judge_bounded() == []


def test_consumption_beyond_the_bound_fails():
    failures = judge_bounded(
        delivered_distinct=0, preserved_distinct=0, answered_probes=1
    )
    assert any("more than the documented bound" in failure for failure in failures)


def test_zero_consumption_passes_the_bounded_case():
    assert judge_bounded(delivered_distinct=6, preserved_distinct=0) == []


def test_a_duplicate_wake_fails_the_bounded_case():
    failures = judge_bounded(duplicate_wakes=1)
    assert any("duplicate" in failure for failure in failures)


def test_an_impossible_accounting_fails_the_bounded_case():
    failures = judge_bounded(delivered_distinct=6, preserved_distinct=2)
    assert any("exceeds the 6 accepted" in failure for failure in failures)


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
    recovering = {"latency_under", "repeated_waiting", "reconnect"}
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
