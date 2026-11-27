#!/usr/bin/env python3
"""Companion ordering cases: arrival order across memory and SQLite.

WHAT THIS DRIVES
----------------
Three questions about the companion's arrival-order contract - deliver
oldest first across the in-memory and persisted queues - each run on the
REAL rig: a real repeater process, its real SQLite store, a real MEDRE
client:

EXIT CONTRACT
-------------
Exit status 0 only when EVERY case this invocation ran is PASS; any FAIL
or NOT RUN makes it 1 (--summary-only writes no case and exits 0).

  o1-older-persisted-newer-memory
      OLDER messages are persisted at the companion while NO client is
      attached.  NEWER messages then arrive while the store is locked, so
      they stay in memory.  The lock lifts and the client syncs: the sync
      must serve the lowest outstanding arrival number across BOTH stores,
      i.e. OLDER then NEWER.

  o2-restart-backlog-then-live
      A backlog is persisted, the REPEATER is restarted (the queue
      survives and the arrival counter re-seeds above the stored rows),
      one NEW live message arrives behind a short lock (so it stays in
      memory), and the client syncs: backlog in order first, then the
      live message.

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
    persisted_while_locked: int, persisted_before_drain: int, requested: int
) -> str | None:
    """Return why the cross-store merge premise failed, or None.

    The merge claim (the sync serves the lowest outstanding arrival
    number across the in-memory AND the persisted queue) needs the newer
    (or live) block to be IN MEMORY ONLY when the client drains:
    nothing of it persisted while the store was locked, and at least one
    row of it still unpersisted when the drain began.  A row persisted
    WHILE THE LOCK WAS HELD means the lock did not hold; every row
    persisted BEFORE THE DRAIN means the in-memory block is empty, and
    FIFO over the persisted store alone would produce the asserted
    order.  Either way the case is NOT RUN - but only AFTER the drain,
    so no rows are left behind.
    """
    if persisted_while_locked > 0:
        return (
            f"{persisted_while_locked} row(s) were persisted while the store "
            "was locked: the lock did not hold, so the newer block was not "
            "in memory only and the merge premise cannot be verified"
        )
    if requested > 0 and persisted_before_drain >= requested:
        return (
            f"every one of the {requested} newer row(s) was already persisted "
            "before the drain began: the in-memory block is empty, and FIFO "
            "over the persisted store alone would produce the asserted order"
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
        name = "o1-older-persisted-newer-memory"
        old_prefix, new_prefix = "p4o1a", "p4o1b"
        restore_bridge = False
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
            # stays in the in-memory queue.
            lock_proc = backlog.hold_exclusive_lock(30)
            if lock_proc is None:
                self.not_run(
                    name,
                    "the repeater's SQLite store could not be locked",
                    {"preconditions": pre},
                )
                return
            new_send = self.peer_send(newer, new_prefix, settle=3.0)
            new_markers_sent = backlog.sent_markers(new_send)
            rows_while_locked, _ = self.repeater_queue(new_prefix)
            persisted_while_locked, _ = backlog.count_nonce_rows(
                rows_while_locked, only=new_markers_sent
            )
            try:
                lock_proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                lock_proc.kill()
                lock_proc.wait()
                raise
            # The merge premise, read BEFORE the drain: how much of the
            # NEWER block the store had already swallowed.
            rows_before_drain, _ = self.repeater_queue(new_prefix)
            newer_persisted_before_drain, _ = backlog.count_nonce_rows(
                rows_before_drain, only=new_markers_sent
            )
            premise_failure = merge_premise_reason(
                persisted_while_locked, newer_persisted_before_drain, newer
            )
            mine = old_markers_sent | new_markers_sent
            self.start_medre()
            self.wait_for(
                lambda: len(self.markers_from_store("p4o1", mine)) >= older + newer,
                timeout=180,
                interval=3.0,
            )
            markers = self.markers_from_store("p4o1", mine)
            verdict = merged_block_verdict(markers, ["o1a", "o1b"])
            remaining_rows, _ = self.repeater_queue("")
            remaining_count, remaining_markers = backlog.count_nonce_rows(
                remaining_rows, only=mine
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
            if premise_failure:
                # The premise failed AFTER the drain ran, so no rows are
                # left behind; the case is NOT RUN with both counts in
                # the record.
                self.not_run(
                    name,
                    premise_failure,
                    {
                        "newer_persisted_while_store_locked": persisted_while_locked,
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
                    "newer_persisted_while_store_locked": persisted_while_locked,
                    "newer_persisted_before_drain": newer_persisted_before_drain,
                    "block_verdict": verdict,
                    "remaining_persisted": remaining_markers,
                    "ordering_promise": (
                        "sync serves the lowest outstanding arrival number "
                        "across the in-memory and the SQLite queue; the OLDER "
                        "persisted block must arrive entirely before the NEWER "
                        "in-memory block"
                    ),
                    "older_send": old_send,
                    "newer_send": new_send,
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

    def case_o2(self, backlog_count: int = 5) -> None:
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
            # A short lock keeps the live message out of the store, so it
            # is genuinely "a new live message in memory" behind the
            # restarted backlog.
            lock_proc = backlog.hold_exclusive_lock(25)
            if lock_proc is None:
                self.not_run(
                    name,
                    "the repeater's SQLite store could not be locked",
                    {"preconditions": pre},
                )
                return
            live_send = self.peer_send(1, live_prefix, settle=3.0)
            live_markers_sent = backlog.sent_markers(live_send)
            rows_live_locked, _ = self.repeater_queue(live_prefix)
            live_persisted_while_locked, _ = backlog.count_nonce_rows(
                rows_live_locked, only=live_markers_sent
            )
            try:
                lock_proc.wait(timeout=50)
            except subprocess.TimeoutExpired:
                lock_proc.kill()
                lock_proc.wait()
                raise
            # The merge premise, read BEFORE the drain: whether the live
            # message really stayed out of the store.
            rows_live_before_drain, _ = self.repeater_queue(live_prefix)
            live_persisted_before_drain, _ = backlog.count_nonce_rows(
                rows_live_before_drain, only=live_markers_sent
            )
            premise_failure = merge_premise_reason(
                live_persisted_while_locked, live_persisted_before_drain, 1
            )
            mine = back_markers_sent | live_markers_sent
            self.start_medre()
            self.wait_for(
                lambda: len(self.markers_from_store("p4o2", mine)) >= backlog_count + 1,
                timeout=180,
                interval=3.0,
            )
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
            if premise_failure:
                # The premise failed AFTER the drain ran, so no rows are
                # left behind; the case is NOT RUN with both counts in
                # the record.
                self.not_run(
                    name,
                    premise_failure,
                    {
                        "live_persisted_while_store_locked": live_persisted_while_locked,
                        "live_persisted_before_drain": live_persisted_before_drain,
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
                    "backlog_requested": backlog_count,
                    "backlog_accepted": back_send.get("accepted_count"),
                    "backlog_persisted_before_restart": len(back_markers_sent),
                    "backlog_persisted_after_restart": persisted_after_restart,
                    "live_accepted": live_send.get("accepted_count"),
                    "live_persisted_while_store_locked": live_persisted_while_locked,
                    "live_persisted_before_drain": live_persisted_before_drain,
                    "block_verdict": verdict,
                    "remaining_persisted": remaining_markers,
                    "ordering_promise": (
                        "after a restart the arrival counter re-seeds above the "
                        "stored rows, so a message that arrives after the restart "
                        "cannot overtake the persisted backlog"
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
        try:
            if not backlog.container_running(MEDRE_BRIDGE):
                self.not_run(name, "the MEDRE bridge container is not running")
                return
            pre = self.preconditions(
                "MEDRE stays attached to companion A while the store is locked"
            )
            warnings_before = self.repeater_log().count(FALLBACK_WARNING)
            lock_proc = backlog.hold_exclusive_lock(lock_seconds)
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
            try:
                lock_proc.wait(timeout=lock_seconds + 30)
            except subprocess.TimeoutExpired:
                lock_proc.kill()
                lock_proc.wait()
                raise
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
                    f"the readable WAL store triggered {warning_delta} unreadable-store warnings"
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
                    "delivered_after_unlock": report["arrivals"],
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
                        "served from memory while it holds, a failed persist is "
                        "retried at a bounded pace so the row lands when the "
                        "lock lifts, and a store that cannot be read at all "
                        "warns once per episode, not once per sync. The case "
                        "asserts the memory service during the lock, the "
                        "once-per-episode warning contract, and that the "
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


CASES = {
    "o1": "case_o1",
    "o2": "case_o2",
    "o3": "case_o3",
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
