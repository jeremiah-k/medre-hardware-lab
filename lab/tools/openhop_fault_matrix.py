"""Lossy-link fault matrix: the MeshCore receive pump under a bad link.

WHAT THIS MEASURES
------------------
The MEDRE MeshCore session owns ONE connection-scoped receive pump: it wakes
on MESSAGES_WAITING and drains GET_MSG until NO_MORE_MSGS.  The question is
how the pump behaves when a companion answers slowly, not at all, or drops
the connection mid-fetch, and whether any wake is lost or duplicated.

The adapter's current design treats a fetch window that elapses with no
inbound traffic as a PARK signal: the node's fetch is a destructive pop,
so the window may have ended because it popped a message whose frame was
lost, and re-issuing would pop again into a link that is not returning
what it pops.  While the pump is parked, an idle-link liveness probe (one
cheap device query over the control connection) retires a silently dead
link, a connection that follows a failure-driven retirement probes the
link before its first fetch and fetches nothing while the probe goes
unanswered, and one ANSWERED probe re-arms exactly one fetch for a drain
an elapsed window interrupted.  The spec bounds the one link shape the
gate cannot close - a link that answers the probes and then swallows
message frames consumes at most one queued message per probe interval -
and the drain is stop-and-wait, so at most one popped message is ever in
flight.  The verdicts assert what the spec promises, and nothing more.

The fault is injected with a TRANSPARENT single-connection TCP splice
(lab/openhop-radio/openhop_fault_proxy.py) between MEDRE and companion A.
The splice forwards every byte in both directions unchanged and never
speaks the companion protocol, so it cannot issue a GET_MSG and cannot
stack one on another: the companion still sees exactly ONE client (the
spliced MEDRE session) at a time.

The matrix
----------
  latency_under    the companion's response is delayed BELOW the fetch
                   timeout (2.0 s): the pump must NOT churn.
  latency_over     the response is delayed ABOVE the fetch timeout: the
                   link answers slowly but answers.
  repeated_waiting several MESSAGES_WAITING notifications arrive in a row
                   while a backlog is waiting.
  reconnect        the link recovers and the client reconnects.
  reply_stall      TIME WINDOW: every server->client frame is swallowed
                   for the window's duration, on every connection inside
                   it (handshake replies and liveness-probe replies are
                   swallowed like any other frame).  The window then ends
                   and the queued messages must be delivered.
  mid_fetch_close  TIME WINDOW: the connection is closed the first time
                   the client puts a request in flight (on a live link the
                   next fetch or probe; on a fresh connection the
                   handshake), and the close re-arms with every connection
                   for the rest of the window.  The window then ends and
                   the queued messages must be delivered.
  saturation       a deep backlog waits behind a reply-stall window: the
                   designed probe retirements of a link that answers
                   nothing are expected, nothing may be consumed, and the
                   whole backlog must be delivered once the window ends.
  reply_stall_drain a TIME WINDOW armed while a backlog drain is in
                   flight: the frame the drain had on the wire when the
                   window opened is swallowed.  At most that one message
                   may be consumed; recovery follows the spec's branches -
                   a silence the connection survives resumes on the same
                   connection within one probe interval of the window's
                   end, a longer silence retires and recovery follows the
                   reconnect backoff plus one gated probe budget - and
                   every other accepted message is delivered once; zero
                   duplicates.
  selective_drop   the PER-CONNECTION worst case the spec bounds: a link
                   that answers the probes and swallows the message
                   frames.  Its verdict asserts the documented bound - at
                   most one consumed message per answered liveness probe
                   beyond the unproven first fetch, every other accepted
                   message delivered or preserved, zero duplicates - and
                   reports the consumed count.
  idle_blackhole   BOTH directions of an up, idle link are blackholed
                   (no close, armed by writing the splice's fault file):
                   the idle-link probe must retire the connection within
                   its measured window, reconnect attempts must continue
                   on backoff without succeeding while the fault holds,
                   and the message queued during the fault must be
                   delivered once after the link returns.

WHAT IT COUNTS
---------------
  reconnect_churn   MEDRE's own "reconnect attempt N" log lines
  lost_wakes        nonces persisted at the companion that never reached
                    MEDRE's store
  duplicate_wakes   nonces that reached MEDRE's store more than once
  consumed_wakes    nonces that are in neither store (bounded per the
                    verdict that judges the case)

The matrix needs the bridge rig up (the relay, both repeaters, the MeshC
nodes) and the fault MEDRE (lab/config/medre-openhop-fault.yaml) stopped
against the direct companion, because the companion serves one client at a
time.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

# The summary aggregator is a SIBLING module: it is imported by path so
# the driver runs from any working directory.
_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import openhop_case_summary as case_summary  # noqa: E402

LAB_ROOT = Path(__file__).resolve().parents[2]
TOOLS = LAB_ROOT / "lab" / "tools"
STATE_TOOL = TOOLS / "openhop_store_snapshot.py"
PEER_PROBE = TOOLS / "openhop_peer_probe.py"

PROXY = "medre-lab-openhop-fault-proxy"
REPEATER_A = "medre-lab-openhop-repeater-a"
MEDRE_BRIDGE = "medre-lab-medre-openhop-bridge"
MEDRE_FAULT = "medre-lab-medre-openhop-fault"

RECONNECT = re.compile(r"reconnect attempt (\d+)", re.IGNORECASE)
# The full marker is p4<case>-<index>-<unique>: the unique tail is what
# makes one message one message, so a message is counted by its marker and
# not by its index (the index alone repeats every run).
NONCE_INDEX = re.compile(r"p4(?P<case>[a-z0-9]+)-(?P<index>\d+)-(?P<unique>[0-9a-f]+)")


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def run(cmd: list[str], timeout: int = 600) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def container_running(name: str) -> bool:
    rc, out, _ = run(["docker", "inspect", "-f", "{{.State.Running}}", name])
    return rc == 0 and out.strip() == "true"


def container_logs(name: str) -> str:
    _, out, err = run(["docker", "logs", name], timeout=120)
    return out + err


def timestamped_logs(name: str) -> str:
    """Return the container's logs with one RFC3339 timestamp per line.

    The idle-blackhole verdict measures WHEN the pump retired relative to
    the moment the fault was armed, so its lines need timestamps; docker's
    -t flag prefixes each line with the host's UTC time.
    """
    _, out, err = run(["docker", "logs", "-t", name], timeout=120)
    return out + err


LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T[^ ]+) ")


def line_time(line: str) -> float | None:
    """Return the line's docker -t timestamp as epoch seconds, or None."""
    match = LOG_TIME.match(line.strip())
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# The idle-link probe's retirement line: the pump names the probe as the
# failure reason and retires through its normal retire path.
# The probe's failure reason carries nested parentheses in the wild
# ("probe unanswered (no_event_received)", "probe timed out"), so the
# reason is matched loosely and the line is anchored by its fixed ends.
PROBE_RETIREMENT = re.compile(
    r"receive pump failed \(RuntimeError: idle-link liveness probe.*\); "
    r"retiring connection",
    re.IGNORECASE,
)

# The parked pump's probe reply lines: the adapter logs one DEBUG line per
# answered liveness probe ("idle-link probe answered ..."), and the
# selective-drop bound is counted in answered probes.
ANSWERED_PROBE = re.compile(r"idle-link probe answered", re.IGNORECASE)


def nonce_index(text: str) -> int | None:
    match = NONCE_INDEX.search(str(text or ""))
    return int(match.group("index")) if match else None


def nonce_marker(text: str) -> str | None:
    """Return the FULL nonce marker, or None.

    The marker is ``p4<case>-<index>-<unique>``: the unique tail is what
    makes one message one message, so a message is counted by its marker and
    not by its index.
    """
    match = NONCE_INDEX.search(str(text or ""))
    return match.group(0) if match else None


def sent_markers(probe_result: dict) -> set[str]:
    """Return the markers THIS run sent.

    MEDRE's store is cumulative, so an unscoped read would also count an
    earlier run's events as duplicates of this one's.
    """
    markers: set[str] = set()
    for nonce in probe_result.get("nonces") or []:
        marker = nonce_marker(nonce)
        if marker:
            markers.add(marker)
    return markers


def state_in(container: str, mode: str, needle: str = "") -> dict:
    run(
        [
            "docker",
            "cp",
            str(STATE_TOOL),
            f"{container}:/tmp/openhop_store_snapshot.py",
        ]
    )
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
    rc, out, err = run(cmd, timeout=300)
    try:
        return json.loads(out)
    except ValueError:
        return {"error": f"state read failed (rc={rc}): {err[-400:]}"}


def count_reconnects(log: str) -> dict:
    """Count MEDRE's own reconnect attempts and how often it succeeded."""
    attempts = [int(match.group(1)) for match in RECONNECT.finditer(log)]
    successes = len(re.findall(r"reconnected successfully", log, re.IGNORECASE))
    # The candidate wording was "retiring ambiguous connection"; the
    # idle-window fix shortened it to "retiring connection". Match both.
    retired = len(re.findall(r"retiring (ambiguous )?connection", log, re.IGNORECASE))
    return {
        "reconnect_attempts": len(attempts),
        "highest_attempt_number": max(attempts) if attempts else 0,
        "reconnect_successes": successes,
        "connections_retired_on_fetch_failure": retired,
    }


def fault_case_failures(
    *,
    accepted: int,
    delivered_distinct: int,
    duplicate_wakes: int,
    preserved_distinct: int,
    retirements: int,
    reconnect_attempts: int,
    expect_recovery: bool,
) -> list[str]:
    """Judge one measured fault case by the park design contract.

    The park design treats an elapsed fetch window with no inbound traffic
    as a park signal, not a retirement, so a stall case producing no
    reconnect attempt is the DESIGN behaviour and must not fail the case.
    What the design promises, and what this verdict asserts:

    * no duplicates: a wake that reached MEDRE's store twice fails the case;
    * every accepted message is either delivered or still preserved at the
      companion (a bounded-window accounting, not an "exactly once" claim);
    * zero retirements for idle windows: no case may retire a connection
      over a timed-out fetch;
    * recovery on a healthy link: the mild-fault cases must deliver every
      accepted message and must not churn.
    """
    failures: list[str] = []
    if duplicate_wakes:
        failures.append(f"{duplicate_wakes} duplicate wake(s) reached MEDRE's store")
    if accepted > 0 and delivered_distinct + preserved_distinct != accepted:
        failures.append(
            f"{delivered_distinct} delivered + {preserved_distinct} preserved at "
            f"the companion != {accepted} accepted messages"
        )
    if retirements:
        failures.append(
            f"{retirements} connection(s) were retired over idle fetch windows"
        )
    if expect_recovery:
        if accepted > 0 and delivered_distinct != accepted:
            failures.append(
                f"the link stayed healthy but only {delivered_distinct} of "
                f"{accepted} accepted messages were delivered"
            )
        if reconnect_attempts:
            failures.append(
                f"a healthy link still caused {reconnect_attempts} reconnect attempts"
            )
    return failures


def idle_blackhole_failures(
    *,
    retirements: int,
    probe_retirements: int,
    retire_seconds_after_arm: float | None,
    reconnect_attempts_while_armed: int,
    reconnect_successes_while_armed: int,
    duplicate_wakes: int,
    accepted: int,
    delivered_distinct: int,
    reconnect_seconds_after_restore: float | None,
) -> list[str]:
    """Judge the silent dead-link case by ITS design contract.

    This case is the opposite of the stall cases: retiring the connection
    IS the designed outcome here, so ``retirements == 1`` is REQUIRED (the
    stall-case verdict, which fails any retirement, must not be reused).
    What the idle-link liveness probe promises, and what this verdict
    asserts:

    * exactly one retirement, and it names the idle-link probe as the
      reason - not a fetch timeout, not a clean close;
    * the retirement lands within the measured detection window (park
      already in place: up to one 15 s probe interval to the first probe
      plus the probe's own timeout, so roughly 15-45 s from the arm);
    * reconnect attempts continue on the backoff while the fault holds
      and NONE of them succeeds (the handshake is blackholed too);
    * zero duplicates anywhere in the run;
    * after the splice passes traffic again, the client reconnects
      through the reconnect backoff (from its base doubling to its cap
      under bounded jitter, one to thirty seconds on TCP) and every
      message queued during the fault is delivered once; the case's own
      measured bound for that reconnect is 45 s.
    """
    failures: list[str] = []
    if retirements != 1 or probe_retirements != 1:
        failures.append(
            f"expected exactly one idle-link probe retirement, measured "
            f"{probe_retirements} probe retirement(s) of {retirements} "
            "retirement(s) total"
        )
    if retire_seconds_after_arm is None:
        failures.append("the retirement line carried no parsable timestamp")
    elif not 10.0 <= retire_seconds_after_arm <= 45.0:
        failures.append(
            f"the probe retirement landed {retire_seconds_after_arm:.1f} s "
            "after the fault was armed; the measured window is roughly "
            "15-45 s (one probe interval to the first probe plus the "
            "probe timeout)"
        )
    if reconnect_attempts_while_armed < 1:
        failures.append("no reconnect attempt was recorded while the fault held")
    if reconnect_successes_while_armed:
        failures.append(
            f"{reconnect_successes_while_armed} reconnect(s) succeeded while "
            "both directions were still blackholed"
        )
    if duplicate_wakes:
        failures.append(f"{duplicate_wakes} duplicate wake(s) reached MEDRE's store")
    if accepted > 0 and delivered_distinct != accepted:
        failures.append(
            f"{delivered_distinct} of {accepted} message(s) queued during the "
            "fault were delivered after the link returned"
        )
    if reconnect_seconds_after_restore is None:
        failures.append("the client did not reconnect after the fault cleared")
    elif reconnect_seconds_after_restore > 45.0:
        failures.append(
            f"the post-fault reconnect took {reconnect_seconds_after_restore:.1f} s"
        )
    return failures


def window_stall_failures(
    *,
    accepted: int,
    delivered_distinct: int,
    preserved_distinct: int,
    duplicate_wakes: int,
    reconnect_attempts_while_armed: int,
    reconnect_successes_while_armed: int,
    reconnect_seconds_after_restore: float | None,
) -> list[str]:
    """Judge a time-window stall case by the spec's promises.

    The window faults are armed at a point in time and persist across
    every connection inside the window: the handshake and the
    liveness probe of a reconnect inside the window are swallowed or
    closed like any other frame, so nothing can be fetched successfully
    while the fault holds.  What the spec promises, and what this verdict
    asserts:

    * no duplicates: a wake that reached MEDRE's store twice fails the
      case;
    * nothing consumed: every accepted message is either delivered or
      still preserved at the companion - a message in neither store was
      consumed into a link that could not return it, and fails the case;
    * a probe retirement of a link that answers nothing is the DESIGNED
      outcome (it is reported, never counted against the case);
    * reconnect attempts continue on backoff while the window holds and
      none of them succeeds (the handshake is faulted too);
    * when the window ends the client reconnects and every queued
      message is delivered: the full backlog arriving after the window
      is the designed recovery, and a message still only preserved at
      the companion after recovery is a shortfall the case reports.
      Recovery timing follows the spec's two branches: a silence the
      connection survives resumes the drain on the same connection
      within one probe interval of the park plus one probe budget for
      the answer; a longer silence retires the connection, and the
      reconnect backoff then runs from its base doubling to its cap
      under bounded jitter (one to thirty seconds on TCP and serial),
      plus one probe budget for a gated reconnect.  The case's own
      measured bound for the post-window reconnect is 45 s.
    """
    failures: list[str] = []
    if duplicate_wakes:
        failures.append(f"{duplicate_wakes} duplicate wake(s) reached MEDRE's store")
    if accepted > 0 and delivered_distinct + preserved_distinct != accepted:
        failures.append(
            f"{delivered_distinct} delivered + {preserved_distinct} preserved at "
            f"the companion != {accepted} accepted messages: "
            f"{accepted - delivered_distinct - preserved_distinct} consumed "
            "while the fault held"
        )
    if reconnect_attempts_while_armed < 1:
        failures.append("no reconnect attempt was recorded while the fault held")
    if reconnect_successes_while_armed:
        failures.append(
            f"{reconnect_successes_while_armed} reconnect(s) succeeded while "
            "the fault window still held"
        )
    if accepted > 0 and delivered_distinct != accepted:
        failures.append(
            f"after the window ended only {delivered_distinct} of "
            f"{accepted} queued message(s) were delivered"
            + (
                f" ({preserved_distinct} still preserved at the companion)"
                if preserved_distinct
                else ""
            )
        )
    if reconnect_seconds_after_restore is None:
        failures.append("the client did not reconnect after the window ended")
    elif reconnect_seconds_after_restore > 45.0:
        failures.append(
            f"the post-window reconnect took "
            f"{reconnect_seconds_after_restore:.1f} s"
        )
    return failures


def selective_drop_failures(
    *,
    accepted: int,
    delivered_distinct: int,
    preserved_distinct: int,
    duplicate_wakes: int,
    answered_probes: int,
) -> list[str]:
    """Judge the per-connection worst case by the spec's documented bound.

    A link that answers the probes and swallows the message frames cannot
    be told from a healthy link by any exchange short of popping a
    message: the probe reply genuinely arrives, so the fetch runs and the
    fetched frame is swallowed.  A fetch window that elapses with no
    inbound traffic parks the pump at once, so each answered probe earns
    exactly one more fetch - the spec bounds the damage at one queued
    message per probe interval, plus the one unproven fetch a fresh
    connection may spend before its first probe - and this verdict
    asserts that bound instead of zero loss:

    * zero duplicates anywhere in the run;
    * the consumed count (accepted, less what is delivered or still
      preserved at the companion) must stay within one message per
      answered liveness probe beyond the unproven first fetch, and is
      reported per run;
    * every other accepted message is delivered or still preserved.
    """
    consumed = accepted - delivered_distinct - preserved_distinct
    bound = 1 + max(0, answered_probes)
    failures: list[str] = []
    if duplicate_wakes:
        failures.append(f"{duplicate_wakes} duplicate wake(s) reached MEDRE's store")
    if consumed < 0:
        failures.append(
            f"{delivered_distinct} delivered + {preserved_distinct} preserved "
            f"exceeds the {accepted} accepted messages"
        )
    elif consumed > bound:
        failures.append(
            f"{consumed} message(s) were consumed into the selective drop, "
            f"more than the documented bound of one per answered probe "
            f"beyond the unproven first fetch "
            f"({answered_probes} answered probe(s), bound {bound})"
        )
    return failures


def drain_stall_failures(
    *,
    accepted: int,
    delivered_distinct: int,
    preserved_distinct: int,
    duplicate_wakes: int,
    reconnect_successes_while_armed: int,
    retired_during_window: bool,
    reconnected_after_window: bool,
    resume_seconds_after_window: float | None,
) -> list[str]:
    """Judge a reply-stall window armed during a backlog drain.

    The window opens while the drain has a fetched frame on the wire: the
    node has popped that message and the splice swallows its frame, so
    that one message may be consumed - the fetch window it belonged to
    elapses with no inbound traffic and the pump parks instead of
    re-issuing into the stalled link.  What the spec promises, and what
    this verdict asserts:

    * at most ONE message is consumed (the in-flight frame's);
    * zero duplicates anywhere in the run;
    * every other accepted message is delivered once: the drain resumes
      and accounts for all of them, so nothing is still queued at the
      companion afterwards;
    * no reconnect succeeds while the window holds (the handshake reply
      is swallowed like any other frame);
    * recovery follows the spec's two branches: a silence the connection
      survives resumes the interrupted drain on the same connection
      within one probe interval of the window's end; a longer silence
      retires the connection, and the reconnect backoff then runs from
      its base doubling to its cap under bounded jitter (one to thirty
      seconds on TCP), plus one probe budget for a gated reconnect.  The
      case's own measured bound after the window ends is 45 s on the
      retired branch and one probe interval (15 s) on the surviving
      branch, and the branch taken is recorded in the evidence.
    """
    consumed = accepted - delivered_distinct - preserved_distinct
    failures: list[str] = []
    if duplicate_wakes:
        failures.append(f"{duplicate_wakes} duplicate wake(s) reached MEDRE's store")
    if consumed > 1:
        failures.append(
            f"{consumed} message(s) were consumed into the stall, more than "
            "the one in-flight frame the window could swallow"
        )
    if delivered_distinct + preserved_distinct > accepted:
        failures.append(
            f"{delivered_distinct} delivered + {preserved_distinct} preserved "
            f"exceeds the {accepted} accepted messages"
        )
    if preserved_distinct:
        failures.append(
            f"{preserved_distinct} message(s) were still queued at the "
            "companion after the drain should have resumed"
        )
    if reconnect_successes_while_armed:
        failures.append(
            f"{reconnect_successes_while_armed} reconnect(s) succeeded while "
            "the stall window still held"
        )
    if retired_during_window:
        if not reconnected_after_window:
            failures.append(
                "the connection retired inside the window and never "
                "reconnected after it ended"
            )
        elif (
            resume_seconds_after_window is not None
            and resume_seconds_after_window > 45.0
        ):
            failures.append(
                f"the drain resumed {resume_seconds_after_window:.1f} s after "
                "the window ended, later than the reconnect backoff plus one "
                "gated probe budget (the case's measured bound is 45 s)"
            )
    return failures


class FaultMatrix:
    def __init__(self, evidence_dir: Path) -> None:
        self.dir = evidence_dir
        self.dir.mkdir(parents=True, exist_ok=True)
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

    # -- rig control ----------------------------------------------------
    def start_proxy(self, fault: dict) -> None:
        """Start the splice with THIS case's fault.

        The fault arguments are written to a file that the proxy reads at
        startup, and the proxy is started by COMPOSE UP: a compose RUN
        would not give the container the service's network alias, and the
        MEDRE client could then not resolve the proxy's name.
        """
        state = LAB_ROOT / "lab/state/openhop-fault"
        state.mkdir(parents=True, exist_ok=True)
        (state / "fault.json").write_text(
            json.dumps(fault, sort_keys=True) + "\n", encoding="utf-8"
        )
        run(["docker", "rm", "-f", PROXY], timeout=120)
        # The proxy is started with DOCKER RUN and an explicit network alias
        # rather than compose: the client resolves the proxy's NAME once, at
        # connect time, so the alias has to be on the network the client is
        # on.  A compose service and a compose RUN cannot both own the same
        # container name, and the run variant does not carry the alias.
        cmd = [
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
            "1000:1000",
            "-v",
            f"{LAB_ROOT / 'lab/openhop-radio/openhop_fault_proxy.py'}:"
            "/opt/openhop-fault/openhop_fault_proxy.py:ro",
            "-v",
            f"{state}:/opt/openhop-fault:rw",
            "medre-lab/openhop-radio-relay:latest",
            "python3",
            "/opt/openhop-fault/openhop_fault_proxy.py",
            "--listen-host",
            "0.0.0.0",  # nosec B104 # The splice serves the private lab Docker network only; the compose file publishes no host port.
            "--listen-port",
            "5050",
            "--upstream-host",
            "openhop-repeater-a",
            "--upstream-port",
            "5000",
            "--fault-file",
            "/opt/openhop-fault/fault.json",
        ]
        run(cmd, timeout=300)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if container_running(PROXY):
                time.sleep(1)
                return
            time.sleep(1)

    def wait_for(self, predicate, timeout: float, interval: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def stop_fault_medre(self) -> None:
        run(["docker", "rm", "-f", MEDRE_FAULT], timeout=180)

    def stop_bridge_medre(self) -> None:
        run(["docker", "stop", MEDRE_BRIDGE], timeout=180)

    def start_fault_medre(self) -> bool:
        # The fault MEDRE mounts lab/state/config, so its config template
        # has to be generated there first (the bridge bring-up generates the
        # bridge one, not this one).
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
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if container_running(MEDRE_FAULT):
                return True
            time.sleep(2)
        return False

    def peer_send(self, count: int, prefix: str) -> dict:
        cmd = [
            "docker",
            "compose",
            "--profile",
            "openhop-bridge",
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
            "openhop-repeater-b",
            "--port-b",
            "5000",
            "--count",
            str(count),
            "--prefix",
            prefix,
            "--settle",
            "4",
        ]
        rc, out, err = run(cmd, timeout=1800)
        text = out.strip()
        start = text.find("{")
        if start < 0:
            return {
                "pass": False,
                "error": "the probe produced no JSON",
                "exit_status": rc,
            }
        try:
            data = json.loads(text[start:])
        except ValueError:
            return {
                "pass": False,
                "error": "the probe JSON did not parse",
                "exit_status": rc,
            }
        data["exit_status"] = rc
        return data

    # -- cases -----------------------------------------------------------
    def run_case(
        self,
        name: str,
        fault: dict,
        count: int,
        prefix: str,
        settle: float,
        expect_recovery: bool,
        verdict: str = "mild",
    ) -> None:
        """Run one fault case end to end.

        The peer sends its nonces while the fault is ACTIVE, so the
        companion's answer to the client travels through the splice.
        ``expect_recovery`` marks the MILD-fault cases (the response delay
        stays below the fetch timeout, so the link never breaks): they must
        deliver every accepted message without churn.  ``verdict`` picks
        the judge: "mild" for the healthy-link cases, "bounded" for the
        per-connection worst case whose spec bound (at most one consumed
        message per reconnect attempt) is asserted instead of zero loss.
        """
        try:
            self.stop_bridge_medre()
            self.stop_fault_medre()
            self.start_proxy(fault)
            # The client resolves the proxy's NAME once, at connect time, so
            # the proxy must be up and resolvable before the client starts.
            if not self.wait_for(lambda: container_running(PROXY), 60, interval=1.0):
                self.not_run(name, "the fault proxy did not start")
                return
            if not self.start_fault_medre():
                self.not_run(name, "the fault MEDRE container did not start")
                return
            time.sleep(settle)
            send = self.peer_send(count, prefix)
            time.sleep(settle)
            log = container_logs(MEDRE_FAULT)
            churn = count_reconnects(log)
            mine = sent_markers(send)
            events = (
                state_in(MEDRE_FAULT, "medre", prefix).get("canonical_events") or []
            )
            indices = [
                marker
                for marker in (
                    nonce_marker(json.dumps(event, sort_keys=True)) for event in events
                )
                if marker is not None and marker in mine
            ]
            distinct = len(set(indices))
            duplicates = len(indices) - distinct
            persisted = (
                state_in(REPEATER_A, "repeater", prefix).get("companion_messages") or []
            )
            persisted_indices = [
                marker
                for marker in (nonce_marker(row.get("text") or "") for row in persisted)
                if marker is not None and marker in mine
            ]
            preserved = len(set(persisted_indices))
            if verdict == "bounded":
                answered_probes = len(ANSWERED_PROBE.findall(log))
                failures = selective_drop_failures(
                    accepted=int(send.get("accepted_count") or 0),
                    delivered_distinct=distinct,
                    preserved_distinct=preserved,
                    duplicate_wakes=duplicates,
                    answered_probes=answered_probes,
                )
                verdict_rule = (
                    "selective-drop verdict (the documented worst case): a link "
                    "that answers the probes and swallows the message frames "
                    "consumes at most one queued message per answered liveness "
                    "probe beyond the unproven first fetch; the consumed count "
                    "is reported per run; every other accepted message is "
                    "delivered or still preserved at the companion; zero "
                    "duplicates"
                )
            else:
                failures = fault_case_failures(
                    accepted=int(send.get("accepted_count") or 0),
                    delivered_distinct=distinct,
                    duplicate_wakes=duplicates,
                    preserved_distinct=preserved,
                    retirements=churn["connections_retired_on_fetch_failure"],
                    reconnect_attempts=churn["reconnect_attempts"],
                    expect_recovery=expect_recovery,
                )
                verdict_rule = (
                    "healthy-link verdict: no duplicate wakes; every accepted "
                    "message delivered or still preserved at the companion; "
                    "zero retirements for idle fetch windows; the mild-fault "
                    "cases must deliver everything without churn"
                )
            consumed = max(
                0, int(send.get("accepted_count") or 0) - distinct - preserved
            )
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "fault": fault,
                    "verdict_rule": verdict_rule,
                    "requested_messages": count,
                    "peer_accepted_sends": send.get("accepted_count"),
                    "reconnect_churn": churn,
                    "answered_probes": (
                        len(ANSWERED_PROBE.findall(log))
                        if verdict == "bounded"
                        else None
                    ),
                    "arrivals_at_medre": len(indices),
                    "distinct_at_medre": distinct,
                    "duplicate_wakes": duplicates,
                    "persisted_at_companion": len(persisted_indices),
                    "distinct_persisted": preserved,
                    "consumed_wakes": consumed,
                    "lost_wakes": max(0, preserved - distinct),
                    "probe": send,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")

    # -- the time-window stall cases --------------------------------------
    def run_window_case(
        self,
        name: str,
        window_fault: dict,
        window_seconds: float,
        count: int,
        prefix: str,
        window_note: str,
    ) -> None:
        """Run one time-window stall case end to end.

        Phase 1: the splice starts with an empty fault and the fault MEDRE
        connects and drains; the run then waits out an idle window so the
        pump is parked.  Phase 2 (T0): the window fault is armed by writing
        its deadline into the splice's fault file, and the peer queues
        messages at the companion while the fault holds.  The fault is
        PERSISTENT across connections: the handshake and the liveness
        probe of any reconnect inside the window are faulted like any
        other frame.  Phase 3 (T1): the deadline passes and the case
        waits for the reconnect and the delivery of what was queued.
        """
        try:
            self.stop_bridge_medre()
            self.stop_fault_medre()
            self.start_proxy({})
            if not self.wait_for(lambda: container_running(PROXY), 60, interval=1.0):
                self.not_run(name, "the fault splice did not start")
                return
            if not self.start_fault_medre():
                self.not_run(name, "the fault MEDRE container did not start")
                return
            # A clean first connect logs "started (mode=tcp, connected=True)";
            # "reconnected successfully" only follows a disconnect-reconnect.
            first_connect = re.compile(
                r"started \(mode=tcp, connected=True\)|reconnected successfully",
                re.IGNORECASE,
            )
            if not self.wait_for(
                lambda: first_connect.search(container_logs(MEDRE_FAULT)) is not None,
                120,
                interval=2.0,
            ):
                self.not_run(name, "the fault MEDRE never completed its first connect")
                return
            # Let the drain rounds finish and the pump park (~10 s of
            # silence after the last round), plus a margin.
            time.sleep(25)
            state_dir = LAB_ROOT / "lab/state/openhop-fault"
            armed_at = time.time()
            # The window keys carry unix deadlines: the fault is armed now
            # and persists until the deadline passes, across every
            # connection established inside the window.
            fault = {key: armed_at + window_seconds for key in window_fault}
            (state_dir / "fault.json").write_text(
                json.dumps(fault, sort_keys=True) + "\n", encoding="utf-8"
            )
            send = self.peer_send(count, prefix)
            # The splice itself stops faulting the moment the deadline
            # passes (it re-reads the file per chunk), so the deadline is
            # the authoritative window end even if queueing overran it.
            window_end = armed_at + window_seconds
            if time.time() < window_end:
                time.sleep(window_end - time.time())
            (state_dir / "fault.json").write_text("{}\n", encoding="utf-8")
            self.wait_for(
                lambda: (
                    "reconnected successfully"
                    in container_logs(MEDRE_FAULT)[
                        container_logs(MEDRE_FAULT).rfind("retiring connection") :
                    ]
                    if "retiring connection" in container_logs(MEDRE_FAULT)
                    else False
                ),
                120,
                interval=2.0,
            )
            # The drain after the reconnect: a 120-message backlog drains
            # in ~4 s on this rig, so a generous margin is plenty.
            time.sleep(30)
            log = timestamped_logs(MEDRE_FAULT)
            churn = count_reconnects(log)
            mine = sent_markers(send)
            events = (
                state_in(MEDRE_FAULT, "medre", prefix).get("canonical_events") or []
            )
            arrivals = [
                marker
                for marker in (
                    nonce_marker(json.dumps(event, sort_keys=True)) for event in events
                )
                if marker is not None and marker in mine
            ]
            distinct = len(set(arrivals))
            duplicates = len(arrivals) - distinct
            persisted = (
                state_in(REPEATER_A, "repeater", prefix).get("companion_messages") or []
            )
            persisted_indices = [
                marker
                for marker in (nonce_marker(row.get("text") or "") for row in persisted)
                if marker is not None and marker in mine
            ]
            preserved = len(set(persisted_indices))

            # Attempts between the arm and the window's end: the log is
            # ordered, so split the reconnect attempts by their
            # timestamps against the DEADLINE, which is when the splice
            # stopped faulting.
            attempts_while_armed = 0
            successes_while_armed = 0
            success_after = None
            for line in log.splitlines():
                stamp = line_time(line)
                if stamp is None:
                    continue
                if RECONNECT.search(line) and armed_at <= stamp <= window_end:
                    attempts_while_armed += 1
                if "reconnected successfully" in line:
                    if stamp <= window_end:
                        successes_while_armed += 1
                    elif success_after is None:
                        success_after = stamp
            reconnect_seconds = (
                success_after - window_end if success_after is not None else None
            )
            accepted = int(send.get("accepted_count") or 0)
            failures = window_stall_failures(
                accepted=accepted,
                delivered_distinct=distinct,
                preserved_distinct=preserved,
                duplicate_wakes=duplicates,
                reconnect_attempts_while_armed=attempts_while_armed,
                reconnect_successes_while_armed=successes_while_armed,
                reconnect_seconds_after_restore=reconnect_seconds,
            )
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "fault": {
                        "keys": sorted(window_fault),
                        "window_seconds": window_seconds,
                        "persistent_across_connections": True,
                        "note": window_note,
                    },
                    "verdict_rule": (
                        "time-window stall verdict: no duplicates; nothing "
                        "consumed - every accepted message is delivered or "
                        "still preserved at the companion, and one in neither "
                        "store fails the case; a probe retirement of a link "
                        "that answers nothing is the designed outcome and is "
                        "reported, not counted against the case; reconnect "
                        "attempts continue on backoff and none succeeds while "
                        "the window holds; when the window ends the client "
                        "reconnects and every queued message is delivered, "
                        "recovery timing following the spec's branches (a "
                        "survived silence resumes within one probe interval; "
                        "a longer silence retires and recovery follows the "
                        "reconnect backoff - one to thirty seconds on TCP "
                        "under bounded jitter - plus one gated probe budget; "
                        "the case's measured bound is 45 s)"
                    ),
                    "requested_messages": count,
                    "peer_accepted_sends": accepted,
                    "window_seconds": window_seconds,
                    "reconnect_churn": churn,
                    "probe_retirements": len(PROBE_RETIREMENT.findall(log)),
                    "reconnect_attempts_while_armed": attempts_while_armed,
                    "reconnect_successes_while_armed": successes_while_armed,
                    "reconnect_seconds_after_restore": reconnect_seconds,
                    "arrivals_at_medre": len(arrivals),
                    "distinct_at_medre": distinct,
                    "duplicate_wakes": duplicates,
                    "distinct_persisted": preserved,
                    "consumed_wakes": max(0, accepted - distinct - preserved),
                    "probe": send,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")

    def case_reply_stall(self) -> None:
        """Swallow every server->client frame for a window, on every
        connection inside it, then deliver the backlog."""
        self.run_window_case(
            "fault-reply-stall",
            {"stall_server_to_client_until": True},
            45.0,
            6,
            "p4qsr",
            "45 s reply-stall window: server->client swallowed",
        )

    def case_mid_fetch_close(self) -> None:
        """Close the connection the first time a request is in flight, for
        a window, then deliver the backlog."""
        self.run_window_case(
            "fault-mid-fetch-close",
            {"close_on_request_until": True},
            45.0,
            6,
            "p4qsc",
            "45 s mid-fetch-close window: connection closed on the first "
            "in-flight request, re-armed per connection",
        )

    def case_saturation(self) -> None:
        """A deep backlog waits behind a reply-stall window; the whole
        backlog must come across once the window ends."""
        self.run_window_case(
            "fault-saturation",
            {"stall_server_to_client_until": True},
            60.0,
            60,
            "p4qs",
            "60 s reply-stall window under a 60-message backlog",
        )

    def case_reply_stall_during_drain(self) -> None:
        """Arm a reply-stall window while a backlog drain is in flight.

        The peer queues a deep backlog and the case watches MEDRE's store:
        the moment the first delivery is observed the drain is pulling
        frames across the splice, and the window is armed, so the frame
        the drain has on the wire is the one the splice swallows.
        The window holds 45 s across every connection inside it, then
        ends and the case waits for the reconnect and the delivery of
        everything that was queued.
        """
        name = "fault-reply-stall-drain"
        # The prefix is unique per run: the arm trigger and the delivery
        # accounting must only see THIS run's messages, never the stale
        # rows an earlier run left in the companion or MEDRE stores.
        prefix = f"p4qsd{uuid.uuid4().hex[:8]}"
        count = 60
        window_seconds = 45.0
        state_dir = LAB_ROOT / "lab/state/openhop-fault"
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.stop_bridge_medre()
            self.stop_fault_medre()
            self.start_proxy({})
            if not self.wait_for(lambda: container_running(PROXY), 60, interval=1.0):
                self.not_run(name, "the fault splice did not start")
                return
            if not self.start_fault_medre():
                self.not_run(name, "the fault MEDRE container did not start")
                return
            # A clean first connect logs "started (mode=tcp, connected=True)";
            # "reconnected successfully" only follows a disconnect-reconnect.
            first_connect = re.compile(
                r"started \(mode=tcp, connected=True\)|reconnected successfully",
                re.IGNORECASE,
            )
            if not self.wait_for(
                lambda: first_connect.search(container_logs(MEDRE_FAULT)) is not None,
                120,
                interval=2.0,
            ):
                self.not_run(name, "the fault MEDRE never completed its first connect")
                return
            # Let the start-up drain finish and the pump park before the
            # backlog starts arriving.
            time.sleep(25)
            send: dict = {}
            sending = threading.Event()

            def queue_backlog() -> None:
                sending.set()
                send.update(self.peer_send(count, prefix))

            sender = threading.Thread(target=queue_backlog, daemon=True)
            sender.start()
            # Arm at the first OBSERVED delivery: the drain is pulling
            # frames across the splice right now, so the next frame it
            # puts on the wire is the one the window swallows.
            armed = False
            armed_at = 0.0
            window_end = 0.0
            delivered_before_arm = 0
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline and not armed:
                delivered = len(
                    state_in(MEDRE_FAULT, "medre", prefix).get("canonical_events") or []
                )
                if delivered > 0:
                    armed_at = time.time()
                    window_end = armed_at + window_seconds
                    (state_dir / "fault.json").write_text(
                        json.dumps(
                            {
                                "stall_server_to_client_until": window_end,
                            },
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    delivered_before_arm = delivered
                    armed = True
                    break
                time.sleep(1.0)
            if not armed:
                self.not_run(
                    name,
                    "no backlog delivery was observed at MEDRE; nothing to stall",
                )
                return
            # The rest of the backlog queues while the fault holds.
            sender.join(timeout=600)
            if time.time() < window_end:
                time.sleep(window_end - time.time())
            (state_dir / "fault.json").write_text("{}\n", encoding="utf-8")
            self.wait_for(
                lambda: (
                    "reconnected successfully"
                    in container_logs(MEDRE_FAULT)[
                        container_logs(MEDRE_FAULT).rfind("retiring connection") :
                    ]
                    if "retiring connection" in container_logs(MEDRE_FAULT)
                    else False
                ),
                120,
                interval=2.0,
            )
            # The resumed drain: a 60-message backlog drains in ~2 s on
            # this rig, so a generous margin is plenty.
            time.sleep(45)
            log = timestamped_logs(MEDRE_FAULT)
            churn = count_reconnects(log)
            mine = sent_markers(send)
            events = (
                state_in(MEDRE_FAULT, "medre", prefix).get("canonical_events") or []
            )
            arrivals = [
                marker
                for marker in (
                    nonce_marker(json.dumps(event, sort_keys=True)) for event in events
                )
                if marker is not None and marker in mine
            ]
            distinct = len(set(arrivals))
            duplicates = len(arrivals) - distinct
            persisted = (
                state_in(REPEATER_A, "repeater", prefix).get("companion_messages") or []
            )
            persisted_indices = [
                marker
                for marker in (nonce_marker(row.get("text") or "") for row in persisted)
                if marker is not None and marker in mine
            ]
            preserved = len(set(persisted_indices))
            accepted = int(send.get("accepted_count") or 0)
            attempts_while_armed = 0
            successes_while_armed = 0
            retired_during_window = False
            resume_seconds = None
            for line in log.splitlines():
                stamp = line_time(line)
                if stamp is None:
                    continue
                if RECONNECT.search(line) and armed_at <= stamp <= window_end:
                    attempts_while_armed += 1
                if "reconnected successfully" in line:
                    if stamp <= window_end:
                        successes_while_armed += 1
                    elif resume_seconds is None:
                        resume_seconds = stamp - window_end
                if "retiring connection" in line and armed_at <= stamp <= window_end:
                    retired_during_window = True
            failures = drain_stall_failures(
                accepted=accepted,
                delivered_distinct=distinct,
                preserved_distinct=preserved,
                duplicate_wakes=duplicates,
                reconnect_successes_while_armed=successes_while_armed,
                retired_during_window=retired_during_window,
                reconnected_after_window=resume_seconds is not None,
                resume_seconds_after_window=resume_seconds,
            )
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "fault": {
                        "keys": ["stall_server_to_client_until"],
                        "window_seconds": window_seconds,
                        "persistent_across_connections": True,
                        "note": (
                            "45 s reply-stall window armed at the first "
                            "observed delivery: the frame the drain has on "
                            "the wire is the one the splice swallows"
                        ),
                    },
                    "verdict_rule": (
                        "drain-stall verdict: the window arms while a backlog "
                        "drain is in flight, so at most the one in-flight "
                        "message may be consumed; zero duplicates; every "
                        "other accepted message is delivered once; no "
                        "reconnect succeeds while the window holds; "
                        "recovery follows the spec's branches - a silence "
                        "the connection survives resumes the drain on the "
                        "same connection within one probe interval of the "
                        "window's end, a longer silence retires the "
                        "connection and recovery follows the reconnect "
                        "backoff (one to thirty seconds on TCP under "
                        "bounded jitter) plus one probe budget for a gated "
                        "reconnect; the case's measured bound is 45 s on "
                        "the retired branch and one probe interval on the "
                        "surviving branch"
                    ),
                    "requested_messages": count,
                    "peer_accepted_sends": accepted,
                    "window_seconds": window_seconds,
                    "delivered_before_arm": delivered_before_arm,
                    "reconnect_churn": churn,
                    "reconnect_attempts_while_armed": attempts_while_armed,
                    "reconnect_successes_while_armed": successes_while_armed,
                    "retired_during_window": retired_during_window,
                    "resume_seconds_after_window": resume_seconds,
                    "arrivals_at_medre": len(arrivals),
                    "distinct_at_medre": distinct,
                    "duplicate_wakes": duplicates,
                    "distinct_persisted": preserved,
                    "consumed_wakes": max(0, accepted - distinct - preserved),
                    "probe": send,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")

    # -- the silent dead-link case ---------------------------------------
    def case_idle_blackhole(self) -> None:
        """Blackhole an up, idle link in both directions, no close.

        Phase 1: the splice starts with an empty fault and the fault MEDRE
        connects and drains; the run then waits out an idle window so the
        pump is parked.  Phase 2 (T0): both directions are armed by
        writing the splice's fault file - no FIN, no RST, no restart - and
        the peer queues messages at the companion while the link is dead.
        Phase 3 (T1): the fault file is cleared and the case waits for the
        reconnect and the delivery of what was queued.
        """
        name = "fault-idle-blackhole"
        prefix = "p4blackhole"
        try:
            self.stop_bridge_medre()
            self.stop_fault_medre()
            self.start_proxy({})
            if not self.wait_for(lambda: container_running(PROXY), 60, interval=1.0):
                self.not_run(name, "the fault splice did not start")
                return
            if not self.start_fault_medre():
                self.not_run(name, "the fault MEDRE container did not start")
                return
            # A clean first connect logs "started (mode=tcp, connected=True)";
            # "reconnected successfully" only follows a disconnect-reconnect.
            first_connect = re.compile(
                r"started \(mode=tcp, connected=True\)|reconnected successfully",
                re.IGNORECASE,
            )
            if not self.wait_for(
                lambda: first_connect.search(container_logs(MEDRE_FAULT)) is not None,
                120,
                interval=2.0,
            ):
                self.not_run(name, "the fault MEDRE never completed its first connect")
                return
            # Let the drain rounds finish and the pump park (~10 s of
            # silence after the last round), plus a margin.
            time.sleep(25)
            armed_at = time.time()
            state_dir = LAB_ROOT / "lab/state/openhop-fault"
            (state_dir / "fault.json").write_text(
                json.dumps(
                    {
                        "stall_server_to_client": True,
                        "stall_client_to_server": True,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            send = self.peer_send(3, prefix)
            # Wait out the detection window (first probe within 15 s of
            # parking plus the probe timeout) for the retirement line.
            retired = self.wait_for(
                lambda: PROBE_RETIREMENT.search(container_logs(MEDRE_FAULT))
                is not None,
                75,
                interval=2.0,
            )
            if not retired:
                self.not_run(
                    name,
                    "no idle-link probe retirement was recorded while the fault held",
                )
                (state_dir / "fault.json").write_text("{}\n", encoding="utf-8")
                return
            time.sleep(4)
            cleared_at = time.time()
            (state_dir / "fault.json").write_text("{}\n", encoding="utf-8")
            self.wait_for(
                lambda: (
                    "reconnected successfully"
                    in container_logs(MEDRE_FAULT)[
                        container_logs(MEDRE_FAULT).rfind("retiring connection") :
                    ]
                    if "retiring connection" in container_logs(MEDRE_FAULT)
                    else False
                ),
                90,
                interval=2.0,
            )
            time.sleep(12)
            log = timestamped_logs(MEDRE_FAULT)
            churn = count_reconnects(log)
            mine = sent_markers(send)
            events = (
                state_in(MEDRE_FAULT, "medre", prefix).get("canonical_events") or []
            )
            arrivals = [
                marker
                for marker in (
                    nonce_marker(json.dumps(event, sort_keys=True)) for event in events
                )
                if marker is not None and marker in mine
            ]
            distinct = len(set(arrivals))
            duplicates = len(arrivals) - distinct

            retire_seconds = None
            for line in log.splitlines():
                if PROBE_RETIREMENT.search(line):
                    stamp = line_time(line)
                    if stamp is not None:
                        retire_seconds = stamp - armed_at
                        break
            # Attempts between the arm and the clear: the log is ordered,
            # so split the reconnect attempts by their timestamps.
            attempts_while_armed = 0
            successes_while_armed = 0
            success_after = None
            for line in log.splitlines():
                stamp = line_time(line)
                if stamp is None:
                    continue
                if RECONNECT.search(line) and armed_at <= stamp <= cleared_at:
                    attempts_while_armed += 1
                if "reconnected successfully" in line:
                    if stamp <= cleared_at:
                        successes_while_armed += 1
                    elif success_after is None:
                        success_after = stamp
            reconnect_seconds = (
                success_after - cleared_at if success_after is not None else None
            )
            failures = idle_blackhole_failures(
                retirements=churn["connections_retired_on_fetch_failure"],
                probe_retirements=len(PROBE_RETIREMENT.findall(log)),
                retire_seconds_after_arm=retire_seconds,
                reconnect_attempts_while_armed=attempts_while_armed,
                reconnect_successes_while_armed=successes_while_armed,
                duplicate_wakes=duplicates,
                accepted=int(send.get("accepted_count") or 0),
                delivered_distinct=distinct,
                reconnect_seconds_after_restore=reconnect_seconds,
            )
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "fault": {
                        "stall_server_to_client": True,
                        "stall_client_to_server": True,
                        "armed_after_idle": True,
                        "no_close": True,
                    },
                    "verdict_rule": (
                        "silent-dead-link verdict: exactly one retirement and "
                        "it must name the idle-link liveness probe, landing "
                        "within the measured 15-45 s window; reconnect "
                        "attempts continue on backoff and none succeeds while "
                        "the fault holds; zero duplicates; after the fault "
                        "clears the client reconnects through the reconnect "
                        "backoff (one to thirty seconds on TCP under bounded "
                        "jitter; the case's measured bound is 45 s) and every "
                        "message queued during the fault is delivered once"
                    ),
                    "requested_messages": 3,
                    "peer_accepted_sends": send.get("accepted_count"),
                    "reconnect_churn": churn,
                    "retire_seconds_after_arm": retire_seconds,
                    "reconnect_attempts_while_armed": attempts_while_armed,
                    "reconnect_successes_while_armed": successes_while_armed,
                    "reconnect_seconds_after_restore": reconnect_seconds,
                    "arrivals_at_medre": len(arrivals),
                    "distinct_at_medre": distinct,
                    "duplicate_wakes": duplicates,
                    "probe": send,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")


# fault dict -> what the splice injects on the server -> client direction,
# then whether the case must fully recover (mild fault, healthy link), and
# which judge scores the run ("mild" healthy-link rule, "bounded" for the
# per-connection worst case whose documented bound is asserted).
CASES = {
    "latency_under": ({"delay_ms": 900}, 6, "p4qu", 12.0, True, "mild"),
    "latency_over": ({"delay_ms": 4000}, 6, "p4qo", 25.0, False, "mild"),
    "selective_drop": ({"stall_after_n": 3}, 6, "p4qd", 25.0, False, "bounded"),
    "repeated_waiting": ({"delay_ms": 200}, 20, "p4qr", 25.0, True, "mild"),
    "reconnect": ({"delay_ms": 400}, 6, "p4qn", 12.0, True, "mild"),
}

# The time-window cases run their own arm / queue / clear flow.
WINDOW_RUNNERS = {
    "reply_stall": "case_reply_stall",
    "mid_fetch_close": "case_mid_fetch_close",
    "saturation": "case_saturation",
    "reply_stall_drain": "case_reply_stall_during_drain",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir", default=str(LAB_ROOT / "lab/logs/openhop-fault")
    )
    parser.add_argument(
        "--case",
        default="all",
        choices=["all", *CASES, *WINDOW_RUNNERS, "idle_blackhole"],
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="regenerate the summary from the existing case files; run no case",
    )
    args = parser.parse_args(argv)

    matrix = FaultMatrix(Path(args.evidence_dir))
    if args.summary_only:
        summary = case_summary.build_summary(
            matrix.dir,
            prefix="fault-",
            label="protocol-simulated",
            generated_at=utcnow(),
        )
        case_summary.write_summary(matrix.dir, summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    try:
        case_summary.bind_source_selection(matrix.dir, LAB_ROOT, "openhop-fault")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    if args.case == "all":
        matrix.case_idle_blackhole()
        for key in WINDOW_RUNNERS:
            getattr(matrix, WINDOW_RUNNERS[key])()
        selected = list(CASES)
    else:
        selected = []
        if args.case == "idle_blackhole":
            matrix.case_idle_blackhole()
        elif args.case in WINDOW_RUNNERS:
            getattr(matrix, WINDOW_RUNNERS[args.case])()
        else:
            selected = [args.case]
    for key in selected:
        fault, count, prefix, settle, expect_recovery, verdict = CASES[key]
        matrix.run_case(
            f"fault-{key.replace('_', '-')}",
            fault,
            count,
            prefix,
            settle,
            expect_recovery,
            verdict,
        )
    # The summary is built from the EVIDENCE DIRECTORY, not from the cases
    # this invocation ran: re-running one case must not drop the other
    # cases from the summary.
    summary = case_summary.build_summary(
        matrix.dir,
        prefix="fault-",
        label="protocol-simulated",
        generated_at=utcnow(),
    )
    case_summary.write_summary(matrix.dir, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
