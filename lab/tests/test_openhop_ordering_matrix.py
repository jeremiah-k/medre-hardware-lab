"""Offline tests for the companion ordering cases (memory vs SQLite).

These pin the verdict logic the ordering cases rely on, so a wrong
conclusion is a test failure rather than a wrong line in an evidence file:

* the merged block verdict fails when a NEWER in-memory message overtakes
  an OLDER persisted message (the inversion the ordering fix removes);
* within-block order is checked, and duplicates are counted by the FULL
  marker;
* a correct OLDER-then-NEWER run produces no violations.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "openhop_ordering_matrix.py"
)
spec = importlib.util.spec_from_file_location("openhop_ordering_matrix", MODULE_PATH)
assert spec and spec.loader
ordering = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ordering)


def marker(block: str, index: int, unique: str = "aa") -> str:
    return f"p4{block}-{index}-{unique}"


def test_a_correct_older_then_newer_run_has_no_violations():
    markers = [marker("o1a", i, f"{i:02x}") for i in range(5)] + [
        marker("o1b", i, f"{i:02x}") for i in range(5)
    ]
    verdict = ordering.merged_block_verdict(markers, ["o1a", "o1b"])
    assert verdict["arrivals"] == 10
    assert verdict["distinct"] == 10
    assert verdict["duplicate_arrivals"] == 0
    assert verdict["cross_block_violations"] == 0
    assert verdict["within_block_violations"] == []


def test_a_newer_memory_message_overtaking_an_older_row_is_a_violation():
    markers = [
        marker("o1a", 0),
        marker("o1a", 1),
        marker("o1b", 0),
        marker("o1a", 2),
    ]
    verdict = ordering.merged_block_verdict(markers, ["o1a", "o1b"])
    assert verdict["cross_block_violations"] == 1


def test_within_block_order_is_checked():
    markers = [marker("o2a", 0), marker("o2a", 2), marker("o2a", 1)]
    verdict = ordering.merged_block_verdict(markers, ["o2a"])
    assert verdict["within_block_violations"] == ["o2a: index 1 arrived after index 2"]


def test_duplicates_are_counted_by_the_full_marker():
    markers = [marker("o3", 0, "ab"), marker("o3", 0, "ab"), marker("o3", 1, "cd")]
    verdict = ordering.merged_block_verdict(markers, ["o3"])
    assert verdict["duplicate_arrivals"] == 1
    assert verdict["distinct"] == 2


def test_a_missing_block_is_reported_without_a_false_cross_violation():
    verdict = ordering.merged_block_verdict([marker("o2a", 0)], ["o2a", "o2b"])
    assert verdict["cross_block_violations"] == 0
    assert verdict["distinct"] == 1


def test_every_ordering_case_is_registered():
    assert set(ordering.CASES) == {"o1", "o2", "o3"}


# ---------------------------------------------------------------------------
# The exit contract: 0 only when every case this invocation ran is PASS;
# a FAIL or a NOT RUN makes it 1; --summary-only writes no case and stays 0.
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_when_a_case_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ordering.case_summary, "bind_source_selection", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        ordering.OrderingMatrix,
        "case_o1",
        lambda self: self.write("o1-older-persisted-newer-memory", {"pass": False}),
    )
    assert ordering.main(["--evidence-dir", str(tmp_path / "ev"), "--case", "o1"]) == 1


def test_main_exits_nonzero_when_a_case_was_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ordering.case_summary, "bind_source_selection", lambda *args, **kwargs: None
    )

    def not_run(self):
        self.write("o2-restart-backlog-then-live", {"pass": False})
        self.results[-1]["status"] = "NOT RUN"

    monkeypatch.setattr(ordering.OrderingMatrix, "case_o2", not_run)
    assert ordering.main(["--evidence-dir", str(tmp_path / "ev2"), "--case", "o2"]) == 1


def test_main_exits_zero_when_every_ran_case_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ordering.case_summary, "bind_source_selection", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        ordering.OrderingMatrix,
        "case_o3",
        lambda self: self.write("o3-store-locked-memory-served", {"pass": True}),
    )
    assert ordering.main(["--evidence-dir", str(tmp_path / "ev3"), "--case", "o3"]) == 0


def test_summary_only_exits_zero_without_running_cases(tmp_path):
    assert (
        ordering.main(["--evidence-dir", str(tmp_path / "ev4"), "--summary-only"]) == 0
    )


# ---------------------------------------------------------------------------
# The cross-store merge premise: the newer (or live) block must be in
# memory ONLY when the client drains - nothing persisted while the store
# was locked, and at least one row still unpersisted at the drain.  The
# decision is a pure function; both counts land in the record either way,
# and the drain runs before the NOT RUN so no rows are left behind.
# ---------------------------------------------------------------------------


def test_the_merge_premise_holds_with_nothing_persisted_early():
    assert ordering.merge_premise_reason(0, 0, 3) is None


def test_the_merge_premise_holds_with_some_but_not_all_persisted_before_the_drain():
    assert ordering.merge_premise_reason(0, 1, 3) is None


def test_the_merge_premise_fails_when_the_lock_did_not_hold():
    reason = ordering.merge_premise_reason(2, 2, 3)
    assert reason is not None
    assert "while the store was locked" in reason


def test_the_merge_premise_fails_when_everything_was_persisted_before_the_drain():
    reason = ordering.merge_premise_reason(0, 3, 3)
    assert reason is not None
    assert "before the drain" in reason


def test_the_merge_premise_tolerates_an_empty_block():
    assert ordering.merge_premise_reason(0, 0, 0) is None
