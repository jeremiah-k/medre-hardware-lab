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
