"""Offline tests for the backlog/persistence matrix helpers.

These cover the counting rules the matrix relies on, so a wrong count is a
test failure rather than a wrong number in an evidence file:

* a nonce-bearing message counts even when it renders imperfectly (a
  corrupted duplicate still carries the marker and is counted);
* duplicates, losses and ordering are reported separately;
* the missing-index helper names exactly the nonces that never arrived.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "openhop_backlog_matrix.py"
)
spec = importlib.util.spec_from_file_location("openhop_backlog_matrix", MODULE_PATH)
assert spec and spec.loader
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


def test_nonce_index_extracts_the_marker_index():
    assert matrix.nonce_index("p4c1-17-9f2ab1") == 17
    assert matrix.nonce_index("MEDRE-RADIO-b: p4c2l-3-deadbeef") == 3


def test_nonce_marker_keeps_the_unique_tail():
    """The unique tail is what makes one message one message."""
    assert matrix.nonce_marker("p4c1-17-9f2ab1") == "p4c1-17-9f2ab1"
    assert matrix.nonce_marker("p4c1-17-other") != "p4c1-17-9f2ab1"


def test_nonce_index_ignores_text_without_a_marker():
    assert matrix.nonce_index("") is None
    assert matrix.nonce_index("no marker here") is None
    assert matrix.nonce_index(None) is None


def test_count_nonce_rows_counts_corrupted_duplicates():
    rows = [
        {"text": "p4c1-0-aaaa"},
        {"text": "p4c1-0-aaaa"},  # a duplicate, rendered the same
        {"text": "p4c1-1-cccc"},
        {"text": "unrelated"},
    ]
    count, markers = matrix.count_nonce_rows(rows)
    assert count == 3
    assert markers == ["p4c1-0-aaaa", "p4c1-0-aaaa", "p4c1-1-cccc"]


def test_count_nonce_rows_can_be_scoped_to_this_run():
    """MEDRE's store is cumulative; a re-run must not count the old rows."""
    rows = [{"text": "p4c1-0-aaaa"}, {"text": "p4c1-0-old0"}]
    count, markers = matrix.count_nonce_rows(rows, only={"p4c1-0-aaaa"})
    assert count == 1
    assert markers == ["p4c1-0-aaaa"]


def test_ordering_report_separates_duplicates_losses_and_order():
    report = matrix.ordering_report(["c1-0-a", "c1-1-a", "c1-2-a", "c1-2-a", "c1-4-a"])
    assert report["arrivals"] == 5
    assert report["distinct"] == 4
    assert report["duplicate_arrivals"] == 1
    assert report["out_of_order_arrivals"] == 0
    assert report["distinct_indices"] == ["c1-0-a", "c1-1-a", "c1-2-a", "c1-4-a"]


def test_ordering_report_flags_an_out_of_order_arrival():
    report = matrix.ordering_report(["p4c1-2-aa", "p4c1-1-bb", "p4c1-0-cc"])
    assert report["out_of_order_arrivals"] == 2
    assert report["duplicate_arrivals"] == 0


def test_every_case_is_registered():
    assert set(matrix.CASES) == {"c1", "c2", "c3", "c4", "c5", "c6"}


def test_two_cases_in_one_run_do_not_collide():
    """Two cases number their nonces from 0, so the index alone collides."""
    backlog = [f"p4c2b-{i}-aa" for i in range(5)]
    live = [f"p4c2l-{i}-bb" for i in range(5)]
    report = matrix.ordering_report(backlog + live)
    assert report["arrivals"] == 10
    assert report["distinct"] == 10
    assert report["duplicate_arrivals"] == 0
    assert report["out_of_order_arrivals"] == 0


def test_ordering_is_judged_within_a_case():
    report = matrix.ordering_report(["c2b-0-a", "c2b-1-a", "c2l-0-b", "c2b-2-a"])
    assert report["out_of_order_arrivals"] == 0
    assert report["duplicate_arrivals"] == 0


def test_missing_indices_names_the_markers_that_never_arrived():
    markers = ["c2b-0-a", "c2b-2-a"]
    assert matrix.missing_indices(markers, {"c2b-0-a", "c2b-1-a", "c2b-2-a"}) == [
        "c2b-1-a"
    ]
    assert matrix.missing_indices(markers, set(markers)) == []


def test_sent_markers_scopes_a_probe_result_to_its_own_nonces():
    markers = matrix.sent_markers({"nonces": ["p4c2b-0-aa", "p4c2b-1-bb"]})
    assert markers == {"p4c2b-0-aa", "p4c2b-1-bb"}


def test_kill_mid_drain_judge_passes_the_designed_run():
    """The designed run: one outstanding message, queue held, one missing."""
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=29,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=["p4c5ab12-0-deadbeef"],
        delivered_before_kill=0,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=7.5,
    )
    assert failures == []


def test_kill_mid_drain_judge_fails_a_shrinking_queue():
    """A queue that kept shrinking means more than one message was popped."""
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=27,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=["p4c5ab12-0-deadbeef", "p4c5ab12-1-aa", "p4c5ab12-2-bb"],
        delivered_before_kill=0,
        queue_samples=[29, 27, 26, 24],
        hold_seconds=7.5,
    )
    assert any("kept shrinking" in failure for failure in failures)
    assert any("3 message(s) are missing" in failure for failure in failures)


def test_kill_mid_drain_judge_fails_a_short_hold():
    """The hold must span longer than two fetch windows (2 s each)."""
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=29,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=["p4c5ab12-0-deadbeef"],
        delivered_before_kill=0,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=3.0,
    )
    assert any("less than two fetch windows" in failure for failure in failures)


def test_kill_mid_drain_judge_fails_a_wrong_missing_message():
    """The drain pops oldest first, so the withheld reply is the first."""
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=29,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=["p4c5ab12-7-deadbeef"],
        delivered_before_kill=0,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=7.5,
    )
    assert any("oldest first" in failure for failure in failures)


def test_kill_mid_drain_judge_fails_something_delivered_or_preserved():
    """Nothing may be delivered before the kill or preserved afterwards."""
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=29,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=["p4c5ab12-0-deadbeef"],
        delivered_before_kill=2,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=7.5,
    )
    assert any("delivered before the kill" in failure for failure in failures)
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=28,
        duplicate_wakes=0,
        preserved_distinct=1,
        missing=["p4c5ab12-0-deadbeef"],
        delivered_before_kill=0,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=7.5,
    )
    assert any("still queued at the companion" in failure for failure in failures)


def test_kill_mid_drain_judge_fails_duplicates_and_shortfalls():
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=28,
        duplicate_wakes=1,
        preserved_distinct=0,
        missing=["p4c5ab12-0-deadbeef"],
        delivered_before_kill=0,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=7.5,
    )
    assert any("duplicate wake" in failure for failure in failures)
    assert any("!= 29 of 30" in failure for failure in failures)


def test_kill_mid_drain_judge_fails_no_accepted_messages():
    failures = matrix.kill_mid_drain_failures(
        accepted=0,
        delivered_distinct=0,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=[],
        delivered_before_kill=0,
        queue_samples=[],
        hold_seconds=None,
    )
    assert failures == ["the peer sent no accepted message"]
