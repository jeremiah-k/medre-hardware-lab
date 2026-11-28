#!/usr/bin/env python3
"""Companion ordering cases: arrival order across memory and SQLite.

WHAT THIS DRIVES
----------------
Four questions about the companion's arrival-order contract - deliver
oldest first across the in-memory and persisted queues - each run on the
REAL rig: a real repeater process, its real SQLite store, a real MEDRE
client:

EXIT CONTRACT
-------------
Exit status 0 only when EVERY case this invocation ran is PASS; any FAIL
or NOT RUN makes it 1 (--summary-only writes no case and exits 0).

  o1-older-persisted-newer-memory
      OLDER messages are persisted at the companion while NO client is
      attached.  NEWER messages then arrive while the store is locked
      for 100 s - longer than the repeater's whole persist-retry budget
      (a failed persist is retried every 1 s until 60 s after its first
      failure, then never again) - so every retry lands while the lock
      holds and the NEWER block stays in memory BY CONSTRUCTION.  The
      lock lifts and the client syncs: the sync must serve the lowest
      outstanding arrival number across BOTH stores, i.e. OLDER then
      NEWER.

  o2-restart-backlog-then-live
      A backlog is persisted, the REPEATER is restarted (the queue
      survives and the arrival counter re-seeds above the stored rows),
      and one NEW live message arrives with no client attached.  The
      client then syncs: the live message is delivered exactly once,
      after the whole backlog, in arrival order, whichever store it is
      in when the drain begins.

  o3-store-locked-memory-served
      The store is held under an EXCLUSIVE lock while new messages arrive
      with the client ATTACHED.  The store is WAL, so an exclusive lock
      blocks writes, not reads: the degrade the case asserts is that
      in-memory messages are served WHILE the lock holds, the unreadable-
      store fallback obeys its once-per-episode warning contract (zero
      warnings on a lock alone, never a warning per sync), and the bounded
      persist retry lands every accepted message when the lock lifts -
      nothing lost, nothing duplicated, nothing left behind.  The case
      counts what was served during the lock, arrivals, duplicates,
      order, and what remains persisted after it.

  o4-older-memory-newer-persisted
      The roles of o1's two blocks are swapped: the OLDER messages
      arrive under the 100 s store lock with NO client attached and
      stay in memory BY CONSTRUCTION (the lock outlasts the repeater's
      whole persist-retry budget - a failed persist is retried every
      1 s until 60 s after its first failure, then never again - so
      every retry lands while the lock holds); the lock lifts, the
      NEWER messages arrive and are persisted with no client attached.
      The client then syncs and must get the OLDER in-memory block
      entirely before the NEWER persisted block.

WHAT A PASS MEANS
-----------------
A pass says THIS bounded run delivered what was accepted with no duplicate
and the measured order - not "exactly once" and not a delivery guarantee.
The companion protocol has no application-level acknowledgement of durable
admission, and the RF medium is simulated by the lab relay.

Counting rules are the backlog matrix's: a message counts when it carries a
nonce marker (the FULL marker, unique tail included), every count is scoped
to the markers this run sent, and ordering is judged within a case.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import openhop_backlog_matrix as backlog  # noqa: E402
import openhop_case_summary as case_summary  # noqa: E402

LAB_ROOT = Path(__file__).resolve().parents[2]

REPEATER_A = backlog.REPEATER_A
MEDRE_BRIDGE = backlog.MEDRE_BRIDGE

# The warning the documented fallback logs when the persisted queue cannot
# be read (repeater/companion/frame_server.py, _sync_message_locked).
FALLBACK_WARNING = "persisted queue unreadable"

# Each five-message batch can queue failed 250 ms writes. A retry
# can wake one interval beyond its 60 s deadline and wait for those writes.
MEMORY_RETRY_COVERAGE_S = 65.0
MEMORY_SEND_BUDGET_S = 19.0
MEMORY_LOCK_SECONDS = int(MEMORY_RETRY_COVERAGE_S + MEMORY_SEND_BUDGET_S)


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


# ---------------------------------------------------------------------------
# Pure helpers (offline-testable)
# ---------------------------------------------------------------------------
def marker_block(marker: str) -> str | None:
    """Return the block tag of a marker (``p4o1a-0-<hex>`` -> ``o1a``)."""
    return backlog.nonce_case(marker)


def marker_index(marker: str) -> int | None:
    return backlog.nonce_index(marker)


def merged_block_verdict(markers: list[str], blocks: list[str]) -> dict:
    """Judge an arrival sequence against an expected BLOCK order.

    ``markers`` is the arrival-ordered sequence of full nonce markers.
    ``blocks`` is the expected order of the block tags (for example
    ``["o1a", "o1b"]``).  The verdict checks:

    * duplicates: a marker arriving twice fails;
    * cross-block order: every marker of an earlier block must arrive
      before every marker of the next block;
    * within-block order: the indices of one block must arrive strictly
      increasing;
    * the arrival sequence is returned so the order is a fact in the
      evidence rather than a summary.
    """
    duplicates = len(markers) - len(set(markers))
    positions: dict[str, list[int]] = {}
    arrivals: list[dict] = []
    for position, marker in enumerate(markers):
        block = marker_block(marker) or ""
        index = marker_index(marker)
        arrivals.append({"block": block, "index": index, "marker": marker})
        positions.setdefault(block, []).append(position)
    cross_violations = 0
    for earlier, later in zip(blocks, blocks[1:]):
        earlier_positions = positions.get(earlier) or []
        later_positions = positions.get(later) or []
        if not earlier_positions or not later_positions:
            continue
        if max(earlier_positions) > min(later_positions):
            cross_violations += 1
    within_violations: list[str] = []
    highest: dict[str, int] = {}
    for arrival in arrivals:
        block = arrival["block"]
        index = arrival["index"]
        if index is None:
            continue
        previous = highest.get(block)
        if previous is not None and index < previous:
            within_violations.append(
                f"{block}: index {index} arrived after index {previous}"
            )
        highest[block] = max(previous if previous is not None else -1, index)
    return {
        "arrivals": len(markers),
        "distinct": len(set(markers)),
        "duplicate_arrivals": duplicates,
        "cross_block_violations": cross_violations,
        "within_block_violations": within_violations,
        "arrival_sequence": arrivals,
    }


def merge_premise_reason(
    persisted_while_locked: int, persisted_before_drain: int
) -> str | None:
    """Return why the cross-store merge premise failed, or None.

    The merge claim (the sync serves the lowest outstanding arrival
    number across the in-memory AND the persisted queue) is only provable
    when the newer block is IN MEMORY ONLY when the client drains.  The
    store lock is held longer than the repeater's whole persist-retry
    budget, so the premise is strict: a row persisted WHILE THE LOCK WAS
    HELD means the lock did not hold, and a row persisted AFTER THE LIFT
    but BEFORE THE DRAIN means a retry outlived the budget - either way
    part of the newer block is on disk and FIFO over the persisted store
    alone would produce the asserted order.  Both counts must be 0.  The
    case is NOT RUN - but only AFTER the drain, so no rows are left
    behind.
    """
    if persisted_while_locked > 0:
        return (
            f"{persisted_while_locked} row(s) were persisted while the store "
            "was locked: the lock did not hold, so the newer block was not "
            "in memory only and the merge premise cannot be verified"
        )
    if persisted_before_drain > 0:
        return (
            f"{persisted_before_drain} row(s) were persisted after the lock "
            "lifted but before the drain: the lock did not outlast the "
            "persist-retry budget, so the newer block was not in memory "
            "only and the merge premise cannot be verified"
        )
    return None


def memory_block_premise_reason(
    older_persisted_while_store_locked: int,
    older_persisted_after_unlock: int,
    older_persisted_before_drain: int,
    newer_persisted_before_drain: int,
    newer_accepted: int | None,
) -> str | None:
    """Return why the o4 premise failed, or None.

    The o4 claim (the OLDER in-memory block arrives entirely before the
    NEWER persisted block) is only provable when the OLDER block is IN
    MEMORY ONLY and the NEWER block is FULLY PERSISTED when the client
    drains.  The OLDER block arrives under a store lock held longer than
    the repeater's whole persist-retry budget, so the premise is strict:
    an OLDER row persisted WHILE THE LOCK WAS HELD means the lock did not
    hold during the arrivals, and an OLDER row persisted AFTER THE LIFT
    means a retry outlived the budget - either way part of the OLDER
    block is on disk.  All three OLDER persisted counts must be 0, and
    the NEWER persisted count must equal the NEWER accepted send (which
    must be positive).  The case is NOT RUN - but only AFTER the drain,
    so no rows are left behind.
    """
    if older_persisted_while_store_locked > 0:
        return (
            f"{older_persisted_while_store_locked} OLDER row(s) were "
            "persisted while the store was locked: the lock did not hold "
            "during the OLDER batch's arrivals, so the OLDER block was "
            "not in memory only and the premise cannot be verified"
        )
    if older_persisted_after_unlock > 0:
        return (
            f"{older_persisted_after_unlock} OLDER row(s) were persisted "
            "after the lock lifted: a persist retry outlived the budget, "
            "so the OLDER block was not in memory only and the premise "
            "cannot be verified"
        )
    if older_persisted_before_drain > 0:
        return (
            f"{older_persisted_before_drain} OLDER row(s) were persisted "
            "before the drain: the OLDER block was not in memory only and "
            "the premise cannot be verified"
        )
    if not newer_accepted or newer_persisted_before_drain != newer_accepted:
        return (
            f"{newer_persisted_before_drain} of {newer_accepted} accepted "
            "NEWER message(s) were persisted before the drain: the NEWER "
            "block was not fully on disk, so the cross-store order cannot "
            "be verified"
        )
    return None


# ---------------------------------------------------------------------------
# Rig plumbing (the backlog matrix's helpers, reused where possible)
# ---------------------------------------------------------------------------
class OrderingMatrix:
    def __init__(self, evidence_dir: Path, profile: str) -> None:
        self.dir = evidence_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.profile = profile
        self.results: list[dict] = []

    def write(self, name: str, payload: dict) -> None:
        payload.setdefault("label", "protocol-simulated")
        payload.setdefault("generated_at", utcnow())
        path = self.dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        self.results.append(
            {
                "case": name,
                "status": "PASS" if payload.get("pass") else "FAIL",
                "evidence": str(path),
            }
        )

    def not_run(self, name: str, reason: str, extra: dict | None = None) -> None:
        payload = {"case": name, "pass": False, "status": "NOT RUN", "reason": reason}
        payload.update(extra or {})
        self.write(name, payload)
        self.results[-1]["status"] = "NOT RUN"

    # small wrappers over the backlog matrix plumbing ---------------------
    def preconditions(self, note: str) -> dict:
        return backlog.Matrix(self.dir, self.profile).preconditions(note)

    def wait_for(self, predicate, timeout: float, interval: float = 1.0) -> bool:
        return backlog.Matrix(self.dir, self.profile).wait_for(
            predicate, timeout, interval
        )

    def peer_send(self, count: int, prefix: str, settle: float = 3.0) -> dict:
        return backlog.peer_send(self.profile, count, prefix, settle=settle)

    def medre_events(self, needle: str, only: set[str] | None = None) -> list[dict]:
        return backlog.Matrix(self.dir, self.profile).medre_events(needle, only)

    def repeater_queue(self, needle: str) -> tuple[list[dict], dict]:
        return backlog.Matrix(self.dir, self.profile).repeater_queue(REPEATER_A, needle)

    def repeater_log(self) -> str:
        return backlog.container_logs(REPEATER_A)

    def markers_from_store(self, needle: str, mine: set[str]) -> list[str]:
        """The run's markers at MEDRE, in MEDRE's store order (arrival order).

        ONE store query filtered by ONE needle: the two sub-batches of a
        case (``p4o1a``/``p4o1b``) share the ``p4o1`` marker family, so a
        single query returns the interleaved arrival order.  Two separate
        per-prefix queries would lose that order.  The backlog cases
        already use MEDRE's store order as the arrival order (c3's
        ``arrival_order_indices``).  Only this run's markers are returned.
        """
        events = self.medre_events(needle, only=mine)
        return [
            marker
            for marker in (
                backlog.nonce_marker(json.dumps(event, sort_keys=True))
                for event in events
            )
            if marker is not None and marker in mine
        ]

    def persisted_markers(self, mine: set[str]) -> tuple[int, list[str]]:
        rows, state = self.repeater_queue("")
        count, markers = backlog.count_nonce_rows(rows, only=mine)
        return count, markers

    def stop_medre(self) -> None:
        backlog.checked_docker("stop", MEDRE_BRIDGE)

    def start_medre(self) -> None:
        backlog.checked_docker("start", MEDRE_BRIDGE)
        self.wait_for(lambda: backlog.container_running(MEDRE_BRIDGE), 90)

    # -- cases -----------------------------------------------------------
    def case_o1(self, older: int = 5, newer: int = 5) -> None:
        """OLDER persisted block first, NEWER memory block second.

        The NEWER block is held in memory BY CONSTRUCTION: the store lock
        (100 s) outlasts the repeater's whole persist-retry budget (a
        failed persist is retried every 1 s until 60 s after its first
        failure, then never again), so every retry of the NEWER block
        lands while the lock still holds and nothing persists the block
        afterwards.  The premise is strict - both pre-drain persisted
        counts must be 0 - and a NEWER row on disk after the drain is a
        FAIL: a retry would have landed during the drain itself.
        """
        name = "o1-older-persisted-newer-memory"
        old_prefix, new_prefix = "p4o1a", "p4o1b"
        restore_bridge = False
        lock_proc = None
        try:
            if not backlog.container_running(MEDRE_BRIDGE):
                self.not_run(name, "the MEDRE bridge container is not running")
                return
            restore_bridge = True
            self.stop_medre()
            pre = self.preconditions(
                "MEDRE stopped; the OLDER batch is persisted with no client "
                "attached before the NEWER batch arrives"
            )
            if not pre["medre_stopped"] or not pre["companion_a_client_free"]:
                self.not_run(
                    name,
                    "MEDRE did not stop cleanly, so no persisted backlog could form",
                    {"preconditions": pre},
                )
                return
            old_send = self.peer_send(older, old_prefix, settle=5.0)
            old_markers_sent = backlog.sent_markers(old_send)
            if not self.wait_for(
                lambda: self.persisted_markers(old_markers_sent)[0] >= older, 60
            ):
                self.not_run(
                    name,
                    "the OLDER batch never became fully persisted at the companion",
                    {"preconditions": pre},
                )
                return
            # Lock the store so the NEWER batch cannot be persisted and
            # stays in the in-memory queue.  The duration is derived from
            # the repeater's persist-retry budget
            # (repeater/companion/frame_server.py): a failed persist is
            # retried every _PERSIST_RETRY_INTERVAL_S (1 s) until
            # _PERSIST_RETRY_DEADLINE_S (60 s) after its first failure,
            # and after that deadline nothing persists the message again.
            # Verify the send left enough lock time for every retry.
            lock_started = time.monotonic()
            lock_proc = backlog.hold_exclusive_lock(MEMORY_LOCK_SECONDS)
            if lock_proc is None:
                self.not_run(
                    name,
                    "the repeater's SQLite store could not be locked",
                    {"preconditions": pre},
                )
                return
            new_send = self.peer_send(newer, new_prefix, settle=3.0)
            new_markers_sent = backlog.sent_markers(new_send)
            send_elapsed_s = time.monotonic() - lock_started
            rows_while_locked, _ = self.repeater_queue(new_prefix)
            persisted_while_locked, _ = backlog.count_nonce_rows(
                rows_while_locked, only=new_markers_sent
            )
            try:
                lock_proc.wait(timeout=MEMORY_LOCK_SECONDS + 5)
            except subprocess.TimeoutExpired:
                lock_proc.kill()
                lock_proc.wait()
                raise
            lock_proc = None
            lock_hold_seconds = round(time.monotonic() - lock_started, 3)
            if send_elapsed_s > MEMORY_SEND_BUDGET_S:
                self.not_run(
                    name,
                    "the send left insufficient lock time to exhaust persist retries",
                )
                return
            # The strict merge premise, read BEFORE the drain: with the
            # lock outlasting the retry budget, BOTH counts must be 0 -
            # a row here means some of the NEWER block reached the disk.
            rows_before_drain, _ = self.repeater_queue(new_prefix)
            newer_persisted_before_drain, _ = backlog.count_nonce_rows(
                rows_before_drain, only=new_markers_sent
            )
            premise_failure = merge_premise_reason(
                persisted_while_locked, newer_persisted_before_drain
            )
            mine = old_markers_sent | new_markers_sent
            drain_started = time.monotonic()
            self.start_medre()
            self.wait_for(
                lambda: len(set(self.markers_from_store("p4o1", mine))) >= older + newer,
                timeout=180,
                interval=3.0,
            )
            drain_seconds = round(time.monotonic() - drain_started, 3)
            markers = self.markers_from_store("p4o1", mine)
            verdict = merged_block_verdict(markers, ["o1a", "o1b"])
            remaining_rows, _ = self.repeater_queue("")
            remaining_count, remaining_markers = backlog.count_nonce_rows(
                remaining_rows, only=mine
            )
            # Read AFTER the drain has completed, BEFORE the record is
            # written: the NEWER block was in memory before the drain and
            # was delivered, so a row on disk here means a persist retry
            # landed during the drain.
            rows_after_drain, _ = self.repeater_queue(new_prefix)
            newer_persisted_after_drain, _ = backlog.count_nonce_rows(
                rows_after_drain, only=new_markers_sent
            )
            failures: list[str] = []
            if not old_send.get("accepted_count") or not new_send.get("accepted_count"):
                failures.append("the peer did not accept both batches")
            if verdict["distinct"] != older + newer:
                failures.append(
                    f"MEDRE recorded {verdict['distinct']} distinct markers, "
                    f"expected {older + newer}"
                )
            if verdict["duplicate_arrivals"]:
                failures.append(f"{verdict['duplicate_arrivals']} duplicate arrivals")
            if verdict["cross_block_violations"]:
                failures.append(
                    f"{verdict['cross_block_violations']} NEWER message(s) overtook "
                    "an OLDER persisted message"
                )
            if verdict["within_block_violations"]:
                failures.append(
                    f"within-block order broken: {verdict['within_block_violations']}"
                )
            if remaining_count:
                failures.append(
                    f"{remaining_count} marker(s) remained persisted after the drain"
                )
            if newer_persisted_after_drain:
                failures.append(
                    f"{newer_persisted_after_drain} NEWER row(s) were persisted "
                    "during the drain: a persist retry landed after the lock "
                    "lifted, so the NEWER block was not in memory only"
                )
            if premise_failure:
                # The premise failed AFTER the drain ran, so no rows are
                # left behind; the case is NOT RUN with the pre-drain
                # counts in the record.
                self.not_run(
                    name,
                    premise_failure,
                    {
                        "newer_persisted_while_store_locked": persisted_while_locked,
                        "newer_persisted_before_drain": newer_persisted_before_drain,
                        "newer_persisted_after_drain": newer_persisted_after_drain,
                        "preconditions": pre,
                    },
                )
                return
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "preconditions": pre,
                    "older_requested": older,
                    "older_accepted": old_send.get("accepted_count"),
                    "newer_requested": newer,
                    "newer_accepted": new_send.get("accepted_count"),
                    "newer_persisted_while_store_locked": persisted_while_locked,
                    "newer_persisted_before_drain": newer_persisted_before_drain,
                    "newer_persisted_after_drain": newer_persisted_after_drain,
                    "lock_hold_seconds": lock_hold_seconds,
                    "locked_send_seconds": send_elapsed_s,
                    "drain_seconds": drain_seconds,
                    "block_verdict": verdict,
                    "remaining_persisted": remaining_markers,
                    "ordering_promise": (
                        "sync serves the lowest outstanding arrival number "
                        "across the in-memory and the SQLite queue; the NEWER "
                        "block is held in memory by construction (the store "
                        "lock outlasts the whole persist-retry budget), so "
                        "the OLDER persisted block must arrive entirely "
                        "before the NEWER in-memory block"
                    ),
                    "older_send": old_send,
                    "newer_send": new_send,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")
        finally:
            if lock_proc is not None:
                try:
                    backlog.release_lock(lock_proc, MEMORY_LOCK_SECONDS + 5)
                except Exception as exc:
                    self.not_run(name, f"could not release store lock: {exc}")
            if restore_bridge and not backlog.container_running(MEDRE_BRIDGE):
                try:
                    self.start_medre()
                except (RuntimeError, subprocess.SubprocessError) as exc:
                    self.not_run(name, f"could not restore the MEDRE bridge: {exc}")

    def case_o4(self, older: int = 5, newer: int = 5) -> None:
        """OLDER in-memory block first, NEWER persisted block second.

        The mirror of o1: the OLDER block is held in memory BY
        CONSTRUCTION - it arrives while the store is locked for 100 s,
        past the repeater's whole persist-retry budget (a failed persist
        is retried every 1 s until 60 s after its first failure, then
        never again), so every retry lands while the lock holds and
        nothing persists the block afterwards.  The NEWER block then
        arrives with the store writable and is fully persisted before
        the drain.  The premise is strict - all three OLDER persisted
        counts must be 0 and the NEWER persisted count must equal the
        accepted send - and an OLDER row on disk after the drain is a
        FAIL: the OLDER block would not have been memory-only, so the
        case would have proved single-store order, not the merge.
        """
        name = "o4-older-memory-newer-persisted"
        old_prefix, new_prefix = "p4o4a", "p4o4b"
        restore_bridge = False
        lock_proc = None
        try:
            if not backlog.container_running(MEDRE_BRIDGE):
                self.not_run(name, "the MEDRE bridge container is not running")
                return
            restore_bridge = True
            self.stop_medre()
            pre = self.preconditions(
                "MEDRE stopped; the OLDER batch arrives under the store "
                "lock with no client attached and stays in memory; the "
                "NEWER batch is persisted with no client attached before "
                "the drain"
            )
            if not pre["medre_stopped"] or not pre["companion_a_client_free"]:
                self.not_run(
                    name,
                    "MEDRE did not stop cleanly, so no in-memory block could form",
                    {"preconditions": pre},
                )
                return
            # Lock the store so the OLDER batch cannot be persisted and
            # stays in the in-memory queue.  The duration is derived from
            # the repeater's persist-retry budget
            # (repeater/companion/frame_server.py): a failed persist is
            # retried every _PERSIST_RETRY_INTERVAL_S (1 s) until
            # _PERSIST_RETRY_DEADLINE_S (60 s) after its first failure,
            # and after that deadline nothing persists the message again.
            # Verify the send left enough lock time for every retry.
            lock_started = time.monotonic()
            lock_proc = backlog.hold_exclusive_lock(MEMORY_LOCK_SECONDS)
            if lock_proc is None:
                self.not_run(
                    name,
                    "the repeater's SQLite store could not be locked",
                    {"preconditions": pre},
                )
                return
            old_send = self.peer_send(older, old_prefix, settle=5.0)
            old_markers_sent = backlog.sent_markers(old_send)
            send_elapsed_s = time.monotonic() - lock_started
            rows_while_locked, _ = self.repeater_queue(old_prefix)
            older_persisted_while_store_locked, _ = backlog.count_nonce_rows(
                rows_while_locked, only=old_markers_sent
            )
            try:
                lock_proc.wait(timeout=MEMORY_LOCK_SECONDS + 5)
            except subprocess.TimeoutExpired:
                lock_proc.kill()
                lock_proc.wait()
                raise
            lock_proc = None
            lock_hold_seconds = round(time.monotonic() - lock_started, 3)
            if send_elapsed_s > MEMORY_SEND_BUDGET_S:
                self.not_run(
                    name,
                    "the send left insufficient lock time to exhaust persist retries",
                )
                return
            # A retry that somehow outlived the budget would land and be
            # counted within these seconds: after the lift, nothing may
            # persist any part of the OLDER block.
            time.sleep(3)
            rows_after_unlock, _ = self.repeater_queue(old_prefix)
            older_persisted_after_unlock, _ = backlog.count_nonce_rows(
                rows_after_unlock, only=old_markers_sent
            )
            # No client is attached and the store is writable, so the
            # NEWER block is normally on disk within about a second of
            # its arrival; both counts are read from the store BEFORE
            # the drain.
            new_send = self.peer_send(newer, new_prefix, settle=5.0)
            new_markers_sent = backlog.sent_markers(new_send)
            rows_older_before_drain, _ = self.repeater_queue(old_prefix)
            older_persisted_before_drain, _ = backlog.count_nonce_rows(
                rows_older_before_drain, only=old_markers_sent
            )
            rows_newer_before_drain, _ = self.repeater_queue(new_prefix)
            newer_persisted_before_drain, _ = backlog.count_nonce_rows(
                rows_newer_before_drain, only=new_markers_sent
            )
            premise_failure = memory_block_premise_reason(
                older_persisted_while_store_locked,
                older_persisted_after_unlock,
                older_persisted_before_drain,
                newer_persisted_before_drain,
                new_send.get("accepted_count"),
            )
            mine = old_markers_sent | new_markers_sent
            drain_started = time.monotonic()
            self.start_medre()
            self.wait_for(
                lambda: len(set(self.markers_from_store("p4o4", mine))) >= older + newer,
                timeout=180,
                interval=3.0,
            )
            drain_seconds = round(time.monotonic() - drain_started, 3)
            markers = self.markers_from_store("p4o4", mine)
            verdict = merged_block_verdict(markers, ["o4a", "o4b"])
            remaining_rows, _ = self.repeater_queue("")
            remaining_count, remaining_markers = backlog.count_nonce_rows(
                remaining_rows, only=mine
            )
            # Read AFTER the drain has completed, BEFORE the record is
            # written: the OLDER block was in memory before the drain and
            # was delivered, so a row on disk here means part of the
            # OLDER block reached the store at some point - single-store
            # order, not the merge the case proves.
            rows_after_drain, _ = self.repeater_queue(old_prefix)
            older_persisted_after_drain, _ = backlog.count_nonce_rows(
                rows_after_drain, only=old_markers_sent
            )
            failures: list[str] = []
            if not old_send.get("accepted_count") or not new_send.get("accepted_count"):
                failures.append("the peer did not accept both batches")
            if verdict["distinct"] != older + newer:
                failures.append(
                    f"MEDRE recorded {verdict['distinct']} distinct markers, "
                    f"expected {older + newer}"
                )
            if verdict["duplicate_arrivals"]:
                failures.append(f"{verdict['duplicate_arrivals']} duplicate arrivals")
            if verdict["cross_block_violations"]:
                failures.append(
                    f"{verdict['cross_block_violations']} NEWER persisted message(s) "
                    "overtook an OLDER in-memory message"
                )
            if verdict["within_block_violations"]:
                failures.append(
                    f"within-block order broken: {verdict['within_block_violations']}"
                )
            if remaining_count:
                failures.append(
                    f"{remaining_count} marker(s) remained persisted after the drain"
                )
            if older_persisted_after_drain:
                failures.append(
                    f"{older_persisted_after_drain} OLDER row(s) were on disk "
                    "after the drain: the OLDER block was not in memory only, "
                    "so the case proved single-store order, not the merge"
                )
            if premise_failure:
                # The premise failed AFTER the drain ran, so no rows are
                # left behind; the case is NOT RUN with the counts in the
                # record.
                self.not_run(
                    name,
                    premise_failure,
                    {
                        "older_persisted_while_store_locked": (
                            older_persisted_while_store_locked
                        ),
                        "older_persisted_after_unlock": older_persisted_after_unlock,
                        "older_persisted_before_drain": older_persisted_before_drain,
                        "newer_persisted_before_drain": newer_persisted_before_drain,
                        "preconditions": pre,
                    },
                )
                return
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "preconditions": pre,
                    "older_requested": older,
                    "older_accepted": old_send.get("accepted_count"),
                    "newer_requested": newer,
                    "newer_accepted": new_send.get("accepted_count"),
                    "older_persisted_while_store_locked": (
                        older_persisted_while_store_locked
                    ),
                    "older_persisted_after_unlock": older_persisted_after_unlock,
                    "older_persisted_before_drain": older_persisted_before_drain,
                    "newer_persisted_before_drain": newer_persisted_before_drain,
                    "older_persisted_after_drain": older_persisted_after_drain,
                    "lock_hold_seconds": lock_hold_seconds,
                    "locked_send_seconds": send_elapsed_s,
                    "drain_seconds": drain_seconds,
                    "block_verdict": verdict,
                    "remaining_persisted": remaining_markers,
                    "ordering_promise": (
                        "sync serves the lowest outstanding arrival number "
                        "across the in-memory and the SQLite queue; the OLDER "
                        "block is held in memory by construction (the store "
                        "lock outlasts the whole persist-retry budget), so "
                        "the OLDER in-memory block must arrive entirely "
                        "before the NEWER persisted block"
                    ),
                    "older_send": old_send,
                    "newer_send": new_send,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")
        finally:
            if lock_proc is not None:
                try:
                    backlog.release_lock(lock_proc, MEMORY_LOCK_SECONDS + 5)
                except Exception as exc:
                    self.not_run(name, f"could not release store lock: {exc}")
            if restore_bridge and not backlog.container_running(MEDRE_BRIDGE):
                try:
                    self.start_medre()
                except (RuntimeError, subprocess.SubprocessError) as exc:
                    self.not_run(name, f"could not restore the MEDRE bridge: {exc}")

    def case_o2(self, backlog_count: int = 5) -> None:
        """Backlog first, then one live message - whichever store it is in.

        After a repeater restart with a persisted backlog, a live message
        that arrives afterwards is delivered exactly once, after the
        whole backlog, in arrival order, WHICHEVER STORE it is in when
        the drain begins.  No store lock and no premise: the repeater
        persists a live message within about a second of its arrival, so
        where the row sat is recorded (``live_persisted_before_drain``,
        expected 1) and the claim is stated to hold either way.
        """
        name = "o2-restart-backlog-then-live"
        back_prefix, live_prefix = "p4o2a", "p4o2b"
        restore_bridge = False
        try:
            if not backlog.container_running(MEDRE_BRIDGE):
                self.not_run(name, "the MEDRE bridge container is not running")
                return
            restore_bridge = True
            self.stop_medre()
            pre = self.preconditions(
                "MEDRE stopped; the backlog is persisted, then the repeater "
                "restarts before the live message arrives"
            )
            if not pre["medre_stopped"] or not pre["companion_a_client_free"]:
                self.not_run(
                    name,
                    "MEDRE did not stop cleanly, so no persisted backlog could form",
                    {"preconditions": pre},
                )
                return
            back_send = self.peer_send(backlog_count, back_prefix, settle=5.0)
            back_markers_sent = backlog.sent_markers(back_send)
            if not self.wait_for(
                lambda: self.persisted_markers(back_markers_sent)[0] >= backlog_count,
                60,
            ):
                self.not_run(
                    name,
                    "the backlog never became fully persisted at the companion",
                    {"preconditions": pre},
                )
                return
            backlog.restart_repeater(REPEATER_A)
            time.sleep(3)
            rows_after_restart, _ = self.repeater_queue(back_prefix)
            persisted_after_restart, _ = backlog.count_nonce_rows(
                rows_after_restart, only=back_markers_sent
            )
            live_send = self.peer_send(1, live_prefix, settle=3.0)
            live_markers_sent = backlog.sent_markers(live_send)
            # Information only (expected 1): the repeater persists the
            # live message within about a second of the send, so the row
            # is normally on disk before the drain.  The claim holds
            # whichever store the message is in when the drain begins.
            rows_live_before_drain, _ = self.repeater_queue(live_prefix)
            live_persisted_before_drain, _ = backlog.count_nonce_rows(
                rows_live_before_drain, only=live_markers_sent
            )
            mine = back_markers_sent | live_markers_sent
            drain_started = time.monotonic()
            self.start_medre()
            self.wait_for(
                lambda: len(set(self.markers_from_store("p4o2", mine))) >= backlog_count + 1,
                timeout=180,
                interval=3.0,
            )
            drain_seconds = round(time.monotonic() - drain_started, 3)
            markers = self.markers_from_store("p4o2", mine)
            verdict = merged_block_verdict(markers, ["o2a", "o2b"])
            remaining_rows, _ = self.repeater_queue("")
            remaining_count, remaining_markers = backlog.count_nonce_rows(
                remaining_rows, only=mine
            )
            failures: list[str] = []
            if not back_send.get("accepted_count") or not live_send.get(
                "accepted_count"
            ):
                failures.append(
                    "the peer did not accept the backlog and the live message"
                )
            if persisted_after_restart != len(back_markers_sent):
                failures.append(
                    f"the persisted backlog changed across the restart: "
                    f"{persisted_after_restart} of {len(back_markers_sent)} "
                    "survived"
                )
            if verdict["distinct"] != backlog_count + 1:
                failures.append(
                    f"MEDRE recorded {verdict['distinct']} distinct markers, "
                    f"expected {backlog_count + 1}"
                )
            if verdict["duplicate_arrivals"]:
                failures.append(f"{verdict['duplicate_arrivals']} duplicate arrivals")
            if verdict["cross_block_violations"]:
                failures.append("the live message overtook the persisted backlog")
            if verdict["within_block_violations"]:
                failures.append(
                    f"within-block order broken: {verdict['within_block_violations']}"
                )
            if remaining_count:
                failures.append(
                    f"{remaining_count} marker(s) remained persisted after the drain"
                )
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "preconditions": pre,
                    "backlog_requested": backlog_count,
                    "backlog_accepted": back_send.get("accepted_count"),
                    "backlog_persisted_before_restart": len(back_markers_sent),
                    "backlog_persisted_after_restart": persisted_after_restart,
                    "live_accepted": live_send.get("accepted_count"),
                    "live_persisted_before_drain": live_persisted_before_drain,
                    "drain_seconds": drain_seconds,
                    "block_verdict": verdict,
                    "remaining_persisted": remaining_markers,
                    "ordering_promise": (
                        "after a repeater restart with a persisted backlog, a "
                        "live message that arrives afterwards is delivered "
                        "exactly once, after the whole backlog, in arrival "
                        "order, whichever store it is in when the drain begins"
                    ),
                    "backlog_send": back_send,
                    "live_send": live_send,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")
        finally:
            if restore_bridge and not backlog.container_running(MEDRE_BRIDGE):
                try:
                    self.start_medre()
                except (RuntimeError, subprocess.SubprocessError) as exc:
                    self.not_run(name, f"could not restore the MEDRE bridge: {exc}")

    def case_o3(self, count: int = 8, lock_seconds: int = 40) -> None:
        name = "o3-store-locked-memory-served"
        prefix = "p4o3"
        lock_proc = None
        try:
            if not backlog.container_running(MEDRE_BRIDGE):
                self.not_run(name, "the MEDRE bridge container is not running")
                return
            pre = self.preconditions(
                "MEDRE stays attached to companion A while the store is locked"
            )
            warnings_before = self.repeater_log().count(FALLBACK_WARNING)
            lock_started = time.monotonic()
            lock_proc = backlog.hold_exclusive_lock(lock_seconds, release_on_close=True)
            if lock_proc is None:
                self.not_run(name, "the repeater's SQLite store could not be locked")
                return
            send = self.peer_send(count, prefix, settle=3.0)
            mine = backlog.sent_markers(send)
            # Serve what can be served WHILE the store is still locked.
            during_lock = self.markers_from_store(prefix, mine)
            served_while_locked = len(during_lock) if lock_proc.poll() is None else 0
            rows_while_locked, _ = self.repeater_queue(prefix)
            persisted_while_locked, _ = backlog.count_nonce_rows(
                rows_while_locked, only=mine
            )
            if lock_proc.poll() is not None:
                served_while_locked = 0
            assert lock_proc.stdin is not None
            lock_proc.stdin.close()
            try:
                lock_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                lock_proc.kill()
                lock_proc.wait()
                raise
            lock_proc = None
            lock_hold_seconds = time.monotonic() - lock_started
            # After the lock lifts: nothing new may appear and nothing may
            # duplicate; everything accepted must have been delivered or
            # must still be preserved at the companion.
            time.sleep(5)
            markers = self.markers_from_store(prefix, mine)
            report = backlog.ordering_report(markers)
            rows_after, _ = self.repeater_queue(prefix)
            persisted_after, _ = backlog.count_nonce_rows(rows_after, only=mine)
            warnings_after = self.repeater_log().count(FALLBACK_WARNING)
            warning_lines = [
                line.strip()
                for line in self.repeater_log().splitlines()
                if FALLBACK_WARNING in line
            ][-3:]
            warning_delta = warnings_after - warnings_before
            failures: list[str] = []
            if not send.get("accepted_count"):
                failures.append(
                    "the peer sent no accepted message while the store was locked"
                )
            if served_while_locked == 0:
                failures.append(
                    "nothing was served from memory while the store was locked"
                )
            if warning_delta:
                failures.append(
                    f"the readable WAL store triggered {warning_delta} "
                    "unreadable-store warnings"
                )
            if report["duplicate_arrivals"]:
                failures.append(
                    f"{report['duplicate_arrivals']} duplicate arrivals across the lock"
                )
            accepted = int(send.get("accepted_count") or 0)
            if report["distinct"] != accepted:
                failures.append(
                    f"MEDRE recorded {report['distinct']} of {accepted} accepted "
                    "messages after the lock lifted"
                )
            delivered_or_preserved = report["distinct"] + persisted_after
            if delivered_or_preserved != accepted:
                failures.append(
                    f"{report['distinct']} delivered + {persisted_after} preserved != "
                    f"{accepted} accepted messages"
                )
            if report["out_of_order_arrivals"]:
                failures.append(
                    f"{report['out_of_order_arrivals']} arrivals were out of order"
                )
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "preconditions": pre,
                    "requested_messages": count,
                    "peer_accepted_sends": send.get("accepted_count"),
                    "served_while_store_locked": served_while_locked,
                    "persisted_while_store_locked": persisted_while_locked,
                    "lock_hold_seconds": lock_hold_seconds,
                    "total_delivered_observed_after_unlock": report["arrivals"],
                    "distinct_at_medre": report["distinct"],
                    "duplicate_arrivals": report["duplicate_arrivals"],
                    "arrival_order_indices": [
                        backlog.nonce_index(marker)
                        for marker in markers
                        if backlog.nonce_index(marker) is not None
                    ],
                    "remained_persisted_after_unlock": persisted_after,
                    "fallback_warning_delta": warning_delta,
                    "fallback_warning_lines": warning_lines,
                    "fallback_promise": (
                        "the store is WAL, so an exclusive lock blocks writes "
                        "but not reads: messages accepted during the lock are "
                        "served from memory while it holds. The case asserts "
                        "memory service during contention, zero unreadable-store "
                        "warnings, and that the "
                        "episode ends with every accepted message delivered "
                        "with no duplicate and nothing left behind"
                    ),
                    "probe": send,
                    "note": (
                        "The repeater's SQLite store was held under an EXCLUSIVE "
                        "lock for the whole arrival-and-sync window; the counts "
                        "above are what survived that contention."
                    ),
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")

        finally:
            if lock_proc is not None:
                try:
                    backlog.release_lock(lock_proc, lock_seconds + 5)
                except Exception as exc:
                    self.not_run(name, f"could not release store lock: {exc}")


CASES = {
    "o1": "case_o1",
    "o2": "case_o2",
    "o3": "case_o3",
    "o4": "case_o4",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir", default=str(LAB_ROOT / "lab/logs/openhop-ordering")
    )
    parser.add_argument("--profile", default="openhop-bridge")
    parser.add_argument("--case", default="all", choices=["all", *CASES])
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="regenerate the summary from the existing case files; run no case",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop after the first case that does not pass",
    )
    args = parser.parse_args(argv)

    matrix = OrderingMatrix(Path(args.evidence_dir), args.profile)
    if args.summary_only:
        summary = case_summary.build_summary(
            matrix.dir,
            prefix="o",
            label="protocol-simulated",
            generated_at=utcnow(),
            profile=args.profile,
        )
        case_summary.write_summary(matrix.dir, summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    try:
        case_summary.bind_source_selection(matrix.dir, LAB_ROOT, args.profile)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    selected = list(CASES) if args.case == "all" else [args.case]
    for key in selected:
        getattr(matrix, CASES[key])()
        if args.fail_fast and any(row["status"] != "PASS" for row in matrix.results):
            break
    summary = case_summary.build_summary(
        matrix.dir,
        prefix="o",
        label="protocol-simulated",
        generated_at=utcnow(),
        profile=args.profile,
    )
    case_summary.write_summary(matrix.dir, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return (
        0
        if matrix.results and all(row["status"] == "PASS" for row in matrix.results)
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
