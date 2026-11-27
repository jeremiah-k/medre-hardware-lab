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
  reconnect        TIME WINDOW: a bounded close window breaks the
                   established link (the first request in flight is
                   closed, re-armed per connection inside the window).
                   The window then ends and the adapter must reconnect
                   through its own reconnect path - its reconnect
                   attempts and its reconnected-successfully line are the
                   required evidence, not merely traffic resuming - and
                   deliver the batch once on the new connection.
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
  saturation       a deep backlog waits behind a reply-stall window
                   sized to the batch (batch x measured peer pacing x
                   1.5 + 10 s, fallback pacing 2 s/send), so the whole
                   batch is accepted inside the window by construction:
                   the designed probe retirements of a link that
                   answers nothing are expected, nothing may be
                   consumed, and the whole backlog must be delivered
                   once the window ends.
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
  selective_drop   the PER-CONNECTION worst case the spec bounds: THE
                   LINK, constructed content-triggered (drop_message_
                   frames): the splice forwards the handshake and the
                   liveness-probe (device-query) replies and swallows
                   the message frames AND the MESSAGES_WAITING
                   notifications, so only an answered probe can re-arm
                   a fetch and the link consumes at most one queued
                   message per probe interval.  Its verdict asserts
                   the documented bound per connection - at most one
                   consumed message per answered liveness probe on
                   that connection beyond the unproven first fetch,
                   every other accepted message delivered or
                   preserved, zero duplicates - and PROVES the shape:
                   the splice's own connection identity must attribute
                   an answered probe and a lost frame to the SAME
                   connection, observed within a window bounded by
                   at least two probe intervals after the batch is
                   confirmed queued.
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
import os
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
from openhop_fault_settings import write_fault  # noqa: E402

LAB_ROOT = Path(__file__).resolve().parents[2]
TOOLS = LAB_ROOT / "lab" / "tools"
STATE_TOOL = TOOLS / "openhop_store_snapshot.py"
PEER_PROBE = TOOLS / "openhop_peer_probe.py"

PROXY = "medre-lab-openhop-fault-proxy"
REPEATER_A = "medre-lab-openhop-repeater-a"
MEDRE_BRIDGE = "medre-lab-medre-openhop-bridge"
MEDRE_FAULT = "medre-lab-medre-openhop-fault"

# The selective-drop link's proof parameters.  The pinned adapter probes
# the idle link every 15 s (_PROBE_IDLE_INTERVAL in the adapter's
# session.py); the deterministic observation bound is at least two of
# those intervals.  The message-carrying frame types are the replies to
# the client's sync command (a swallowed one is a consumed message); the
# probe reply type is DEVICE_INFO, which the link forwards.
PROBE_INTERVAL_S = 15.0
MESSAGE_CARRYING_TYPES = frozenset({0x07, 0x08, 0x10, 0x11})
PROBE_REPLY_TYPE = 0x0D

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


def wall_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp.timestamp() if stamp.tzinfo is not None else None


def nonce_index(text: str) -> int | None:
    match = NONCE_INDEX.search(str(text or ""))
    return int(match.group("index")) if match else None


def splice_stall_events(proxy_log: str) -> list[tuple[float, str, int]]:
    """Return (timestamp, connection) for every frame boundary the splice logged.

    The splice logs one JSON line per per-connection point where it
    started swallowing server->client frames - the budget exhaustion
    (``{"event": "stalled", "connection": N}``) and the content-triggered
    message-frame drops (``{"event": "message_frame_dropped",
    "connection": N}``) - and ``docker logs -t`` prefixes each line with
    the host's UTC time.  These events are the splice's own connection
    identity: the boundaries that attribute MEDRE's answered probes and
    the lost frames to the connection they belonged to, instead of
    counting aggregates over the whole run.  Returns (timestamp, event,
    connection) triples.
    """
    boundary_events = {"stalled", "message_frame_dropped"}
    events: list[tuple[float, str, int]] = []
    for line in proxy_log.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        stamp = line_time(line)
        if stamp is None:
            continue
        try:
            payload = json.loads(line[start:])
        except ValueError:
            continue
        if payload.get("event") in boundary_events and isinstance(
            payload.get("connection"), int
        ):
            events.append((stamp, payload["event"], payload["connection"]))
    return sorted(events)


def splice_link_events(proxy_log: str) -> list[dict]:
    """Return the splice's link events with their packet types.

    Every ``{"event": ..., "connection": N, "packet_type": T}`` line the
    splice logged for the link the selective-drop case constructs:
    ``message_frame_dropped`` (a swallowed message frame or notification),
    ``probe_reply_forwarded`` (a liveness-probe reply the link let
    through), and the legacy budget ``stalled`` boundaries.  Timestamps
    come from ``docker logs -t``.
    """
    events: list[dict] = []
    for line in proxy_log.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        stamp = line_time(line)
        if stamp is None:
            continue
        try:
            payload = json.loads(line[start:])
        except ValueError:
            continue
        if payload.get("event") in (
            "stalled",
            "message_frame_dropped",
            "probe_reply_forwarded",
        ) and isinstance(payload.get("connection"), int):
            events.append(
                {
                    "at": stamp,
                    "event": payload["event"],
                    "connection": payload["connection"],
                    "packet_type": payload.get("packet_type"),
                }
            )
    return sorted(events, key=lambda e: e["at"])


def selective_drop_proof_report(link_events: list[dict]) -> dict:
    """Prove, per connection, the link both answered a probe and lost a frame.

    The selective-drop claim is about ONE connection: the same link
    forwarded a liveness-probe reply and swallowed a message-carrying
    frame (packet types 0x07/0x08/0x10/0x11 - a swallowed message frame
    is a consumed message).  Swallowed NO_MORE_MSGS replies (0x0a) and
    MESSAGES_WAITING notifications (0x83) are part of the link's
    definition but do not count as lost message frames: they carry no
    message.  The proof holds when one connection has at least one of
    each.
    """
    probe_replies_forwarded: dict[int, int] = {}
    message_frames_dropped: dict[int, int] = {}
    for event in link_events:
        connection = event["connection"]
        if event["event"] == "probe_reply_forwarded":
            probe_replies_forwarded[connection] = (
                probe_replies_forwarded.get(connection, 0) + 1
            )
        elif (
            event["event"] == "message_frame_dropped"
            and event.get("packet_type") in MESSAGE_CARRYING_TYPES
        ):
            message_frames_dropped[connection] = (
                message_frames_dropped.get(connection, 0) + 1
            )
    proven = any(
        message_frames_dropped.get(connection, 0) > 0
        and probe_replies_forwarded.get(connection, 0) > 0
        for connection in set(probe_replies_forwarded) | set(message_frames_dropped)
    )
    return {
        "probe_replies_forwarded_per_connection": probe_replies_forwarded,
        "message_frames_dropped_per_connection": message_frames_dropped,
        "same_connection_proven": proven,
    }


def connection_of(stamp: float, stalls: list[tuple[float, int]]) -> int | None:
    """Return the splice connection a timestamp belongs to, or None.

    Connection N forwards its first chunks - so probes are ANSWERED on it -
    until its own budget exhausts at its stall event; only then does the
    client retire and hand over to the next connection.  A stamp is
    therefore attributed to the connection of the FIRST stall event at or
    after it.  A stamp after the last stall belongs to a connection the
    splice never recorded stalling and cannot be attributed.
    """
    for stall_stamp, _event, connection in stalls:
        if stamp <= stall_stamp:
            return connection
    return None


def per_connection_counts(
    stamps: list[float], stalls: list[tuple[float, int]]
) -> dict[int, int]:
    """Count the timestamps per attributed splice connection."""
    counts: dict[int, int] = {}
    for stamp in stamps:
        connection = connection_of(stamp, stalls)
        if connection is not None:
            counts[connection] = counts.get(connection, 0) + 1
    return counts


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


# The saturation case's batch and the window it is sized against.  The
# precondition the window verdict asserts - the whole batch accepted
# inside the window - holds BY CONSTRUCTION here: the window is the
# batch's duration at the peer's measured send pacing, times a 1.5
# safety factor, plus 10 s of headroom.  The fallback pacing covers a
# standalone saturation run with no preceding measurement on this rig.
SATURATION_BATCH = 60
SATURATION_PACING_FALLBACK_S = 2.0
SATURATION_PACING_FACTOR = 1.5
SATURATION_WINDOW_HEADROOM_S = 10.0


def saturation_window_for(batch: int, measured_pacing_s: float | None) -> float:
    """Derive the saturation window from the peer's measured send pacing.

    The claim is unchanged: a deep backlog builds behind the stalled
    link, the designed probe retirements happen, nothing is consumed
    while the window holds, and the whole backlog is delivered exactly
    once afterwards.  Only the window's length is derived: the batch's
    duration at the measured (or fallback 2 s/send) pacing, times a 1.5
    safety factor, plus 10 s of headroom.
    """
    pacing = (
        measured_pacing_s
        if measured_pacing_s and measured_pacing_s > 0
        else SATURATION_PACING_FALLBACK_S
    )
    return round(
        batch * pacing * SATURATION_PACING_FACTOR + SATURATION_WINDOW_HEADROOM_S, 1
    )


def reconnect_case_failures(
    *,
    accepted: int,
    delivered_distinct: int,
    preserved_distinct: int,
    duplicate_wakes: int,
    reconnect_attempts_while_faulted: int,
    reconnect_successes_while_faulted: int,
    reconnected_after_window: bool,
    reconnect_seconds_after_window: float | None,
) -> list[str]:
    """Judge the case named ``reconnect`` by its own claim.

    The case breaks the established link with a bounded close window and
    is named for what must follow: the adapter reconnecting.  Traffic
    resuming is not the claim - the ADAPTER's reconnect evidence is, so
    the verdict asserts:

    * reconnect attempts on the backoff while the close window holds and
      NONE of them succeeds (the handshake is closed like any other
      request);
    * a reconnect observed AFTER the window ends through the adapter's
      own "reconnected successfully" line - a line that only follows a
      disconnect-reconnect, never a clean first connect.  Without it the
      case is not a pass, whatever arrived;
    * the reconnect lands within the case's measured bound (the
      reconnect backoff plus one gated probe budget, 45 s);
    * every accepted message is delivered once on the new connection;
    * zero duplicates anywhere in the run.
    """
    failures: list[str] = []
    if duplicate_wakes:
        failures.append(f"{duplicate_wakes} duplicate wake(s) reached MEDRE's store")
    if accepted > 0 and delivered_distinct + preserved_distinct != accepted:
        failures.append(
            f"{delivered_distinct} delivered + {preserved_distinct} preserved at "
            f"the companion != {accepted} accepted messages"
        )
    if preserved_distinct:
        failures.append(
            f"{preserved_distinct} message(s) were still queued at the "
            "companion after the reconnect should have drained them"
        )
    if reconnect_attempts_while_faulted < 1:
        failures.append("no reconnect attempt was recorded while the fault held")
    if reconnect_successes_while_faulted:
        failures.append(
            f"{reconnect_successes_while_faulted} reconnect(s) succeeded while "
            "the close window still held"
        )
    if not reconnected_after_window:
        failures.append(
            "no reconnect was observed after the fault ended: the adapter's "
            "reconnect evidence is missing, and traffic resuming is not a "
            "reconnect"
        )
    elif reconnect_seconds_after_window is None:
        failures.append("the reconnect line carried no parsable timestamp")
    elif reconnect_seconds_after_window > 45.0:
        failures.append(
            f"the post-fault reconnect took {reconnect_seconds_after_window:.1f} s, "
            "later than the reconnect backoff and probe budget (45 s)"
        )
    return failures


def selective_drop_failures(
    *,
    accepted: int,
    delivered_distinct: int,
    preserved_distinct: int,
    duplicate_wakes: int,
    consumed_per_connection: dict[int, int],
    answered_per_connection: dict[int, int],
    unattributed_consumed: int,
    same_connection_proven: bool,
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
    asserts that bound PER CONNECTION, attributed by the splice's own
    connection identity, instead of zero loss:

    * zero duplicates anywhere in the run;
    * the proof requires ONE connection with both an answered liveness
      probe and a lost message frame attributed to it - aggregate counts
      alone do not prove the selective drop, and without that
      attribution the case is not a pass;
    * every connection that lost frames stays within its own bound of
      one consumed message per answered probe on that same connection
      beyond the unproven first fetch, and the counts are reported per
      connection;
    * a lost frame that cannot be attributed to a connection the splice
      recorded is a refusal, not a pass;
    * every other accepted message is delivered or still preserved.
    """
    consumed = sum(consumed_per_connection.values()) + unattributed_consumed
    failures: list[str] = []
    if duplicate_wakes:
        failures.append(f"{duplicate_wakes} duplicate wake(s) reached MEDRE's store")
    if consumed < 0 or delivered_distinct + preserved_distinct > accepted:
        failures.append(
            f"{delivered_distinct} delivered + {preserved_distinct} preserved "
            f"exceeds the {accepted} accepted messages"
        )
    if unattributed_consumed:
        failures.append(
            f"{unattributed_consumed} lost message(s) could not be attributed "
            "to any connection the splice recorded stalling"
        )
    if not same_connection_proven:
        failures.append(
            "no connection had both an answered liveness probe and a lost "
            "message frame attributed to it: the per-connection attribution "
            "does not prove the selective drop"
        )
    for connection, consumed_on_connection in sorted(consumed_per_connection.items()):
        answered_on_connection = answered_per_connection.get(connection, 0)
        bound = 1 + max(0, answered_on_connection)
        if consumed_on_connection > bound:
            failures.append(
                f"connection {connection} lost {consumed_on_connection} "
                f"message(s), more than the documented bound of one per "
                f"answered probe beyond the unproven first fetch on that "
                f"same connection ({answered_on_connection} answered "
                f"probe(s), bound {bound})"
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
    if retired_during_window and not reconnected_after_window:
        failures.append(
            "the connection retired inside the window and never reconnected after it ended"
        )
    limit = 45.0 if retired_during_window else 15.0
    if resume_seconds_after_window is None:
        failures.append("no delivery was observed after the stall window ended")
    elif resume_seconds_after_window > limit:
        if retired_during_window:
            failures.append(
                "the drain resumed later than the reconnect backoff and probe budget (45 s)"
            )
        else:
            failures.append(
                "the surviving connection took longer than one probe interval (15 s) to resume"
            )
    return failures


class FaultMatrix:
    def __init__(self, evidence_dir: Path) -> None:
        self.dir = evidence_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.results: list[dict] = []
        # The peer's measured per-send pacing (seconds between consecutive
        # accepted send completions), refreshed by every peer_send call; the
        # saturation window is derived from it.  The fallback pacing covers
        # a standalone saturation run with no preceding measurement.
        self.peer_pacing_s: float | None = None

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
        write_fault(state / "fault.json", fault)
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
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{LAB_ROOT / 'lab/openhop-radio/openhop_fault_proxy.py'}:"
            "/opt/openhop-fault/openhop_fault_proxy.py:ro",
            "-v",
            f"{state}:/opt/openhop-fault:ro",
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
        self.record_peer_pacing(data)
        return data

    def drain_companion_a(
        self, rounds: int = 3, wake_batch: int = 8, seconds: float = 90.0
    ) -> int:
        """Drain companion A's leftover queue rows into their owner.

        The bounded selective-drop case leaves messages preserved at the
        companion by design, and the fork's queue-ownership rule binds
        those rows to the client identity that was attached when they
        were persisted - a generic SDK probe cannot pop another client's
        rows.  The drain therefore re-attaches the case's OWN MEDRE
        through an empty splice (no fault) and lets its receive pump pop
        the queue into MEDRE's own store, polling the repeater's queue
        total until it reads zero or the deadline passes.  Returns the
        rows still held when the drain gave up; the caller logs the
        remainder.  Hygiene, not measurement: every case's preserved
        counts are already recorded in its evidence.
        """
        first_connect = re.compile(
            r"started \(mode=tcp, connected=True\)|reconnected successfully",
            re.IGNORECASE,
        )
        try:
            if container_running(MEDRE_BRIDGE):
                # The companion serves one client at a time: the drain's
                # MEDRE is the client while this runs.
                run(["docker", "stop", MEDRE_BRIDGE], timeout=180)
            self.start_proxy({})
            if not self.wait_for(lambda: container_running(PROXY), 60, interval=1.0):
                return (
                    state_in(REPEATER_A, "repeater").get("companion_message_total") or 0
                )
            self.stop_fault_medre()
            if not self.start_fault_medre():
                return (
                    state_in(REPEATER_A, "repeater").get("companion_message_total") or 0
                )
            self.wait_for(
                lambda: first_connect.search(container_logs(MEDRE_FAULT)) is not None,
                120,
                interval=2.0,
            )
            deadline = time.monotonic() + seconds
            for _ in range(rounds):
                remaining = (
                    state_in(REPEATER_A, "repeater").get("companion_message_total") or 0
                )
                if remaining == 0:
                    return 0
                # A fresh arrival is announced (a real MESSAGES_WAITING
                # push), and the wake-driven drain serves oldest-first:
                # the batch empties the companion's queue, stale rows
                # included.
                self.peer_send(wake_batch, "p4drainw")
                batch_deadline = time.monotonic() + 60.0
                while time.monotonic() < batch_deadline:
                    remaining = (
                        state_in(REPEATER_A, "repeater").get("companion_message_total")
                        or 0
                    )
                    if remaining == 0:
                        return 0
                    time.sleep(3)
                if time.monotonic() >= deadline:
                    break
            remaining = (
                state_in(REPEATER_A, "repeater").get("companion_message_total") or 0
            )
            return remaining
        finally:
            self.stop_fault_medre()
            run(["docker", "rm", "-f", PROXY], timeout=120)

    def record_peer_pacing(self, probe_result: dict) -> None:
        """Refresh the peer's measured per-send pacing from a batch.

        The pacing is the mean gap between consecutive accepted sends'
        completion stamps - the peer's own send round trip on companion B,
        which the faults do not touch.
        """
        stamps = sorted(
            stamp
            for entry in probe_result.get("sends") or []
            if entry.get("accepted") is True
            and (stamp := wall_timestamp(entry.get("finished_at"))) is not None
        )
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        if gaps:
            self.peer_pacing_s = sum(gaps) / len(gaps)

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
            if verdict != "bounded":
                if not self.start_fault_medre():
                    self.not_run(name, "the fault MEDRE container did not start")
                    return
                time.sleep(settle)
                send = self.peer_send(count, prefix)
                if send.get("pass") is not True or len(sent_markers(send)) != count:
                    self.not_run(
                        name, "the peer did not accept the complete traffic batch"
                    )
                    return
                accepted = int(send.get("accepted_count") or 0)
                time.sleep(settle)
            else:
                # (a) ORDER, bounded only: the link is armed but NO client
                # is attached, so the peer queues the whole batch at
                # companion A and the rows ACCUMULATE (nothing drains
                # them); the case confirms the batch queued BEFORE the
                # fault MEDRE attaches - the attach is the observation
                # window's start.
                time.sleep(settle)
                send = self.peer_send(count, prefix)
                if send.get("pass") is not True or len(sent_markers(send)) != count:
                    self.not_run(
                        name, "the peer did not accept the complete traffic batch"
                    )
                    return
                accepted = int(send.get("accepted_count") or 0)
                batch_queued = self.wait_for(
                    lambda: len(
                        state_in(REPEATER_A, "repeater", prefix).get(
                            "companion_messages"
                        )
                        or []
                    )
                    >= accepted,
                    180,
                    interval=2.0,
                )
                if not batch_queued:
                    self.not_run(
                        name,
                        "the companion never reported the whole batch queued; "
                        "the observation window cannot start",
                    )
                    return
                if not self.start_fault_medre():
                    self.not_run(name, "the fault MEDRE container did not start")
                    return
            log = container_logs(MEDRE_FAULT)
            churn = count_reconnects(log)
            mine = sent_markers(send)
            queue_snapshots: list[dict] = []
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
            connection_evidence: dict = {}
            if verdict == "bounded":
                # (b) CONTENT-TRIGGERED WAIT: the batch is already confirmed
                # queued and the fault MEDRE has just attached, so the
                # window observes until the splice has recorded, on ONE
                # connection, both a forwarded probe reply and a dropped
                # message-carrying frame.  The bound is at least two probe
                # intervals (three, for margin) derived from the configured
                # probe interval.
                observation_bound_s = 3 * PROBE_INTERVAL_S
                observation_deadline = time.monotonic() + observation_bound_s
                proof_report: dict = {}
                while True:
                    proof_report = selective_drop_proof_report(
                        splice_link_events(timestamped_logs(PROXY))
                    )
                    if proof_report["same_connection_proven"]:
                        break
                    if time.monotonic() >= observation_deadline:
                        break
                    time.sleep(2.0)
                # One probe interval of margin before the accounting reads,
                # so a second consumed message (the next probe's fetch) is
                # spanned too.
                time.sleep(PROBE_INTERVAL_S)
                # Per-connection attribution: the splice's own frame-boundary
                # events carry the connection identity, so the answered
                # probes and the lost frames are attributed to the
                # connection they belonged to instead of being counted as
                # run totals.
                stalls = splice_stall_events(timestamped_logs(PROXY))
                stamped_medre_log = timestamped_logs(MEDRE_FAULT)
                probe_stamps = [
                    stamp
                    for line in stamped_medre_log.splitlines()
                    if ANSWERED_PROBE.search(line)
                    and (stamp := line_time(line)) is not None
                ]
                answered_per_connection = per_connection_counts(probe_stamps, stalls)
                send_stamps: dict[str, float] = {}
                for entry in send.get("sends") or []:
                    marker = nonce_marker(entry.get("nonce") or "")
                    stamp = wall_timestamp(entry.get("finished_at"))
                    if marker is not None and stamp is not None:
                        send_stamps[marker] = stamp
                consumed_per_connection: dict[int, int] = {}
                unattributed_consumed = 0
                for marker in mine - set(indices) - set(persisted_indices):
                    stamp = send_stamps.get(marker)
                    connection = (
                        connection_of(stamp, stalls) if stamp is not None else None
                    )
                    if connection is None:
                        unattributed_consumed += 1
                    else:
                        consumed_per_connection[connection] = (
                            consumed_per_connection.get(connection, 0) + 1
                        )
                same_connection_proven = any(
                    consumed_on_connection > 0
                    and answered_per_connection.get(connection, 0) > 0
                    for connection, consumed_on_connection in (
                        consumed_per_connection.items()
                    )
                )
                failures = selective_drop_failures(
                    accepted=int(send.get("accepted_count") or 0),
                    delivered_distinct=distinct,
                    preserved_distinct=preserved,
                    duplicate_wakes=duplicates,
                    consumed_per_connection=consumed_per_connection,
                    answered_per_connection=answered_per_connection,
                    unattributed_consumed=unattributed_consumed,
                    same_connection_proven=same_connection_proven,
                )
                proof_report = selective_drop_proof_report(
                    splice_link_events(timestamped_logs(PROXY))
                )
                connection_evidence = {
                    "link_definition": (
                        "the splice forwards the handshake and the "
                        "liveness-probe (device-query) replies and swallows "
                        "the message frames AND the MESSAGES_WAITING "
                        "notifications, so only an answered probe can re-arm "
                        "a fetch"
                    ),
                    "probe_interval_s": PROBE_INTERVAL_S,
                    "observation_bound_s": 3 * PROBE_INTERVAL_S,
                    "batch_confirmed_queued": batch_queued,
                    **proof_report,
                    "splice_frame_events": [
                        {
                            "event": event,
                            "connection": c,
                            "at": time.strftime(
                                "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(s)
                            ),
                        }
                        for s, event, c in stalls
                    ],
                    "answered_probes_per_connection": answered_per_connection,
                    "consumed_per_connection": consumed_per_connection,
                    "unattributed_consumed": unattributed_consumed,
                    "same_connection_proof": same_connection_proven,
                }
                verdict_rule = (
                    "selective-drop verdict (the documented worst case): the "
                    "link forwards the handshake and the liveness-probe "
                    "(device-query) replies and swallows the message frames "
                    "AND the MESSAGES_WAITING notifications, so only an "
                    "answered probe can re-arm a fetch, and it consumes at "
                    "most one queued message per answered liveness probe "
                    "beyond the unproven first fetch, PER CONNECTION - the "
                    "injection is content-triggered (the splice parses the "
                    "companion frames), so the answered probes and the "
                    "swallowed message frames are the same connection's by "
                    "construction; the proof requires one connection with "
                    "both, observed within an observation window bounded by "
                    "at least two probe intervals; every other accepted "
                    "message is delivered or still preserved at the "
                    "companion; zero duplicates"
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
                    **connection_evidence,
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
        fault_path = None
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
            fault_path = state_dir / "fault.json"
            write_fault(fault_path, fault)
            send = self.peer_send(count, prefix)
            if send.get("pass") is not True or len(sent_markers(send)) != count:
                self.not_run(name, "the peer did not accept the complete traffic batch")
                return
            # The splice itself stops faulting the moment the deadline
            # passes (it re-reads the file per chunk), so the deadline is
            # the authoritative window end even if queueing overran it.
            window_end = armed_at + window_seconds
            if any(
                (stamp := wall_timestamp(entry.get("finished_at"))) is None
                or stamp >= window_end
                for entry in send["sends"]
            ):
                self.not_run(
                    name,
                    "the peer traffic batch did not finish inside the fault window",
                )
                return
            if time.time() < window_end:
                time.sleep(window_end - time.time())
            write_fault(state_dir / "fault.json", {})
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
                if "reconnected successfully" in line and stamp >= armed_at:
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
        finally:
            if fault_path is not None:
                write_fault(fault_path, {})

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
        """A deep backlog waits behind a reply-stall window sized to the
        peer's measured send pacing; the whole backlog must come across
        once the window ends."""
        window = saturation_window_for(SATURATION_BATCH, self.peer_pacing_s)
        self.run_window_case(
            "fault-saturation",
            {"stall_server_to_client_until": True},
            window,
            SATURATION_BATCH,
            "p4qs",
            f"reply-stall window sized to the batch ({SATURATION_BATCH} "
            f"messages x peer pacing x 1.5 + 10 s = {window:.0f} s; pacing "
            f"fallback {SATURATION_PACING_FALLBACK_S:.1f} s/send, measured "
            f"{self.peer_pacing_s if self.peer_pacing_s else 'n/a'}), so the "
            "whole batch is accepted inside the window by construction",
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
        fault_path = None
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
                    fault_path = state_dir / "fault.json"
                    write_fault(
                        fault_path,
                        {
                            "stall_server_to_client_until": window_end,
                        },
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
            write_fault(state_dir / "fault.json", {})
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
            if send.get("pass") is not True or len(mine) != count:
                self.not_run(name, "the peer did not accept the complete traffic batch")
                return
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
            reconnected_after_window = False
            deliveries_after_window = [
                stamp
                for event in events
                if nonce_marker(json.dumps(event, sort_keys=True)) in mine
                and (stamp := wall_timestamp(event.get("created_at"))) is not None
                and stamp >= window_end
            ]
            resume_seconds = (
                min(deliveries_after_window) - window_end
                if deliveries_after_window
                else None
            )
            for line in log.splitlines():
                stamp = line_time(line)
                if stamp is None:
                    continue
                if RECONNECT.search(line) and armed_at <= stamp <= window_end:
                    attempts_while_armed += 1
                if "reconnected successfully" in line and stamp >= armed_at:
                    if stamp <= window_end:
                        successes_while_armed += 1
                    else:
                        reconnected_after_window = True
                if "retiring connection" in line and armed_at <= stamp <= window_end:
                    retired_during_window = True
            failures = drain_stall_failures(
                accepted=accepted,
                delivered_distinct=distinct,
                preserved_distinct=preserved,
                duplicate_wakes=duplicates,
                reconnect_successes_while_armed=successes_while_armed,
                retired_during_window=retired_during_window,
                reconnected_after_window=reconnected_after_window,
                resume_seconds_after_window=resume_seconds,
            )
            if delivered_before_arm >= accepted:
                failures.append("the backlog was exhausted when the stall was armed")
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
        finally:
            if fault_path is not None:
                write_fault(fault_path, {})

    def case_reconnect(self) -> None:
        """Break the established link once, then require the reconnect.

        Phase 1: the splice forwards with the case's mild delay and the
        fault MEDRE connects; the run waits out an idle window so the
        pump is parked.  Phase 2 (T0): a bounded close window is armed -
        the first request in flight (the parked pump's next probe, within
        one probe interval of the arm) has its connection closed, and the
        close re-arms with every connection inside the window, so the
        reconnect attempts' handshakes are closed too.  Phase 3 (T1): the
        window ends, the adapter must reconnect through its own reconnect
        path, and the batch is sent and must be delivered once on the new
        connection.
        """
        name = "fault-reconnect"
        prefix = "p4qn"
        count = 6
        window_seconds = 30.0
        state_dir = LAB_ROOT / "lab/state/openhop-fault"
        state_dir.mkdir(parents=True, exist_ok=True)
        fault_path = None
        try:
            self.stop_bridge_medre()
            self.stop_fault_medre()
            self.start_proxy({"delay_ms": 400})
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
            # Let the drain rounds finish and the pump park before the
            # window opens, so the first request it closes is the parked
            # pump's own probe.
            time.sleep(25)
            armed_at = time.time()
            window_end = armed_at + window_seconds
            fault_path = state_dir / "fault.json"
            write_fault(
                fault_path,
                {
                    "delay_ms": 400,
                    "close_on_request_until": window_end,
                },
            )
            # The deadline is the authoritative window end: the splice
            # re-reads the file per chunk and stops closing the moment it
            # has passed.
            if time.time() < window_end:
                time.sleep(window_end - time.time())
            write_fault(state_dir / "fault.json", {})

            def reconnected_after_window() -> bool:
                for line in timestamped_logs(MEDRE_FAULT).splitlines():
                    stamp = line_time(line)
                    if (
                        stamp is not None
                        and stamp >= window_end
                        and "reconnected successfully" in line
                    ):
                        return True
                return False

            self.wait_for(reconnected_after_window, 120, interval=2.0)
            time.sleep(5)
            send = self.peer_send(count, prefix)
            mine = sent_markers(send)
            if send.get("pass") is not True or len(mine) != count:
                self.not_run(name, "the peer did not accept the complete traffic batch")
                return
            accepted = int(send.get("accepted_count") or 0)
            self.wait_for(
                lambda: len(
                    state_in(MEDRE_FAULT, "medre", prefix).get("canonical_events") or []
                )
                >= accepted,
                180,
                interval=3.0,
            )
            # The drain: a 6-message batch delivers in seconds on this
            # rig, so a short margin is plenty.
            time.sleep(12)
            log = timestamped_logs(MEDRE_FAULT)
            churn = count_reconnects(log)
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
            attempts_while_faulted = 0
            successes_while_faulted = 0
            success_after = None
            for line in log.splitlines():
                stamp = line_time(line)
                if stamp is None:
                    continue
                if RECONNECT.search(line) and armed_at <= stamp <= window_end:
                    attempts_while_faulted += 1
                if "reconnected successfully" in line and stamp >= armed_at:
                    if stamp <= window_end:
                        successes_while_faulted += 1
                    elif success_after is None:
                        success_after = stamp
            reconnect_seconds = (
                success_after - window_end if success_after is not None else None
            )
            failures = reconnect_case_failures(
                accepted=accepted,
                delivered_distinct=distinct,
                preserved_distinct=preserved,
                duplicate_wakes=duplicates,
                reconnect_attempts_while_faulted=attempts_while_faulted,
                reconnect_successes_while_faulted=successes_while_faulted,
                reconnected_after_window=success_after is not None,
                reconnect_seconds_after_window=reconnect_seconds,
            )
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "fault": {
                        "keys": ["delay_ms", "close_on_request_until"],
                        "window_seconds": window_seconds,
                        "persistent_across_connections": True,
                        "note": (
                            "30 s close window armed while the pump was "
                            "parked: the first request in flight is closed, "
                            "and every handshake inside the window with it"
                        ),
                    },
                    "verdict_rule": (
                        "reconnect verdict: reconnect attempts continue on "
                        "backoff and none succeeds while the close window "
                        "holds; after the window ends the adapter must show "
                        "its own reconnect evidence - a reconnected-"
                        "successfully line, which only follows a disconnect-"
                        "reconnect - within the measured 45 s bound, and "
                        "every accepted message is delivered once on the new "
                        "connection; traffic resuming alone is not a pass"
                    ),
                    "requested_messages": count,
                    "peer_accepted_sends": accepted,
                    "armed_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(armed_at)
                    ),
                    "window_seconds": window_seconds,
                    "reconnect_churn": churn,
                    "reconnect_attempts_while_faulted": attempts_while_faulted,
                    "reconnect_successes_while_faulted": successes_while_faulted,
                    "reconnected_after_window": success_after is not None,
                    "reconnect_seconds_after_window": reconnect_seconds,
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
        finally:
            if fault_path is not None:
                write_fault(fault_path, {})

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
        fault_path = None
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
            fault_path = state_dir / "fault.json"
            write_fault(
                fault_path,
                {
                    "stall_server_to_client": True,
                    "stall_client_to_server": True,
                },
            )
            send = self.peer_send(3, prefix)
            if send.get("pass") is not True or len(sent_markers(send)) != 3:
                self.not_run(name, "the peer did not accept the complete traffic batch")
                return
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
                write_fault(state_dir / "fault.json", {})
                return
            time.sleep(4)
            cleared_at = time.time()
            write_fault(state_dir / "fault.json", {})
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
                if "reconnected successfully" in line and stamp >= armed_at:
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
        finally:
            if fault_path is not None:
                write_fault(fault_path, {})


# fault dict -> what the splice injects on the server -> client direction,
# then whether the case must fully recover (mild fault, healthy link), and
# which judge scores the run ("mild" healthy-link rule, "bounded" for the
# per-connection worst case whose documented bound is asserted).
CASES = {
    "latency_under": ({"delay_ms": 900}, 6, "p4qu", 12.0, True, "mild"),
    "latency_over": ({"delay_ms": 4000}, 6, "p4qo", 25.0, False, "mild"),
    "selective_drop": (
        {"drop_message_frames": True},
        6,
        "p4qd",
        25.0,
        False,
        "bounded",
    ),
    "repeated_waiting": ({"delay_ms": 200}, 20, "p4qr", 25.0, True, "mild"),
}

# The time-window cases run their own arm / queue / clear flow; the
# reconnect case is one of them: its claim (the link recovers and the
# client reconnects) needs a fault that starts and ends, and its verdict
# asserts the adapter's own reconnect evidence.
WINDOW_RUNNERS = {
    "reply_stall": "case_reply_stall",
    "mid_fetch_close": "case_mid_fetch_close",
    "saturation": "case_saturation",
    "reply_stall_drain": "case_reply_stall_during_drain",
    "reconnect": "case_reconnect",
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
    # Hygiene: the bounded selective-drop case can leave messages
    # preserved at companion A BY DESIGN (that is its documented
    # outcome).  A later rig whose listener attaches to companion A
    # would receive those stale rows and count them against its own
    # one-packet assertions, so the invocation ends by stopping the
    # case's MEDRE and splice (the companion serves one client at a
    # time) and draining the leftover queue rows through the documented
    # client path.  Hygiene, not measurement: the preserved counts are
    # already recorded in each case's evidence.
    matrix.stop_fault_medre()
    remaining = matrix.drain_companion_a()
    if remaining:
        print(
            f"companion A still holds {remaining} leftover row(s) after the "
            "drain; a later listener at companion A will receive them"
        )
    return (
        0
        if matrix.results and all(row["status"] == "PASS" for row in matrix.results)
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
