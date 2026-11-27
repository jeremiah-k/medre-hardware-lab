#!/usr/bin/env python3
"""Companion backlog/persistence matrix driver (host side).

WHAT THIS DRIVES
----------------
Six questions about the MEDRE <-> openHop companion link, each measured on
the lab's virtual radio rig.  Every case reports PASS / FAIL / NOT RUN with
the counts it measured and the evidence path it wrote.

  c1-backlog-120-medre-restart
      120 unique nonces are sent while MEDRE is STOPPED, so the companion
      persists them with no client attached.  MEDRE is then started and must
      drain them: no loss, no duplicate, order kept.

  c2-backlog-with-live
      A backlog is queued while MEDRE is stopped; MEDRE is started and the
      peer keeps sending live notifications while the drain is in flight.
      The recorded arrival order must prove the interleaving: at least one
      live message observed at MEDRE before the last backlog message was
      delivered, not merely both kinds present after the fact.

  c3-client-sync-during-persist
      The repeater's SQLite store is LOCKED (held from inside the repeater
      container) while a message arrives and the client syncs, so the persist
      and the sync contend for the same store.

  c4-replaced-client
      A second client replaces the first one on companion B (the companion
      serves ONE client at a time).  The replaced client must be fenced and
      nothing may be lost or duplicated.  MEDRE is never evicted from
      companion A.

  c5-kill-mid-drain-one-in-flight
      The fault splice withholds replies from the first client->server
      request it sees after the arm - phased to open a couple of seconds
      after a fresh idle-link probe line, so the parked pump's next probe
      is a full interval away and the wake-driven fetch is that first
      request - so the companion pops message 1 and its frame never
      reaches MEDRE.  The case holds that state - the
      companion's queue must stop shrinking for longer than two fetch
      windows (2 s each) while that one message is outstanding - kills
      MEDRE inside it, clears the fault, and restarts.  The drain is
      stop-and-wait, so exactly that one message is lost: every other
      message is delivered exactly once, nothing is preserved at the
      companion afterwards, and the case records in_flight_at_kill = 1.

  c6-repeater-restart-persisted-queue
      The repeater is restarted while a queue is persisted; the queue must
      survive the restart and be delivered.

COUNTING RULES
--------------
* A message counts when it carries a nonce marker, whether or not it renders
  perfectly: a corrupted duplicate still carries the marker and is counted.
* The wire truth is the repeater's own record (its SQLite queue rows and its
  RX log); the client truth is what the client received.  Both are reported.
* No payload, key or secret is printed - only counts, lengths and digests.
* Every evidence file is labelled "protocol-simulated": the RF MEDIUM is
  simulated by the lab relay.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import subprocess
import sys
import time
import uuid
from pathlib import Path

# The summary aggregator is a SIBLING module: it is imported by path so
# the driver runs from any working directory.
_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import openhop_case_summary as case_summary  # noqa: E402
from openhop_fault_settings import write_fault  # noqa: E402

LAB_ROOT = Path(__file__).resolve().parents[2]
TOOLS = LAB_ROOT / "lab" / "tools"
STATE_TOOL = TOOLS / "openhop_store_snapshot.py"
PEER_PROBE = TOOLS / "openhop_peer_probe.py"

REPEATER_A = "medre-lab-openhop-repeater-a"
REPEATER_B = "medre-lab-openhop-repeater-b"
MEDRE_BRIDGE = "medre-lab-medre-openhop-bridge"
MEDRE_FAULT = "medre-lab-medre-openhop-fault"
PROXY = "medre-lab-openhop-fault-proxy"
RELAY = "medre-lab-openhop-radio-relay"
FAULT_STATE_DIR = LAB_ROOT / "lab/state/openhop-fault"

# The full marker is p4<case>-<index>-<unique>: the case separates the
# cases, the index gives the order, and the unique tail makes one message
# one message (the index alone repeats every run).
NONCE_INDEX = re.compile(r"p4(?P<case>[a-z0-9]+)-(?P<index>\d+)-(?P<unique>[0-9a-f]+)")
# The probe anchor for the c5 arm phasing: the parked pump repeats its
# idle-link probe at one interval, and a healthy probe completes quietly
# within milliseconds of its line.  Phasing the arm a couple of seconds
# after a FRESH probe line puts the whole interval between the arm and the
# next probe, so the wake-driven fetch is the first client->server chunk
# the arm can see.
PROBE_LINE = "probing the idle receive link"


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def run(cmd: list[str], timeout: int = 600) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def checked_docker(*args: str, timeout: int = 180) -> str:
    rc, output, _ = run(["docker", *args], timeout=timeout)
    if rc:
        raise RuntimeError(f"docker {args[0]} failed (exit {rc})")
    return output.strip()


def restart_repeater(container: str) -> None:
    before = checked_docker("inspect", "-f", "{{.State.StartedAt}}", container)
    checked_docker("restart", container, timeout=300)
    after = checked_docker("inspect", "-f", "{{.State.StartedAt}}", container)
    if not after or before == after or not container_running(container):
        raise RuntimeError(f"did not observe a new start of {container}")


def hold_exclusive_lock(seconds: int) -> subprocess.Popen[bytes] | None:
    script = (
        "import sqlite3,time,sys;"
        "c=sqlite3.connect('/var/lib/openhop_repeater/repeater.db',"
        "timeout=1,isolation_level=None);"
        "c.execute('BEGIN EXCLUSIVE');"
        "print('locked',flush=True);"
        "time.sleep(float(sys.argv[1]));"
        "c.execute('ROLLBACK')"
    )
    proc = subprocess.Popen(
        ["docker", "exec", REPEATER_A, "python3", "-c", script, str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    confirmed = False
    try:
        # A partial line can block readline() even after select marks it ready.
        deadline = time.monotonic() + 20
        expected = b"locked\n"
        marker = b""
        while len(marker) < len(expected):
            ready, _, _ = select.select(
                [proc.stdout], [], [], max(0, deadline - time.monotonic())
            )
            if not ready or time.monotonic() >= deadline:
                return None
            chunk = os.read(proc.stdout.fileno(), len(expected) - len(marker))
            if not chunk:
                return None
            marker += chunk
        confirmed = marker == expected and proc.poll() is None
        return proc if confirmed else None
    finally:
        proc.stdout.close()
        if not confirmed:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# Pure helpers (offline-testable)
# ---------------------------------------------------------------------------
def nonce_marker(text: str) -> str | None:
    """Return the FULL nonce marker embedded in a text, or None.

    The marker is ``p4<case>-<index>-<unique>``.  The unique tail is what
    makes one message one message: the index alone repeats every run and in
    every case, so counting on the index would call a correct run a pile of
    duplicates.
    """
    match = NONCE_INDEX.search(str(text or ""))
    return match.group(0) if match else None


def nonce_case(text: str) -> str | None:
    """Return the case part of a nonce marker, or None."""
    match = NONCE_INDEX.search(str(text or ""))
    return match.group("case") if match else None


def nonce_index(text: str) -> int | None:
    """Return the index embedded in a nonce marker, or None."""
    match = NONCE_INDEX.search(str(text or ""))
    return int(match.group("index")) if match else None


def sent_markers(probe_result: dict) -> set[str]:
    """Return the markers THIS run sent, so the counts cover this run only.

    MEDRE's store is cumulative: it still holds the events of an earlier run
    of the same case.  Without this filter a re-run would count the earlier
    run's events as duplicates of this one's.
    """
    markers: set[str] = set()
    for nonce in probe_result.get("nonces") or []:
        marker = nonce_marker(nonce)
        if marker:
            markers.add(marker)
    return markers


def count_nonce_rows(
    rows: list[dict], field: str = "text", only: set[str] | None = None
) -> tuple[int, list[str]]:
    """Count nonce-bearing rows and return their markers in row order.

    A row counts when it carries a nonce marker, whether or not it renders
    perfectly: a corrupted duplicate still carries the marker and is counted.
    ``only`` restricts the count to the markers this run sent.
    """
    markers: list[str] = []
    for row in rows:
        marker = nonce_marker(row.get(field) or "")
        if marker is None:
            continue
        if only is not None and marker not in only:
            continue
        markers.append(marker)
    return len(markers), markers


def ordering_report(markers: list[str]) -> dict:
    """Report duplicates, losses and ordering over a marker sequence.

    Ordering is judged WITHIN a case: the nonces of one case must arrive in
    the order they were sent.  The ordering of two different cases relative
    to each other is not a property of the link.
    """
    seen: set[str] = set()
    duplicates = 0
    out_of_order = 0
    highest: dict[str, int] = {}
    for marker in markers:
        if marker in seen:
            duplicates += 1
        else:
            seen.add(marker)
        case = nonce_case(marker) or ""
        index = nonce_index(marker)
        if index is None:
            continue
        if index < highest.get(case, -1):
            out_of_order += 1
        highest[case] = max(highest.get(case, -1), index)
    return {
        "arrivals": len(markers),
        "distinct": len(seen),
        "duplicate_arrivals": duplicates,
        "out_of_order_arrivals": out_of_order,
        "distinct_indices": sorted(seen),
    }


def missing_indices(markers: list[str], expected: set[str]) -> list[str]:
    """Return the markers of this run that never arrived."""
    seen = set(markers)
    return sorted(expected - seen)


def interleaving_report(
    arrival_order: list[str],
    backlog_markers: set[str],
    live_markers: set[str],
) -> dict:
    """Locate the live arrivals inside the backlog's delivery span.

    MEDRE's store returns the events in recorded arrival order, so the
    positions decide the interleaving: the case proves a live message was
    handled while the backlog was still being delivered when at least one
    live marker was recorded BEFORE the last backlog marker.
    """
    backlog_positions = [
        index for index, marker in enumerate(arrival_order) if marker in backlog_markers
    ]
    live_positions = [
        index for index, marker in enumerate(arrival_order) if marker in live_markers
    ]
    return {
        "backlog_arrivals": len(backlog_positions),
        "live_arrivals": len(live_positions),
        "first_live_position": min(live_positions) if live_positions else None,
        "last_backlog_position": max(backlog_positions) if backlog_positions else None,
        "live_arrived_before_last_backlog": bool(
            live_positions
            and backlog_positions
            and min(live_positions) < max(backlog_positions)
        ),
    }


def backlog_with_live_failures(
    *,
    delivered_distinct: int,
    expected_distinct: int,
    duplicate_arrivals: int,
    arrival_order: list[str],
    backlog_markers: set[str],
    live_markers: set[str],
) -> list[str]:
    """Judge the backlog-with-live case by its interleaving claim.

    Both kinds arriving eventually is not what the case is named for: it
    measures a backlog drain that competes with live traffic.  On top of
    the accounting (every backlog and live message delivered, zero
    duplicates) the recorded arrival order must therefore prove the
    interleaving itself - at least one live message observed at MEDRE
    before the last backlog message was delivered.  Fail closed: an
    arrival order that shows no live arrival inside the backlog's
    delivery span, or no ordered record at all, is not a pass.
    """
    failures: list[str] = []
    if delivered_distinct != expected_distinct:
        failures.append(
            f"MEDRE recorded {delivered_distinct} distinct nonces, expected "
            f"{expected_distinct}"
        )
    if duplicate_arrivals:
        failures.append(
            f"{duplicate_arrivals} duplicate arrivals while the backlog "
            "competed with live notifications"
        )
    interleaving = interleaving_report(arrival_order, backlog_markers, live_markers)
    if not interleaving["live_arrived_before_last_backlog"]:
        failures.append(
            "no live message was observed at MEDRE before the last backlog "
            "delivery: the recorded arrival order does not prove a live "
            "message was handled while the backlog drain was in flight"
        )
    return failures


def kill_mid_drain_failures(
    *,
    accepted: int,
    delivered_distinct: int,
    duplicate_wakes: int,
    preserved_distinct: int,
    missing: list[str],
    delivered_before_kill: int,
    queue_samples: list[int],
    hold_seconds: float | None,
) -> list[str]:
    """Judge the killed-with-one-in-flight case by the stop-and-wait contract.

    The splice withheld replies from the first client->server request after
    the arm, so the companion popped exactly one message whose frame never
    reached MEDRE.  What the case asserts:

    * the hold was real: the companion's queue stopped shrinking across
      samples spanning longer than two fetch windows (2 s each) while
      exactly that one message was outstanding, and MEDRE was killed with
      nothing delivered;
    * zero duplicates anywhere in the run;
    * exactly one message is missing after the restart, and it is the
      backlog's FIRST message: the drain pops oldest first, so the
      withheld frame is the first pop;
    * every other accepted message was delivered (nothing is preserved at
      the companion afterwards).
    """
    failures: list[str] = []
    if accepted <= 0:
        failures.append("the peer sent no accepted message")
        return failures
    if hold_seconds is None or hold_seconds < 4.0:
        failures.append("the queue hold spanned less than two fetch windows (2 s each)")
    if not queue_samples:
        failures.append("no queue-depth sample was taken across the hold")
    elif len(set(queue_samples)) != 1:
        failures.append(
            "the companion's queue kept shrinking while the one message was "
            f"outstanding: samples {queue_samples}"
        )
    elif queue_samples[0] != accepted - 1:
        failures.append(
            f"the companion held {queue_samples[0]} queued messages while one "
            f"was outstanding; expected {accepted - 1} of {accepted}"
        )
    if delivered_before_kill:
        failures.append(
            f"{delivered_before_kill} message(s) were delivered before the "
            "kill; the held state must have nothing delivered"
        )
    if duplicate_wakes:
        failures.append(f"{duplicate_wakes} duplicate wake(s) reached MEDRE's store")
    if delivered_distinct + preserved_distinct != accepted - 1:
        failures.append(
            f"{delivered_distinct} delivered + {preserved_distinct} preserved "
            f"at the companion != {accepted - 1} of {accepted} messages: the "
            "stop-and-wait drain loses at most the one in-flight message"
        )
    if preserved_distinct:
        failures.append(
            f"{preserved_distinct} message(s) were still queued at the "
            "companion after the restarted drain should have emptied it"
        )
    if len(missing) != 1:
        failures.append(
            f"{len(missing)} message(s) are missing after the restart "
            f"({sorted(missing)}); the stop-and-wait drain loses exactly the "
            "one message whose reply was withheld"
        )
    elif nonce_index(missing[0]) != 0:
        failures.append(
            f"the missing message is {missing[0]}; the drain pops oldest "
            "first, so the withheld reply must be the backlog's first message"
        )
    return failures


# ---------------------------------------------------------------------------
# Lab plumbing
# ---------------------------------------------------------------------------
def container_running(name: str) -> bool:
    rc, out, _ = run(["docker", "inspect", "-f", "{{.State.Running}}", name])
    return rc == 0 and out.strip() == "true"


def container_exists(name: str) -> bool:
    return run(["docker", "inspect", "-f", "{{.State.Status}}", name])[0] == 0


def container_logs(name: str) -> str:
    """Return the container's logs.

    The repeater logs to STDERR, so both streams are needed: counting only
    stdout would silently report zero of everything.
    """
    _, out, err = run(["docker", "logs", name], timeout=120)
    return out + err


def peer_send(
    profile: str,
    count: int,
    prefix: str,
    settle: float = 3.0,
    host: str = "openhop-repeater-b",
    timeout: int = 1800,
) -> dict:
    """Send N unique nonces from a one-shot SDK client on the peer companion."""
    cmd = [
        "docker",
        "compose",
        "--profile",
        profile,
        "run",
        "--rm",
        "--no-deps",
        "-v",
        f"{PEER_PROBE}:/opt/openhop-peer-probe/openhop_peer_probe.py:ro",
        "openhop-bridge-probe",
        "python3",
        "/opt/openhop-peer-probe/openhop_peer_probe.py",
        "--mode",
        "send",
        "--host-b",
        host,
        "--port-b",
        "5000",
        "--count",
        str(count),
        "--prefix",
        prefix,
        "--settle",
        str(settle),
    ]
    rc, out, err = run(cmd, timeout=timeout)
    text = out.strip()
    start = text.find("{")
    if start < 0:
        return {"pass": False, "error": "the probe produced no JSON", "exit_status": rc}
    try:
        data = json.loads(text[start:])
    except ValueError as exc:
        return {"pass": False, "error": str(exc), "exit_status": rc}
    data["exit_status"] = rc
    data["accepted_count"] = data.get("accepted_count", 0)
    return data


def state_in(container: str, mode: str, needle: str = "") -> dict:
    """Read a store snapshot from inside a container (read-only)."""
    rc, _, _ = run(
        [
            "docker",
            "cp",
            str(STATE_TOOL),
            f"{container}:/tmp/openhop_store_snapshot.py",
        ]
    )
    if rc:
        raise RuntimeError(f"could not copy the snapshot reader into {container}")
    cmd = [
        "docker",
        "exec",
        container,
        "python3",
        "/tmp/openhop_store_snapshot.py",
        "--mode",
        mode,
    ]
    if needle:
        cmd += ["--needle", needle]
    rc, out, _ = run(cmd, timeout=300)
    try:
        snapshot = json.loads(out)
    except ValueError as exc:
        raise RuntimeError(f"invalid {mode} snapshot from {container}") from exc
    if rc or not isinstance(snapshot, dict) or "error" in snapshot:
        raise RuntimeError(
            f"could not read {mode} snapshot from {container} (exit {rc})"
        )
    return snapshot


class Matrix:
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

    def preconditions(self, note: str) -> dict:
        """Capture the state the case depends on, BEFORE any traffic.

        The backlog cases only mean something if MEDRE really was stopped
        and companion A really had no client attached: otherwise the
        messages were delivered live and no backlog was ever persisted.
        """
        medre_state = run(
            ["docker", "inspect", "-f", "{{.State.Status}}", MEDRE_BRIDGE]
        )[1].strip()
        a_state = run(["docker", "inspect", "-f", "{{.State.Status}}", REPEATER_A])[
            1
        ].strip()
        a_log = container_logs(REPEATER_A)
        # The companion logs "Companion client connected (port=5000)" and
        # "Companion client disconnected (port=5000)".  The LAST of those two
        # decides whether a client is attached right now.
        client_lines = [
            line
            for line in a_log.splitlines()
            if "Companion client connected" in line
            or "Companion client disconnected" in line
        ]
        attached = False
        for line in client_lines:
            if "Companion client connected" in line:
                attached = True
            elif "Companion client disconnected" in line:
                attached = False
        return {
            "note": note,
            "captured_at": utcnow(),
            "medre_container_status": medre_state,
            "medre_stopped": medre_state != "running",
            "repeater_a_status": a_state,
            "companion_a_client_attached": attached,
            "companion_a_client_free": not attached,
            "repeater_a_client_lines": client_lines[-4:],
        }

    def wait_for(self, predicate, timeout: float, interval: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def medre_events(
        self,
        needle: str,
        only: set[str] | None = None,
        container: str = MEDRE_BRIDGE,
    ) -> list[dict]:
        """Return MEDRE's canonical events carrying the needle.

        ``only`` restricts the result to the markers of THIS run: MEDRE's
        store is cumulative, so an unscoped read would also return the
        events of an earlier run of the same case.  ``container`` selects
        which MEDRE instance to read: the c5 case runs the fault MEDRE
        behind the splice.
        """
        snapshot = state_in(container, "medre", needle)
        events = snapshot.get("canonical_events") or []
        if only is None:
            return events
        scoped = []
        for event in events:
            marker = nonce_marker(json.dumps(event, sort_keys=True))
            if marker is not None and marker in only:
                scoped.append(event)
        return scoped

    def repeater_queue(self, container: str, needle: str) -> tuple[list[dict], dict]:
        snapshot = state_in(container, "repeater", needle)
        rows = snapshot.get("companion_messages") or []
        return rows, snapshot

    # -- c5 rig control: the fault splice and its MEDRE instance ---------
    # The killed-with-one-in-flight case needs the link held while one
    # popped message is outstanding, which only the fault splice can do.
    # The helpers mirror the fault matrix driver's proven rig pattern.
    def remove_fault_medre(self) -> None:
        run(["docker", "rm", "-f", MEDRE_FAULT], timeout=180)

    def remove_splice(self) -> None:
        run(["docker", "rm", "-f", PROXY], timeout=120)

    def start_splice(self, fault: dict) -> None:
        """Start the fault splice with THIS case's fault arguments.

        The splice is started with DOCKER RUN and an explicit network
        alias rather than compose: the MEDRE client resolves the splice's
        NAME once, at connect time, so the alias has to be on the network
        the client is on.  The fault arguments live in a file the splice
        re-reads per chunk, so the case arms and clears the fault without
        restarting the splice.
        """
        FAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        write_fault(FAULT_STATE_DIR / "fault.json", fault)
        self.remove_splice()
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                PROXY,
                "--network",
                "medre-lab-openhop-radio",
                "--network-alias",
                "openhop-fault-proxy",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{LAB_ROOT / 'lab/openhop-radio/openhop_fault_proxy.py'}:"
                "/opt/openhop-fault/openhop_fault_proxy.py:ro",
                "-v",
                f"{FAULT_STATE_DIR}:/opt/openhop-fault:ro",
                "medre-lab/openhop-radio-relay:latest",
                "python3",
                "/opt/openhop-fault/openhop_fault_proxy.py",
                "--listen-host",
                "0.0.0.0",  # nosec B104 # The splice serves the private lab Docker network only; compose publishes no host port.
                "--listen-port",
                "5050",
                "--upstream-host",
                "openhop-repeater-a",
                "--upstream-port",
                "5000",
                "--fault-file",
                "/opt/openhop-fault/fault.json",
            ],
            timeout=300,
        )

    def start_fault_medre(self) -> bool:
        """Start the MEDRE instance whose meshcore peer is the splice.

        The fault MEDRE mounts lab/state/config, so its config template
        has to be generated there first.
        """
        target = LAB_ROOT / "lab/state/config/medre-openhop-fault.yaml"
        source = LAB_ROOT / "lab/config/medre-openhop-fault.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        run(
            [
                "docker",
                "compose",
                "--profile",
                "openhop-bridge",
                "--profile",
                "openhop-fault",
                "up",
                "-d",
                "--no-deps",
                "medre-openhop-fault",
            ],
            timeout=300,
        )
        return self.wait_for(lambda: container_running(MEDRE_FAULT), 90, interval=2.0)

    # -- cases -----------------------------------------------------------
    def case_c1(self, count: int = 120) -> None:
        name = "c1-backlog-120-medre-restart"
        prefix = "p4c1"
        restore_bridge = False
        try:
            if not container_running(MEDRE_BRIDGE):
                self.not_run(name, "the MEDRE bridge container is not running")
                return
            restore_bridge = True
            checked_docker("stop", MEDRE_BRIDGE)
            pre = self.preconditions(
                "MEDRE stopped, so companion A has no client attached"
            )
            if not pre["medre_stopped"]:
                self.not_run(
                    name,
                    "MEDRE did not stop, so no backlog could form",
                    {"preconditions": pre},
                )
                return
            if not pre["companion_a_client_free"]:
                self.not_run(
                    name,
                    "companion A still had a client attached, so no backlog could form",
                    {"preconditions": pre},
                )
                return
            a_rx_before = container_logs(REPEATER_A).count("RX GRP_TXT")
            send = peer_send(self.profile, count, prefix, settle=5.0)
            accepted = int(send.get("accepted_count") or 0)
            rows, repeater_state = self.repeater_queue(REPEATER_A, prefix)
            mine = sent_markers(send)
            persisted_count, persisted_indices = count_nonce_rows(rows, only=mine)
            persisted_report = ordering_report(persisted_indices)
            checked_docker("start", MEDRE_BRIDGE)
            self.wait_for(lambda: container_running(MEDRE_BRIDGE), 90)
            self.wait_for(
                lambda: len(self.medre_events(prefix, only=mine)) >= persisted_count,
                timeout=180,
                interval=3.0,
            )
            mine = sent_markers(send)
            events = self.medre_events(prefix, only=mine)
            markers = [
                marker
                for marker in (
                    nonce_marker(json.dumps(event, sort_keys=True)) for event in events
                )
                if marker is not None and marker in mine
            ]
            report = ordering_report(markers)
            a_rx_after = container_logs(REPEATER_A).count("RX GRP_TXT")
            failures: list[str] = []
            if accepted == 0:
                failures.append("the peer sent no accepted message")
            if persisted_count == 0:
                failures.append(
                    "the repeater persisted no nonce-bearing message while no client was attached"
                )
            if set(persisted_indices) != mine or set(markers) != mine:
                failures.append(
                    "the persisted and delivered markers must match the accepted batch"
                )
            if report["distinct"] != persisted_count:
                failures.append(
                    f"MEDRE recorded {report['distinct']} of the {persisted_count} persisted nonces"
                )
            if report["duplicate_arrivals"]:
                failures.append(
                    f"MEDRE recorded {report['duplicate_arrivals']} duplicate arrivals"
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
                    "peer_accepted_sends": accepted,
                    "persisted_at_companion": persisted_count,
                    "persisted_report": persisted_report,
                    "drained_at_medre": report["arrivals"],
                    "drain_report": report,
                    "missing_nonce_indices": missing_indices(
                        markers, sent_markers(send)
                    ),
                    "repeater_rx_group_text_delta": a_rx_after - a_rx_before,
                    "repeater_queue_total": repeater_state.get(
                        "companion_message_total"
                    ),
                    "probe": send,
                    "note": (
                        "MEDRE's meshcore adapter dedups inbound packets by native "
                        "message id, so the client-level duplicate count here is a "
                        "REGRESSION counter over distinct canonical events; the wire "
                        "truth is the repeater's own RX count and persisted rows."
                    ),
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")
        finally:
            if restore_bridge and not container_running(MEDRE_BRIDGE):
                try:
                    checked_docker("start", MEDRE_BRIDGE)
                except (RuntimeError, subprocess.SubprocessError) as exc:
                    self.not_run(name, f"could not restore the MEDRE bridge: {exc}")

    def case_c2(self, backlog: int = 40, live: int = 20) -> None:
        name = "c2-backlog-with-live"
        back_prefix = "p4c2b"
        live_prefix = "p4c2l"
        restore_bridge = False
        try:
            if not container_running(MEDRE_BRIDGE):
                self.not_run(name, "the MEDRE bridge container is not running")
                return
            restore_bridge = True
            checked_docker("stop", MEDRE_BRIDGE)
            pre = self.preconditions(
                "MEDRE stopped, so companion A has no client attached"
            )
            if not pre["medre_stopped"]:
                self.not_run(
                    name,
                    "MEDRE did not stop, so no backlog could form",
                    {"preconditions": pre},
                )
                return
            if not pre["companion_a_client_free"]:
                self.not_run(
                    name,
                    "companion A still had a client attached, so no backlog could form",
                    {"preconditions": pre},
                )
                return
            back_send = peer_send(self.profile, backlog, back_prefix, settle=3.0)
            rows, _ = self.repeater_queue(REPEATER_A, back_prefix)
            persisted_count, _ = count_nonce_rows(rows, only=sent_markers(back_send))
            checked_docker("start", MEDRE_BRIDGE)
            self.wait_for(lambda: container_running(MEDRE_BRIDGE), 90)
            time.sleep(4)
            live_send = peer_send(self.profile, live, live_prefix, settle=8.0)
            back_markers = sent_markers(back_send)
            live_markers_sent = sent_markers(live_send)
            self.wait_for(
                lambda: len(self.medre_events(back_prefix, only=back_markers))
                + len(self.medre_events(live_prefix, only=live_markers_sent))
                >= persisted_count + int(live_send.get("accepted_count") or 0),
                timeout=180,
                interval=3.0,
            )
            # One combined snapshot keeps the recorded arrival order across
            # both kinds: the interleaving proof reads this order (the
            # store returns canonical_events ORDER BY created_at), so two
            # separately-ordered reads would not do.
            mine = back_markers | live_markers_sent
            events = self.medre_events("p4c2", only=mine)
            markers = [
                marker
                for marker in (
                    nonce_marker(json.dumps(event, sort_keys=True)) for event in events
                )
                if marker is not None and marker in mine
            ]
            report = ordering_report(markers)
            live_markers = [
                marker
                for marker in (
                    nonce_marker(json.dumps(event, sort_keys=True))
                    for event in self.medre_events(live_prefix, only=live_markers_sent)
                )
                if marker is not None and marker in live_markers_sent
            ]
            live_report = ordering_report(live_markers)
            interleaving = interleaving_report(markers, back_markers, live_markers_sent)
            failures: list[str] = []
            if (
                back_send.get("pass") is not True
                or live_send.get("pass") is not True
                or persisted_count != backlog
                or len(live_markers_sent) != live
            ):
                failures.append(
                    "the case needs both a full persisted backlog and an accepted live batch"
                )
            failures += backlog_with_live_failures(
                delivered_distinct=report["distinct"],
                expected_distinct=persisted_count
                + int(live_send.get("accepted_count") or 0),
                duplicate_arrivals=report["duplicate_arrivals"],
                arrival_order=markers,
                backlog_markers=back_markers,
                live_markers=live_markers_sent,
            )
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "preconditions": pre,
                    "verdict_rule": (
                        "backlog-with-live verdict: the full persisted backlog "
                        "and the accepted live batch are all delivered with "
                        "zero duplicates, AND the recorded arrival order proves "
                        "the interleaving - at least one live message observed "
                        "at MEDRE before the last backlog message was delivered"
                    ),
                    "backlog_requested": backlog,
                    "backlog_persisted": persisted_count,
                    "live_requested": live,
                    "live_accepted": live_send.get("accepted_count"),
                    "combined_report": report,
                    "live_only_report": live_report,
                    "interleaving_report": interleaving,
                    "backlog_send": back_send,
                    "live_send": live_send,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")
        finally:
            if restore_bridge and not container_running(MEDRE_BRIDGE):
                try:
                    checked_docker("start", MEDRE_BRIDGE)
                except (RuntimeError, subprocess.SubprocessError) as exc:
                    self.not_run(name, f"could not restore the MEDRE bridge: {exc}")

    def case_c3(self, count: int = 10) -> None:
        name = "c3-client-sync-during-persist"
        prefix = "p4c3"
        try:
            if not container_running(MEDRE_BRIDGE):
                self.not_run(name, "the MEDRE bridge container is not running")
                return
            pre = self.preconditions(
                "MEDRE stays attached to companion A while the store is locked"
            )
            if not container_running(MEDRE_BRIDGE):
                self.not_run(name, "the MEDRE bridge container is not running")
                return
            # Hold an EXCLUSIVE lock on the repeater's SQLite store from
            # inside the repeater container: the persist path and the
            # client's sync path then contend for the same database.
            lock_proc = hold_exclusive_lock(45)
            if lock_proc is None:
                self.not_run(name, "the repeater's SQLite store could not be locked")
                return
            send = peer_send(self.profile, count, prefix, settle=3.0)
            mine = sent_markers(send)
            self.wait_for(
                lambda: len(self.medre_events(prefix, only=mine)) >= 1,
                timeout=60,
                interval=2.0,
            )
            events = self.medre_events(prefix, only=mine)
            markers = [
                marker
                for marker in (
                    nonce_marker(json.dumps(event, sort_keys=True)) for event in events
                )
                if marker is not None and marker in mine
            ]
            observed_while_locked = lock_proc.poll() is None and bool(markers)
            repeater_log = container_logs(REPEATER_A)
            lock_errors = [
                line
                for line in repeater_log.splitlines()
                if "locked" in line.lower() or "database is locked" in line.lower()
            ][-5:]
            try:
                lock_proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                lock_proc.kill()
                lock_proc.wait()
                raise
            rows, _ = self.repeater_queue(REPEATER_A, prefix)
            persisted_count, persisted = count_nonce_rows(rows, only=mine)
            events = self.medre_events(prefix, only=mine)
            markers = [
                marker
                for event in events
                if (marker := nonce_marker(json.dumps(event, sort_keys=True))) in mine
            ]
            report = ordering_report(markers)
            failures: list[str] = []
            if not observed_while_locked:
                failures.append("no delivery was observed while the lock was held")
            if set(markers) | set(persisted) != mine:
                failures.append(
                    "delivered and preserved messages do not cover the accepted batch"
                )
            if report["duplicate_arrivals"] or set(markers) & set(persisted):
                failures.append(
                    "a marker was delivered twice or remained queued after delivery"
                )
            if not send.get("accepted_count"):
                failures.append(
                    "the peer sent no accepted message while the store was locked"
                )
            # Use MEDRE's recorded arrival order across both queue tiers.
            order = [nonce_index(marker) for marker in markers]
            first_two = [index for index in order if index is not None][:2]
            ordering_verdict = "PASS"
            if len(first_two) == 2 and first_two[0] > first_two[1]:
                ordering_verdict = "FAIL"
                failures.append(
                    f"the newer message {first_two[0]} was delivered before the older "
                    f"message {first_two[1]}"
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
                    "arrived_at_medre": report["arrivals"],
                    "distinct_at_medre": report["distinct"],
                    "duplicate_arrivals": report["duplicate_arrivals"],
                    "arrival_order_indices": order,
                    "first_two_arrival_indices": first_two,
                    "ordering_verdict": ordering_verdict,
                    "ordering_note": "Indices show the recorded arrival order.",
                    "persisted_after_unlock": persisted_count,
                    "repeater_lock_error_lines": lock_errors,
                    "probe": send,
                    "note": (
                        "The repeater's SQLite store was held under an EXCLUSIVE "
                        "lock for 45 s while the message arrived and the client "
                        "synced; the counts above are what survived that contention."
                    ),
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")

    def case_c4(self) -> None:
        name = "c4-replaced-client"
        try:
            if not container_running(REPEATER_B):
                self.not_run(name, "the peer repeater is not running")
                return
            # Two clients on companion B: the second replaces the first.
            # MEDRE is never touched: it is the only client of companion A.
            cmd = [
                "docker",
                "compose",
                "--profile",
                self.profile,
                "run",
                "--rm",
                "--no-deps",
                "-v",
                f"{PEER_PROBE}:/opt/openhop-peer-probe/openhop_peer_probe.py:ro",
                "openhop-bridge-probe",
                "python3",
                "/opt/openhop-peer-probe/openhop_peer_probe.py",
                "--mode",
                "listen",
                "--host-b",
                "openhop-repeater-b",
                "--port-b",
                "5000",
                "--needle",
                "p4c4",
                "--window",
                "45",
            ]
            baseline = container_logs(REPEATER_B).count("evicting previous connection")
            first = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            try:
                time.sleep(6)
                second = run(cmd, timeout=180)
                first_out, _ = first.communicate(timeout=120)
            finally:
                if first.poll() is None:
                    first.kill()
                    first.communicate()
            out = second[1].strip()
            payload: dict = {}
            start = out.find("{")
            if start >= 0:
                try:
                    payload = json.loads(out[start:])
                except ValueError:
                    payload = {"error": "the second client's JSON did not parse"}
            first_payload: dict = {}
            start = first_out.find("{")
            if start >= 0:
                try:
                    first_payload = json.loads(first_out[start:])
                except ValueError:
                    first_payload = {"error": "the first client's JSON did not parse"}
            b_log = container_logs(REPEATER_B)
            evictions = [
                line for line in b_log.splitlines() if "evict" in line.lower()
            ][-5:]
            failures: list[str] = []
            if second[0] or payload.get("pass") is not True:
                failures.append("the replacing client produced no usable result")
            if first.returncode or first_payload.get("pass") is not True:
                failures.append("the replaced client produced no usable result")
            if b_log.count("evicting previous connection") <= baseline:
                failures.append("the repeater did not record a client replacement")
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "replacing_client": payload,
                    "replaced_client": first_payload,
                    "repeater_b_eviction_lines": evictions,
                    "note": (
                        "The companion serves one client at a time; the second "
                        "connection replaces the first. MEDRE was NOT touched: it "
                        "is the only client of companion A."
                    ),
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")

    def case_c5(self, count: int = 30) -> None:
        name = "c5-kill-mid-drain-one-in-flight"
        # The prefix is unique per run: the hold-state accounting must only
        # see THIS run's messages, never the stale rows an earlier run left
        # in the companion or MEDRE stores.
        prefix = f"p4c5{uuid.uuid4().hex[:8]}"
        first_connect = re.compile(
            r"started \(mode=tcp, connected=True\)|reconnected successfully",
            re.IGNORECASE,
        )
        try:
            if not container_running(REPEATER_A):
                self.not_run(
                    name,
                    "repeater A is not running (bring the bridge profile up first)",
                )
                return
            # The fault MEDRE owns companion A for this case through the
            # splice, so the bridge MEDRE is stopped first (the companion
            # serves ONE client at a time) and restored at the end.
            if container_running(MEDRE_BRIDGE):
                run(["docker", "stop", MEDRE_BRIDGE], timeout=180)
            self.remove_fault_medre()
            self.start_splice({})
            if not self.wait_for(lambda: container_running(PROXY), 60, interval=1.0):
                self.not_run(name, "the fault splice did not start")
                return
            if not self.start_fault_medre():
                self.not_run(name, "the fault MEDRE container did not start")
                return
            if not self.wait_for(
                lambda: first_connect.search(container_logs(MEDRE_FAULT)) is not None,
                120,
                interval=2.0,
            ):
                self.not_run(name, "the fault MEDRE never completed its first connect")
                return
            # Let the start-up drain finish and the pump enter its parked
            # probe cycle: the arm must land while no request is in
            # flight, so the first client->server chunk after the arm is
            # the wake-driven fetch for the fresh backlog.  The arm phases
            # itself a couple of seconds after a FRESH probe line - the
            # healthy probe completes within milliseconds of its line - so
            # the whole probe interval separates the arm from the next
            # probe.  A stall opened on a probe instead of the wake-driven
            # fetch would withhold the notifications and pop nothing; the
            # judge below fails that shape loudly.
            time.sleep(25)
            baseline_probes = container_logs(MEDRE_FAULT).count(PROBE_LINE)
            phasing_started = time.monotonic()
            fresh_probe_phasing = False
            probe_wait_seconds = None
            while time.monotonic() - phasing_started < 90:
                if container_logs(MEDRE_FAULT).count(PROBE_LINE) > baseline_probes:
                    fresh_probe_phasing = True
                    probe_wait_seconds = time.monotonic() - phasing_started
                    time.sleep(2)
                    break
                time.sleep(1.0)
            armed_at = time.time()
            write_fault(
                FAULT_STATE_DIR / "fault.json",
                {"stall_server_to_client_on_request": True},
            )
            send = peer_send(self.profile, count, prefix, settle=5.0)
            mine = sent_markers(send)
            accepted = int(send.get("accepted_count") or 0)
            # The hold: with the first fetch's reply withheld, the companion
            # has popped exactly one message whose frame never left the
            # splice.  The pump parks after the fetch window (2 s) elapses
            # with no traffic, so the queue must hold still; four samples
            # spanning 7.5 s prove it stopped shrinking for longer than two
            # fetch windows while that one message was outstanding.
            queue_samples: list[int] = []
            sample_times: list[float] = []
            for _ in range(4):
                rows, _state = self.repeater_queue(REPEATER_A, prefix)
                held, _markers = count_nonce_rows(rows, only=mine)
                queue_samples.append(held)
                sample_times.append(time.time())
                time.sleep(2.5)
            hold_seconds = (
                sample_times[-1] - sample_times[0] if len(sample_times) > 1 else None
            )
            delivered_before_kill = len(
                self.medre_events(prefix, only=mine, container=MEDRE_FAULT)
            )
            # Kill MEDRE inside the held state.  The restart policy is
            # pinned to "no" for the kill: an unless-stopped container is
            # restarted by the daemon on a kill, which would race the
            # controlled restart below.
            checked_docker("update", "--restart=no", MEDRE_FAULT, timeout=120)
            checked_docker("kill", MEDRE_FAULT, timeout=120)
            killed_at = time.time()
            if (
                checked_docker("inspect", "-f", "{{.State.Running}}", MEDRE_FAULT)
                != "false"
            ):
                raise RuntimeError("MEDRE did not stop at the controlled kill")
            write_fault(FAULT_STATE_DIR / "fault.json", {})
            checked_docker("start", MEDRE_FAULT)
            run(
                ["docker", "update", "--restart=unless-stopped", MEDRE_FAULT],
                timeout=120,
            )
            self.wait_for(lambda: container_running(MEDRE_FAULT), 90)
            self.wait_for(
                lambda: len(self.medre_events(prefix, only=mine, container=MEDRE_FAULT))
                >= accepted - 1,
                timeout=240,
                interval=3.0,
            )
            # The restarted drain: a ~30-message backlog drains in ~2 s on
            # this rig, so a short margin is plenty.
            time.sleep(15)
            events = self.medre_events(prefix, only=mine, container=MEDRE_FAULT)
            markers = [
                marker
                for marker in (
                    nonce_marker(json.dumps(event, sort_keys=True)) for event in events
                )
                if marker is not None and marker in mine
            ]
            report = ordering_report(markers)
            rows, _state = self.repeater_queue(REPEATER_A, prefix)
            preserved_count, _ = count_nonce_rows(rows, only=mine)
            missing = missing_indices(markers, mine)
            failures = kill_mid_drain_failures(
                accepted=accepted,
                delivered_distinct=report["distinct"],
                duplicate_wakes=report["duplicate_arrivals"],
                preserved_distinct=preserved_count,
                missing=missing,
                delivered_before_kill=delivered_before_kill,
                queue_samples=queue_samples,
                hold_seconds=hold_seconds,
            )
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "armed_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(armed_at)
                    ),
                    "killed_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(killed_at)
                    ),
                    "hold_seconds": hold_seconds,
                    "requested_messages": count,
                    "peer_accepted_sends": accepted,
                    "queue_samples_during_hold": queue_samples,
                    "fresh_probe_phasing": fresh_probe_phasing,
                    "probe_wait_seconds": probe_wait_seconds,
                    "splice_log_tail": (
                        run(["docker", "logs", PROXY], timeout=60)[1][-1500:]
                        if container_running(PROXY)
                        else None
                    ),
                    "delivered_before_kill": delivered_before_kill,
                    "in_flight_at_kill": 1 if accepted > 0 else None,
                    "kill_to_arm_seconds": (
                        killed_at - armed_at if accepted > 0 else None
                    ),
                    "drain_report": report,
                    "preserved_after_restart": preserved_count,
                    "missing_nonce_indices": missing_indices(markers, mine),
                    "probe": send,
                    "verdict_rule": (
                        "killed-with-one-in-flight verdict: the splice "
                        "withholds replies from the first client->server "
                        "request after the arm, so the companion pops exactly "
                        "one message whose frame never reaches MEDRE; the "
                        "queue must hold still across the hold (longer than "
                        "two 2 s fetch windows) with nothing delivered, then "
                        "MEDRE is killed inside that state and restarted; "
                        "exactly the backlog's first message is missing, "
                        "every other accepted message is delivered exactly "
                        "once, nothing is preserved at the companion "
                        "afterwards, and zero duplicates occur"
                    ),
                    "note": (
                        "The fault MEDRE ran behind the fault splice so the "
                        "drain could be held with one message outstanding. "
                        "MEDRE's store survives the kill (its state volume "
                        "persists), so the restarted process re-drains what "
                        "the companion still holds. The duplicate count is "
                        "over distinct canonical events, because MEDRE's "
                        "meshcore adapter dedups inbound packets by native "
                        "message id."
                    ),
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")
        finally:
            # Leave the rig as the case found it: the splice and its MEDRE
            # instance go, the fault file clears, and the bridge MEDRE
            # (stopped at the start of the case) runs again.
            if FAULT_STATE_DIR.exists():
                write_fault(FAULT_STATE_DIR / "fault.json", {})
            self.remove_fault_medre()
            self.remove_splice()
            if (
                container_exists(MEDRE_BRIDGE)
                and not container_running(MEDRE_BRIDGE)
                and container_running(REPEATER_A)
            ):
                checked_docker("start", MEDRE_BRIDGE)

    def case_c6(self, count: int = 20) -> None:
        name = "c6-repeater-restart-persisted-queue"
        prefix = "p4c6"
        restore_bridge = False
        try:
            if not container_running(MEDRE_BRIDGE):
                self.not_run(name, "the MEDRE bridge container is not running")
                return
            restore_bridge = True
            checked_docker("stop", MEDRE_BRIDGE)
            pre = self.preconditions(
                "MEDRE stopped, so companion A has no client attached"
            )
            if not pre["medre_stopped"]:
                self.not_run(
                    name,
                    "MEDRE did not stop, so no backlog could form",
                    {"preconditions": pre},
                )
                return
            if not pre["companion_a_client_free"]:
                self.not_run(
                    name,
                    "companion A still had a client attached, so no backlog could form",
                    {"preconditions": pre},
                )
                return
            send = peer_send(self.profile, count, prefix, settle=3.0)
            mine = sent_markers(send)
            rows, _ = self.repeater_queue(REPEATER_A, prefix)
            persisted_before, indices_before = count_nonce_rows(rows, only=mine)
            restart_repeater(REPEATER_A)
            time.sleep(3)
            rows_after, _ = self.repeater_queue(REPEATER_A, prefix)
            persisted_after, indices_after = count_nonce_rows(rows_after, only=mine)
            checked_docker("start", MEDRE_BRIDGE)
            self.wait_for(lambda: container_running(MEDRE_BRIDGE), 90)
            self.wait_for(
                lambda: len(self.medre_events(prefix, only=mine)) >= persisted_after,
                timeout=180,
                interval=3.0,
            )
            events = self.medre_events(prefix, only=mine)
            markers = [
                marker
                for marker in (
                    nonce_marker(json.dumps(event, sort_keys=True)) for event in events
                )
                if marker is not None and marker in mine
            ]
            report = ordering_report(markers)
            failures: list[str] = []
            if (
                not mine
                or set(indices_before) != mine
                or set(indices_after) != mine
                or set(markers) != mine
            ):
                failures.append(
                    "the accepted marker set must survive restart and reach MEDRE"
                )
            if persisted_after != persisted_before:
                failures.append(
                    f"the persisted queue changed across the restart: "
                    f"{persisted_before} -> {persisted_after}"
                )
            if report["distinct"] != persisted_after:
                failures.append(
                    f"MEDRE recorded {report['distinct']} of the {persisted_after} persisted nonces"
                )
            if report["duplicate_arrivals"]:
                failures.append(
                    f"{report['duplicate_arrivals']} duplicate arrivals after the restart"
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
                    "persisted_before_restart": persisted_before,
                    "persisted_after_restart": persisted_after,
                    "drain_report": report,
                    "missing_nonce_indices": missing_indices(
                        markers, sent_markers(send)
                    ),
                    "probe": send,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")
        finally:
            if restore_bridge and not container_running(MEDRE_BRIDGE):
                try:
                    checked_docker("start", MEDRE_BRIDGE)
                except (RuntimeError, subprocess.SubprocessError) as exc:
                    self.not_run(name, f"could not restore the MEDRE bridge: {exc}")


CASES = {
    "c1": "case_c1",
    "c2": "case_c2",
    "c3": "case_c3",
    "c4": "case_c4",
    "c5": "case_c5",
    "c6": "case_c6",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir", default=str(LAB_ROOT / "lab/logs/openhop-backlog")
    )
    parser.add_argument("--profile", default="openhop-bridge")
    parser.add_argument("--case", default="all", choices=["all", *CASES])
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="regenerate the summary from the existing case files; run no case",
    )
    args = parser.parse_args(argv)

    matrix = Matrix(Path(args.evidence_dir), args.profile)
    if args.summary_only:
        summary = case_summary.build_summary(
            matrix.dir,
            prefix="c",
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
    # The summary is built from the EVIDENCE DIRECTORY, not from the cases
    # this invocation ran: re-running one case must not drop the other
    # cases from the summary.
    summary = case_summary.build_summary(
        matrix.dir,
        prefix="c",
        label="protocol-simulated",
        generated_at=utcnow(),
        profile=args.profile,
    )
    case_summary.write_summary(matrix.dir, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
