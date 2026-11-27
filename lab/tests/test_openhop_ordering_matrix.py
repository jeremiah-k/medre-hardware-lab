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
import json
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
    assert set(ordering.CASES) == {"o1", "o2", "o3", "o4"}


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
# The cross-store merge premise: the newer block must be in memory ONLY
# when the client drains.  The o1 store lock outlasts the repeater's
# whole persist-retry budget, so the premise is strict - BOTH pre-drain
# counts must be 0; any other combination fails it.  The decision is a
# pure function; the counts land in the record either way, and the drain
# runs before the NOT RUN so no rows are left behind.
# ---------------------------------------------------------------------------


def test_the_merge_premise_holds_with_nothing_persisted_before_the_drain():
    assert ordering.merge_premise_reason(0, 0) is None


def test_the_merge_premise_fails_when_any_row_was_persisted_while_the_lock_held():
    reason = ordering.merge_premise_reason(1, 0)
    assert reason is not None
    assert "while the store was locked" in reason


def test_the_merge_premise_fails_when_any_row_was_persisted_before_the_drain():
    reason = ordering.merge_premise_reason(0, 1)
    assert reason is not None
    assert "before the drain" in reason
    assert "persist-retry budget" in reason


def test_a_row_persisted_while_locked_is_reported_before_a_row_persisted_later():
    reason = ordering.merge_premise_reason(2, 2)
    assert reason is not None
    assert "while the store was locked" in reason


# ---------------------------------------------------------------------------
# The o4 premise: the OLDER block must be in MEMORY ONLY and the NEWER
# block FULLY PERSISTED when the client drains.  The three OLDER
# persisted counts must all be 0 (any row means part of the OLDER block
# reached the store) and the NEWER persisted count must equal the NEWER
# accepted send.  Each failed count names itself in the reason.
# ---------------------------------------------------------------------------


def test_the_o4_premise_holds_with_no_older_row_and_a_full_newer_block():
    assert ordering.memory_block_premise_reason(0, 0, 0, 5, 5) is None


def test_the_o4_premise_fails_when_an_older_row_was_persisted_while_the_lock_held():
    reason = ordering.memory_block_premise_reason(1, 0, 0, 5, 5)
    assert reason is not None
    assert "while the store was locked" in reason


def test_the_o4_premise_fails_when_an_older_row_was_persisted_after_the_unlock():
    reason = ordering.memory_block_premise_reason(0, 1, 0, 5, 5)
    assert reason is not None
    assert "after the lock lifted" in reason


def test_the_o4_premise_fails_when_an_older_row_was_persisted_before_the_drain():
    reason = ordering.memory_block_premise_reason(0, 0, 1, 5, 5)
    assert reason is not None
    assert "before the drain" in reason


def test_the_o4_premise_fails_when_the_newer_block_is_not_fully_persisted():
    reason = ordering.memory_block_premise_reason(0, 0, 0, 4, 5)
    assert reason is not None
    assert "NEWER" in reason


# ---------------------------------------------------------------------------
# The o1 flow, offline: the rig plumbing is stubbed so the case runs
# end to end without Docker.  These pin the construction - the 100 s
# lock (retry deadline + interval + send margin), the strict premise,
# the three persisted counts in the record - and the after-drain rule:
# a NEWER row on disk once the drain has completed is a FAIL, not a
# NOT RUN.
# ---------------------------------------------------------------------------


class _FakeLock:
    def __init__(self) -> None:
        self.seconds: int = 0
        self.wait_timeout: int | None = None

    def poll(self) -> int:
        return 0

    def wait(self, timeout: int | None = None) -> int:
        self.wait_timeout = timeout
        return 0

    def kill(self) -> None:
        return None


def _stub_rig(monkeypatch, *, store_order: list[str], queue_rows):
    """Stub every rig touchpoint the ordering cases use.

    ``queue_rows(needle, drained)`` returns the companion store rows a
    store read sees for that needle at that stage of the case; the
    ``drained`` flag flips when the case starts MEDRE.  ``store_order``
    is the arrival order MEDRE's store holds.  Returns the fake lock and
    the state flag so a test can assert on them.
    """
    state = {"drained": False}

    def fake_start_medre(self):
        state["drained"] = True

    def fake_repeater_queue(self, needle):
        rows = queue_rows(needle, state["drained"])
        return [{"text": row} for row in rows], {}

    def fake_markers_from_store(self, needle, only=None):
        return [row for row in store_order if only is None or row in only]

    def fake_peer_send(self, count, prefix, settle=3.0):
        return {
            "accepted_count": count,
            "nonces": [f"{prefix}-{index}-{index:02x}" for index in range(count)],
        }

    lock = _FakeLock()

    def fake_hold_exclusive_lock(seconds):
        lock.seconds = seconds
        return lock

    monkeypatch.setattr(
        ordering.OrderingMatrix,
        "preconditions",
        lambda self, note: {
            "note": note,
            "medre_stopped": True,
            "companion_a_client_free": True,
        },
    )
    monkeypatch.setattr(
        ordering.OrderingMatrix,
        "wait_for",
        lambda self, predicate, timeout, interval=1.0: True,
    )
    monkeypatch.setattr(ordering.OrderingMatrix, "peer_send", fake_peer_send)
    monkeypatch.setattr(ordering.OrderingMatrix, "repeater_queue", fake_repeater_queue)
    monkeypatch.setattr(
        ordering.OrderingMatrix, "markers_from_store", fake_markers_from_store
    )
    monkeypatch.setattr(ordering.OrderingMatrix, "start_medre", fake_start_medre)
    monkeypatch.setattr(ordering.backlog, "container_running", lambda name: True)
    monkeypatch.setattr(ordering.backlog, "checked_docker", lambda *args, **kwargs: "")
    monkeypatch.setattr(ordering.backlog, "restart_repeater", lambda container: None)
    monkeypatch.setattr(
        ordering.backlog, "hold_exclusive_lock", fake_hold_exclusive_lock
    )
    return lock, state


def test_case_o1_locks_past_the_retry_budget_and_passes_with_clean_counts(
    tmp_path, monkeypatch
):
    store_order = [marker("o1a", i, f"{i:02x}") for i in range(5)] + [
        marker("o1b", i, f"{i:02x}") for i in range(5)
    ]

    def queue_rows(needle, drained):
        return []

    lock, _ = _stub_rig(monkeypatch, store_order=store_order, queue_rows=queue_rows)
    matrix = ordering.OrderingMatrix(tmp_path / "ev-o1", "openhop-bridge")
    matrix.case_o1()
    path = tmp_path / "ev-o1" / "o1-older-persisted-newer-memory.json"
    record = json.loads(path.read_text())
    assert record["pass"] is True
    assert record["failures"] == []
    assert record["newer_persisted_while_store_locked"] == 0
    assert record["newer_persisted_before_drain"] == 0
    assert record["newer_persisted_after_drain"] == 0
    assert record["block_verdict"]["distinct"] == 10
    # 100 s lock (60 s retry deadline + 1 s interval + send margin), and
    # the wait must be bounded past the lock, not past the old 30 s.
    assert lock.seconds == 100
    assert lock.wait_timeout == 130


def test_case_o1_fails_when_a_newer_row_lands_on_disk_during_the_drain(
    tmp_path, monkeypatch
):
    store_order = [marker("o1a", i, f"{i:02x}") for i in range(5)] + [
        marker("o1b", i, f"{i:02x}") for i in range(5)
    ]
    # A retried row is one of the sent markers; the fake peer sends the
    # unique tails "00".."04".
    late_row = marker("o1b", 4, "04")

    def queue_rows(needle, drained):
        return [late_row] if needle == "p4o1b" and drained else []

    lock, _ = _stub_rig(monkeypatch, store_order=store_order, queue_rows=queue_rows)
    matrix = ordering.OrderingMatrix(tmp_path / "ev-o1-late", "openhop-bridge")
    matrix.case_o1()
    path = tmp_path / "ev-o1-late" / "o1-older-persisted-newer-memory.json"
    record = json.loads(path.read_text())
    assert record["pass"] is False
    assert matrix.results[0]["status"] == "FAIL"
    assert record["newer_persisted_after_drain"] == 1
    assert any("during the drain" in failure for failure in record["failures"])


def test_case_o1_is_not_run_when_a_newer_row_was_persisted_before_the_drain(
    tmp_path, monkeypatch
):
    store_order = [marker("o1a", i, f"{i:02x}") for i in range(5)] + [
        marker("o1b", i, f"{i:02x}") for i in range(5)
    ]
    reads = {"p4o1b": 0}

    def queue_rows(needle, drained):
        # The two pre-drain reads: while the lock held nothing was on
        # disk; between the lift and the drain a retry landed.
        if needle == "p4o1b" and not drained:
            reads["p4o1b"] += 1
            return [] if reads["p4o1b"] == 1 else [store_order[-1]]
        return []

    lock, state = _stub_rig(monkeypatch, store_order=store_order, queue_rows=queue_rows)
    matrix = ordering.OrderingMatrix(tmp_path / "ev-o1-premise", "openhop-bridge")
    matrix.case_o1()
    path = tmp_path / "ev-o1-premise" / "o1-older-persisted-newer-memory.json"
    record = json.loads(path.read_text())
    assert record["status"] == "NOT RUN"
    assert record["pass"] is False
    assert record["newer_persisted_while_store_locked"] == 0
    assert record["newer_persisted_before_drain"] == 1
    assert "persist-retry budget" in record["reason"]
    # The drain still ran, so no rows are left behind.
    assert state["drained"] is True
    assert lock.seconds == 100


# ---------------------------------------------------------------------------
# The o4 flow, offline: the mirror of o1.  The OLDER block arrives under
# the 100 s lock (retry deadline + interval + send margin) and must stay
# in memory BY CONSTRUCTION - all three OLDER persisted counts 0 - while
# the NEWER block is fully persisted before the drain.  An OLDER row on
# disk once the drain has completed is a FAIL, and a broken premise is a
# NOT RUN only after the drain ran.  The NEWER batch must be sent only
# after the lock has lifted: draining or persisting under the lock would
# invalidate the case.
# ---------------------------------------------------------------------------


def _o4_queue(newer_rows_before_drain, *, older_after_drain=None):
    """A store view for case_o4: the NEWER rows are on disk before the
    drain, the OLDER block never is (unless ``older_after_drain`` plants
    one there for the after-drain read)."""
    newer_markers = list(newer_rows_before_drain)

    def queue_rows(needle, drained):
        if needle == "p4o4a" and drained:
            return list(older_after_drain or [])
        if needle == "p4o4b" and not drained:
            return newer_markers
        return []

    return queue_rows


def test_case_o4_locks_past_the_retry_budget_and_passes_with_clean_counts(
    tmp_path, monkeypatch
):
    store_order = [marker("o4a", i, f"{i:02x}") for i in range(5)] + [
        marker("o4b", i, f"{i:02x}") for i in range(5)
    ]

    def queue_rows(needle, drained):
        if needle == "p4o4b" and not drained:
            return [marker("o4b", i, f"{i:02x}") for i in range(5)]
        return []

    lock, _ = _stub_rig(monkeypatch, store_order=store_order, queue_rows=queue_rows)
    monkeypatch.setattr(ordering.time, "sleep", lambda seconds: None)
    matrix = ordering.OrderingMatrix(tmp_path / "ev-o4", "openhop-bridge")
    matrix.case_o4()
    path = tmp_path / "ev-o4" / "o4-older-memory-newer-persisted.json"
    record = json.loads(path.read_text())
    assert record["pass"] is True
    assert record["failures"] == []
    assert record["older_persisted_while_store_locked"] == 0
    assert record["older_persisted_after_unlock"] == 0
    assert record["older_persisted_before_drain"] == 0
    assert record["newer_persisted_before_drain"] == 5
    assert record["older_persisted_after_drain"] == 0
    assert record["block_verdict"]["distinct"] == 10
    assert record["remaining_persisted"] == []
    # 100 s lock (60 s retry deadline + 1 s interval + send margin), and
    # the wait must be bounded past the lock, not past the old 30 s.
    assert lock.seconds == 100
    assert lock.wait_timeout == 130
    assert (
        "the OLDER in-memory block must arrive entirely" in record["ordering_promise"]
    )


def test_case_o4_fails_when_an_older_row_is_on_disk_after_the_drain(
    tmp_path, monkeypatch
):
    store_order = [marker("o4a", i, f"{i:02x}") for i in range(5)] + [
        marker("o4b", i, f"{i:02x}") for i in range(5)
    ]
    # A persisted OLDER row after the drain means the OLDER block was
    # never memory-only; the fake peer sends the unique tails "00".."04".
    late_row = marker("o4a", 4, "04")

    lock, _ = _stub_rig(
        monkeypatch,
        store_order=store_order,
        queue_rows=_o4_queue(
            [marker("o4b", i, f"{i:02x}") for i in range(5)],
            older_after_drain=[late_row],
        ),
    )
    monkeypatch.setattr(ordering.time, "sleep", lambda seconds: None)
    matrix = ordering.OrderingMatrix(tmp_path / "ev-o4-late", "openhop-bridge")
    matrix.case_o4()
    path = tmp_path / "ev-o4-late" / "o4-older-memory-newer-persisted.json"
    record = json.loads(path.read_text())
    assert record["pass"] is False
    assert matrix.results[0]["status"] == "FAIL"
    assert record["older_persisted_after_drain"] == 1
    assert any("on disk after the drain" in failure for failure in record["failures"])


def test_case_o4_is_not_run_when_the_newer_block_is_not_fully_persisted(
    tmp_path, monkeypatch
):
    store_order = [marker("o4a", i, f"{i:02x}") for i in range(5)] + [
        marker("o4b", i, f"{i:02x}") for i in range(5)
    ]
    # One NEWER message never reached the store before the drain.
    short_newer = [marker("o4b", i, f"{i:02x}") for i in range(4)]

    lock, state = _stub_rig(
        monkeypatch, store_order=store_order, queue_rows=_o4_queue(short_newer)
    )
    monkeypatch.setattr(ordering.time, "sleep", lambda seconds: None)
    matrix = ordering.OrderingMatrix(tmp_path / "ev-o4-premise", "openhop-bridge")
    matrix.case_o4()
    path = tmp_path / "ev-o4-premise" / "o4-older-memory-newer-persisted.json"
    record = json.loads(path.read_text())
    assert record["status"] == "NOT RUN"
    assert record["pass"] is False
    assert record["older_persisted_while_store_locked"] == 0
    assert record["newer_persisted_before_drain"] == 4
    assert "NEWER" in record["reason"]
    # The drain still ran, so no rows are left behind.
    assert state["drained"] is True
    assert lock.seconds == 100


def test_case_o4_sends_the_newer_batch_only_after_the_lock_has_lifted(
    tmp_path, monkeypatch
):
    store_order = [marker("o4a", i, f"{i:02x}") for i in range(5)] + [
        marker("o4b", i, f"{i:02x}") for i in range(5)
    ]

    lock, _ = _stub_rig(
        monkeypatch,
        store_order=store_order,
        queue_rows=_o4_queue([marker("o4b", i, f"{i:02x}") for i in range(5)]),
    )
    order: list[str] = []

    def tracking_lock(seconds):
        order.append("lock")
        return lock

    original_peer = ordering.OrderingMatrix.peer_send

    def tracking_peer(self, count, prefix, settle=3.0):
        order.append(f"send:{prefix}")
        return original_peer(self, count, prefix, settle=settle)

    original_wait = lock.wait

    def tracking_wait(timeout=None):
        order.append("wait")
        return original_wait(timeout)

    monkeypatch.setattr(ordering.backlog, "hold_exclusive_lock", tracking_lock)
    monkeypatch.setattr(ordering.OrderingMatrix, "peer_send", tracking_peer)
    monkeypatch.setattr(lock, "wait", tracking_wait)
    monkeypatch.setattr(ordering.time, "sleep", lambda seconds: None)
    matrix = ordering.OrderingMatrix(tmp_path / "ev-o4-order", "openhop-bridge")
    matrix.case_o4()
    # The lock starts before the OLDER batch is sent, and the NEWER
    # batch is sent only once the wait for the lift has returned.
    assert order == ["lock", "send:p4o4a", "wait", "send:p4o4b"]


# ---------------------------------------------------------------------------
# The o2 flow, offline: the corrected claim holds whichever store the
# live message is in when the drain begins, so the case neither locks
# the store nor consults the merge premise - the pre-drain count is
# recorded as information only.
# ---------------------------------------------------------------------------


def test_case_o2_records_the_live_count_without_a_lock_or_a_premise(
    tmp_path, monkeypatch
):
    backlog_markers = [marker("o2a", i, f"{i:02x}") for i in range(5)]
    live_marker = marker("o2b", 0, "00")
    store_order = backlog_markers + [live_marker]

    def queue_rows(needle, drained):
        if needle == "p4o2a":
            return backlog_markers
        if needle == "p4o2b":
            # The live message sits in the store before the drain, as
            # the repeater persists it within about a second.
            return [live_marker]
        return []

    lock, _ = _stub_rig(monkeypatch, store_order=store_order, queue_rows=queue_rows)

    def forbidden_lock(seconds):
        raise AssertionError("case_o2 must not lock the repeater's store")

    def forbidden_premise(*args):
        raise AssertionError("case_o2 must not consult the merge premise")

    monkeypatch.setattr(ordering.backlog, "hold_exclusive_lock", forbidden_lock)
    monkeypatch.setattr(ordering, "merge_premise_reason", forbidden_premise)
    matrix = ordering.OrderingMatrix(tmp_path / "ev-o2", "openhop-bridge")
    matrix.case_o2()
    path = tmp_path / "ev-o2" / "o2-restart-backlog-then-live.json"
    record = json.loads(path.read_text())
    assert record["pass"] is True
    assert record["failures"] == []
    assert record["live_persisted_before_drain"] == 1
    assert "live_persisted_while_store_locked" not in record
    assert record["block_verdict"]["distinct"] == 6
    assert record["block_verdict"]["duplicate_arrivals"] == 0
    arrivals = record["block_verdict"]["arrival_sequence"]
    assert arrivals[-1]["block"] == "o2b"
    assert [row["block"] for row in arrivals[:5]] == ["o2a"] * 5
    assert (
        "whichever store it is in when the drain begins" in record["ordering_promise"]
    )
