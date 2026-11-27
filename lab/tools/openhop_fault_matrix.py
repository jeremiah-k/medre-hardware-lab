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
                   CONSTRUCTED to cover the batch: the stall is armed
                   with a far guard deadline (hang protection only),
                   the batch is sent, and the stall holds one headroom
                   interval past the last accepted send before the
                   window ends, so the whole batch is accepted inside
                   the window by construction: the designed probe
                   retirements of a link that answers nothing are
                   expected, nothing may be consumed, and the whole
                   backlog must be delivered once the window ends.  The
                   predicted figure (batch x measured peer pacing x
                   1.5 + 10 s, fallback pacing 2 s/send) is recorded
                   for information; the measured window governs.
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
                   confirmed queued.  The accounting is frozen at the
                   observation's end: the fault MEDRE's log and store
                   are captured, the client is stopped, the companion
                   store is read once after the stop, and only the
                   splice events up to the stop are counted; every
                   lost message is attributed to the connection whose
                   swallowed frame carried its marker and cross-checked
                   against the swallowed frames per connection.
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
from openhop_observe import (  # noqa: E402
    empty_reply_after,
    reconnect_after,
    scoped_log_line,
)

LAB_ROOT = Path(__file__).resolve().parents[2]
TOOLS = LAB_ROOT / "lab" / "tools"
STATE_TOOL = TOOLS / "openhop_store_snapshot.py"
PEER_PROBE = TOOLS / "openhop_peer_probe.py"

PROXY = "medre-lab-openhop-fault-proxy"
REPEATER_A = "medre-lab-openhop-repeater-a"
MEDRE_BRIDGE = "medre-lab-medre-openhop-bridge"
MEDRE_FAULT = "medre-lab-medre-openhop-fault"

# The splice's forwarded-frame trace: both drivers start the splice with
# it ALWAYS on, so the case records carry every forwarded frame's packet
# type and lab marker next to the fault events.
PROXY_TRACE_ENV = "OPENHOP_FAULT_PROXY_TRACE"

# The selective-drop link's proof parameters.  The pinned adapter probes
# the idle link every 15 s (_PROBE_IDLE_INTERVAL in the adapter's
# session.py); the deterministic observation bound is at least two of
# those intervals.  The message-carrying frame types are the replies to
# the client's sync command (a swallowed one is a consumed message); the
# probe reply type is DEVICE_INFO, which the link forwards.
PROBE_INTERVAL_S = 15.0
FETCH_WINDOW_S = 2.0
# Allow a parked probe to get its answer and one following fetch to finish.
PARK_BOUND_S = PROBE_INTERVAL_S + FETCH_WINDOW_S + 3.0
# The TCP reconnect cap is 30 s, the gated probe can take 15 s, and
# eight seconds allows a drain of 60 local messages plus Docker polling.
RECOVERY_DRAIN_BOUND_S = 30.0 + PROBE_INTERVAL_S + 8.0

MESSAGE_CARRYING_TYPES = frozenset({0x07, 0x08, 0x10, 0x11})
PROBE_REPLY_TYPE = 0x0D
DROP_MESSAGE_FRAMES_KEY = "drop_message_frames"

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


def run_merged(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    """Run a command with stderr merged into stdout, in true write order.

    The rig containers log to both streams (the repeater's ``logging``
    output is stderr, the splice's JSON events are stdout), and docker
    relays each stream separately - so two separately captured strings
    cannot be interleaved afterwards.  The consumers that need the lines
    in the order the container wrote them read ONE merged pipe.
    """
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout


def container_running(name: str) -> bool:
    rc, out, _ = run(["docker", "inspect", "-f", "{{.State.Running}}", name])
    return rc == 0 and out.strip() == "true"


def container_logs(name: str) -> str:
    _, merged = run_merged(["docker", "logs", name], timeout=120)
    return merged


def timestamped_logs(name: str) -> str:
    """Return the container's logs with one RFC3339 timestamp per line.

    The idle-blackhole verdict measures WHEN the pump retired relative to
    the moment the fault was armed, so its lines need timestamps; docker's
    -t flag prefixes each line with the host's UTC time.  Both streams are
    read through one merged pipe, so the timestamps order the lines as
    the container wrote them.
    """
    _, merged = run_merged(["docker", "logs", "-t", name], timeout=120)
    return merged


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


def splice_stall_events(proxy_log: str) -> list[dict]:
    """Return every frame-boundary event the splice logged, as dicts.

    The splice logs one JSON line per per-connection point where it
    started swallowing server->client frames - the budget exhaustion
    (``{"event": "stalled", "connection": N}``) and the content-triggered
    message-frame drops (``{"event": "message_frame_dropped",
    "connection": N, "packet_type": T, "marker": M}``) - and ``docker
    logs -t`` prefixes each line with the host's UTC time.  These events
    are the splice's own connection identity: the boundaries that
    attribute MEDRE's answered probes and the lost frames to the
    connection they belonged to, instead of counting aggregates over the
    whole run.  Returns dicts with ``at`` (epoch), ``event``,
    ``connection``, ``packet_type`` (hex string when present) and
    ``marker`` (the lab nonce the frame carried, None when it carries
    none).
    """
    boundary_events = {"stalled", "message_frame_dropped"}
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
        if payload.get("event") in boundary_events and isinstance(
            payload.get("connection"), int
        ):
            packet_type = payload.get("packet_type")
            events.append(
                {
                    "at": stamp,
                    "event": payload["event"],
                    "connection": payload["connection"],
                    "packet_type": (
                        "0x%02x" % packet_type if isinstance(packet_type, int) else None
                    ),
                    "marker": payload.get("marker"),
                }
            )
    return sorted(events, key=lambda e: e["at"])


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
                    "marker": payload.get("marker"),
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
        "same_connection_proof": proven,
    }


def probe_replies_forwarded_per_connection(events: list[dict]) -> dict[int, int]:
    """Count the liveness-probe replies the splice FORWARDED, per connection.

    The splice's own ``probe_reply_forwarded`` events ARE the answered
    probes - one forwarded DEVICE_INFO reply each, with the connection
    id the splice recorded on the wire - so a probe answered just before
    its connection retires is credited to the connection that answered
    it, never to the connection that followed it.
    """
    counts: dict[int, int] = {}
    for event in events:
        if event.get("event") != "probe_reply_forwarded":
            continue
        connection = event["connection"]
        counts[connection] = counts.get(connection, 0) + 1
    return counts


def swallowed_message_frames(events: list[dict]) -> list[dict]:
    """The swallowed MESSAGE-CARRYING frames among the splice's events.

    A dropped frame of one of the message-carrying types carried a queued
    message; dropped NO_MORE_MSGS replies and MESSAGES_WAITING pushes
    carry no message and never count here.
    """
    return [
        event
        for event in events
        if event.get("event") == "message_frame_dropped"
        and event.get("packet_type") in MESSAGE_CARRYING_TYPES
    ]


def swallowed_marker_connections(events: list[dict]) -> dict[str, int]:
    """Map each swallowed message-carrying frame's marker to its connection.

    This is the attribution rule for a lost message: it belongs to the
    connection whose recorded drop carried the message's marker.
    """
    return {
        event["marker"]: event["connection"]
        for event in swallowed_message_frames(events)
        if event.get("marker")
    }


def selective_drop_cross_check(events: list[dict], lost_markers: list[str]) -> dict:
    """Cross-check the splice's own accounting against the final store read.

    Per connection: the message-carrying frames the splice swallowed
    (each named by the lab marker it carried) against the rows actually
    lost at the final read - a swallowed frame consumed exactly the
    message it carried, so the two counts must agree on every connection.
    A swallowed message-carrying frame WITHOUT a marker cannot be counted
    for any message, and a lost row whose swallowed frame the splice did
    not record cannot be attributed; either makes ``equal`` false, and
    the case fails on it.
    """
    swallowed = swallowed_message_frames(events)
    frame_connections = swallowed_marker_connections(events)
    per_connection: dict[str, dict] = {}
    for event in swallowed:
        entry = per_connection.setdefault(
            str(event["connection"]),
            {"swallowed_message_frames": 0, "lost_messages": 0, "equal": False},
        )
        entry["swallowed_message_frames"] += 1
    unattributed_lost = 0
    for marker in lost_markers:
        connection = frame_connections.get(marker)
        if connection is None:
            unattributed_lost += 1
            continue
        entry = per_connection.setdefault(
            str(connection),
            {"swallowed_message_frames": 0, "lost_messages": 0, "equal": False},
        )
        entry["lost_messages"] += 1
    for entry in per_connection.values():
        entry["equal"] = entry["swallowed_message_frames"] == entry["lost_messages"]
    without_marker = sum(1 for event in swallowed if not event.get("marker"))
    equal = (
        without_marker == 0
        and unattributed_lost == 0
        and all(entry["equal"] for entry in per_connection.values())
    )
    return {
        "per_connection": per_connection,
        "swallowed_message_frames_without_marker": without_marker,
        "unattributed_lost_messages": unattributed_lost,
        "equal": equal,
    }


def reconnected_after_retirement(log: str) -> bool:
    """True when the log shows a reconnect success after its last retirement.

    The reconnect evidence the stall cases wait for: the line only counts
    when it follows the connection's retirement, and the log is read ONCE
    so the slice and its anchor come from the same snapshot.
    """
    anchor = log.rfind("retiring connection")
    if anchor < 0:
        return False
    return "reconnected successfully" in log[anchor:]


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


def wake_batch_arrived_whole(batch: dict, delivered_markers: set[str]) -> bool:
    """The queue drain's success test: its own wake batch arrived whole.

    ``batch`` is one peer_send result and ``delivered_markers`` the
    markers the fault MEDRE's store holds for the wake prefix.  Each
    batch's unique tails make one message one message, so an earlier
    wake batch's rows cannot stand in for this one: every marker THIS
    batch sent must be among the delivered ones.
    """
    markers = sent_markers(batch)
    return bool(markers) and markers <= delivered_markers


def this_run_rows(rows: list[dict], mine: set[str]) -> tuple[int, int]:
    """Split prefix-scoped companion rows into this run's and foreign.

    Returns (this run's rows, rows of another run).  A store read scoped
    only by the PREFIX also matches an earlier run's rows - the unique
    tails are what tell the runs apart - so a wait condition counting
    prefix rows can be satisfied by leftovers before this run's batch
    has even arrived.
    """
    own = 0
    foreign = 0
    for row in rows:
        marker = nonce_marker(row.get("text") or "")
        if marker is not None and marker in mine:
            own += 1
        else:
            foreign += 1
    return own, foreign


def delivered_this_run(events: list[dict], mine: set[str]) -> int:
    """How many of THIS run's markers a MEDRE store snapshot holds.

    MEDRE's store is cumulative, so a count over the prefix alone rises
    with an earlier run's events; the unique tails scope the count to
    this run.
    """
    markers = {
        marker
        for marker in (
            nonce_marker(json.dumps(event, sort_keys=True)) for event in events
        )
        if marker is not None
    }
    return len(markers & mine)


def inert_rows_by_identity(rows: list[dict]) -> list[dict]:
    """Group the rows that remain after the drain by companion identity.

    The queue-ownership rule binds a row to the client identity that was
    attached when it was persisted, so rows of ANOTHER identity can never
    be popped by this client: they are inert, reported (hash and ids)
    rather than chased as leftovers.
    """
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(str(row.get("companion_hash")), []).append(row.get("id"))
    return [
        {"companion_hash": companion_hash, "ids": sorted(ids, key=str)}
        for companion_hash, ids in sorted(grouped.items())
    ]


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


# The saturation case's batch and the window around it.  The window is
# CONSTRUCTED to cover the batch: the stall is armed with a far guard
# deadline, and the real window ends one headroom interval past the last
# accepted send, so the whole batch is accepted inside the window by
# construction whatever pacing the peer measures.  saturation_window_for
# keeps deriving the PREDICTED figure (batch x measured pacing x 1.5
# safety factor + headroom) that the record carries for information; the
# fallback pacing covers a standalone saturation run with no preceding
# measurement on this rig.
SATURATION_BATCH = 60
SATURATION_PACING_FALLBACK_S = 2.0
SATURATION_PACING_FACTOR = 1.5
SATURATION_WINDOW_HEADROOM_S = 10.0
# The saturation case's stall is armed with this FAR deadline, which only
# guards against a hang: the real window end is constructed after the
# batch (one headroom interval past the last accepted send), so the stall
# covers the batch whatever its measured duration.
SATURATION_WINDOW_GUARD_S = 3600.0


def saturation_window_for(batch: int, measured_pacing_s: float | None) -> float:
    """Predict the saturation window from the peer's measured send pacing.

    The claim is unchanged: a deep backlog builds behind the stalled
    link, the designed probe retirements happen, nothing is consumed
    while the window holds, and the whole backlog is delivered exactly
    once afterwards.  The stall itself is CONSTRUCTED to cover the batch
    (armed with a far guard deadline, ended one headroom interval past
    the last accepted send); this function derives the PREDICTED figure
    the record carries for information: the batch's duration at the
    measured (or fallback 2 s/send) pacing, times a 1.5 safety factor,
    plus 10 s of headroom.
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
    same_connection_proof: bool,
    accounting_cross_check: dict | None = None,
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
    if not same_connection_proof:
        failures.append(
            "no connection had both an answered liveness probe and a lost "
            "message frame attributed to it: the per-connection attribution "
            "does not prove the selective drop"
        )
    if accounting_cross_check is not None:
        if not accounting_cross_check.get("equal"):
            failures.append(
                "the accounting cross-check failed: the swallowed "
                "message-carrying frames the splice recorded and the lost "
                "rows disagree, or a swallowed frame carried no lab marker "
                f"({json.dumps(accounting_cross_check, sort_keys=True)})"
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
            "the connection retired inside the window and never "
            "reconnected after it ended"
        )
    limit = 45.0 if retired_during_window else 15.0
    if resume_seconds_after_window is None:
        failures.append("no delivery was observed after the stall window ended")
    elif resume_seconds_after_window > limit:
        if retired_during_window:
            failures.append(
                "the drain resumed later than the reconnect backoff "
                "and probe budget (45 s)"
            )
        else:
            failures.append(
                "the surviving connection took longer than one probe "
                "interval (15 s) to resume"
            )
    return failures


class FaultMatrix:
    def __init__(self, evidence_dir: Path) -> None:
        self.dir = evidence_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.results: list[dict] = []
        self.observations: list[dict] = []
        # Pass-through control for the selective-drop case: when set, the
        # case arms its link WITHOUT drop_message_frames, so nothing is
        # lost and the verdict fails on the missing proof by design.  The
        # evidence records the control shape.
        self.selective_drop_passthrough = False
        # The peer's measured per-send pacing (seconds between consecutive
        # accepted send completions), refreshed by every peer_send call; the
        # saturation window is derived from it.  The fallback pacing covers
        # a standalone saturation run with no preceding measurement.
        self.peer_pacing_s: float | None = None

    def capture_case_logs(
        self, name: str, captured: dict[str, str] | None = None
    ) -> None:
        """Save each fault-rig container's timestamped log into the evidence.

        Called at the end of every case, BEFORE the end-of-run drain or any
        teardown removes the containers.  The logs are read through the
        merged pipe, so a container that logs to stderr (the repeater's
        ``logging`` output) is saved whole, in true time order.
        ``captured`` supplies log text already read from a container that
        has since been removed: the bounded selective-drop case freezes
        its MEDRE (log captured, client stopped) before the accounting,
        so its log is saved from the freeze capture.
        """
        captured = captured or {}
        for container, suffix in (
            (PROXY, "proxy"),
            (MEDRE_FAULT, "medre"),
            (REPEATER_A, "repeater-a"),
        ):
            merged = (
                captured[container]
                if container in captured
                else timestamped_logs(container)
            )
            (self.dir / f"{name}.{suffix}.log").write_text(merged, encoding="utf-8")

    def companion_queue_snapshot(self, label: str) -> dict:
        """WAL-aware snapshot of companion A's queue: one entry per row.

        Reads the snapshot tool's own mode=ro path inside the repeater
        container, all companion hashes, all columns, reduced to the
        fields the queue analysis needs (id, companion_hash,
        arrival_seq, marker).
        """
        snapshot = state_in(REPEATER_A, "repeater", "")
        rows = []
        for row in snapshot.get("companion_messages") or []:
            rows.append(
                {
                    "id": row.get("id"),
                    "companion_hash": row.get("companion_hash"),
                    "arrival_seq": row.get("arrival_seq"),
                    "marker": nonce_marker(row.get("text") or ""),
                }
            )
        return {"label": label, "rows": rows}

    def write(self, name: str, payload: dict) -> None:
        if self.observations:
            payload["observation_waits"] = self.observations
            self.observations = []
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
            "-e",
            f"{PROXY_TRACE_ENV}=1",
            "-v",
            f"{LAB_ROOT / 'lab/openhop-radio/openhop_fault_proxy.py'}:"
            "/opt/openhop-fault/openhop_fault_proxy.py:ro",
            "-v",
            f"{state}:/opt/openhop-fault:ro",
            "medre-lab/openhop-radio-relay:latest",
            "python3",
            "/opt/openhop-fault/openhop_fault_proxy.py",
            "--listen-host",
            "0.0.0.0",  # nosec B104 # serves the private lab Docker network only.
            "--listen-port",
            "5050",
            "--upstream-host",
            "openhop-repeater-a",
            "--upstream-port",
            "5000",
            "--fault-file",
            "/opt/openhop-fault/fault.json",
        ]
        rc, _, _ = run(cmd, timeout=300)
        if rc:
            raise RuntimeError(f"the fault splice did not launch (exit {rc})")
        # The proxy announces readiness only once its TCP socket listens.
        # Allow ten seconds for the measured one-to-two-second container start.
        if not self.wait_for(
            lambda: '"event": "listening"' in container_logs(PROXY), 10, interval=0.25
        ):
            raise RuntimeError("the fault splice did not announce a listening socket")

    def wait_for(self, predicate, timeout: float, interval: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def observe(self, name: str, predicate, timeout: float) -> bool:
        started = time.monotonic()
        observed = self.wait_for(predicate, timeout, interval=0.5)
        self.observations.append(
            {
                "name": name,
                "bound_seconds": timeout,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "observed": observed,
            }
        )
        return observed

    def wait_for_parked_link(self) -> bool:
        return self.observe(
            "initial-empty-queue",
            lambda: empty_reply_after(timestamped_logs(PROXY), 0),
            PARK_BOUND_S,
        )

    def wait_for_drain(
        self,
        prefix: str,
        markers: set[str],
        *,
        since: float,
        expected: int,
        timeout: float = RECOVERY_DRAIN_BOUND_S,
    ) -> bool:
        def complete() -> bool:
            events = state_in(MEDRE_FAULT, "medre", prefix)["canonical_events"]
            matched = [
                event
                for event in events
                if nonce_marker(json.dumps(event, sort_keys=True)) in markers
            ]
            if delivered_this_run(matched, markers) < expected:
                return False
            # The last fetched message can commit after the empty reply.
            # Both observations must be newer than the fault boundary.
            return empty_reply_after(timestamped_logs(PROXY), since)

        return self.observe("delivery-and-empty-queue", complete, timeout)

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
        self, rounds: int = 2, wake_batch: int = 2, seconds: float = 90.0
    ) -> dict:
        """Drain companion A's leftover queue rows through their owner.

        The bounded selective-drop case leaves messages preserved at the
        companion by design, and the fork's queue-ownership rule binds
        those rows to the client identity that was attached when they
        were persisted - a generic SDK probe cannot pop another client's
        rows.  The drain therefore re-attaches the case's OWN MEDRE
        through an empty splice (no fault) and lets its receive pump pop
        the queue into MEDRE's own store.

        The success criterion is CONSTRUCTIVE: the drain's own wake batch
        (prefix p4drainw, sent through the pass-through splice) arrives
        whole at the fault MEDRE.  The queue serves oldest-first, so when
        the wake batch has arrived, every row queued ahead of it for this
        client has been popped.  One retry with a second wake batch is
        allowed before the drain reports failure.  Rows that remain after
        the criterion is met belong to another lab identity and are inert
        for this client: they are reported as ``inert_rows`` (hash and
        ids), not chased.  Hygiene, not measurement: every case's
        preserved counts are already recorded in its evidence.
        """
        first_connect = re.compile(
            r"started \(mode=tcp, connected=True\)|reconnected successfully",
            re.IGNORECASE,
        )
        report: dict = {
            "wake_batch_arrived_whole": False,
            "rounds_used": 0,
            "remaining_rows": None,
            "inert_rows": [],
            "wake_batches": [],
        }
        try:
            if container_running(MEDRE_BRIDGE):
                # The companion serves one client at a time: the drain's
                # MEDRE is the client while this runs.
                run(["docker", "stop", MEDRE_BRIDGE], timeout=180)
            self.start_proxy({})
            if not self.wait_for(lambda: container_running(PROXY), 60, interval=1.0):
                report["error"] = "the fault splice did not start"
                report["remaining_rows"] = (
                    state_in(REPEATER_A, "repeater").get("companion_message_total") or 0
                )
                return report
            self.stop_fault_medre()
            if not self.start_fault_medre():
                report["error"] = "the fault MEDRE container did not start"
                report["remaining_rows"] = (
                    state_in(REPEATER_A, "repeater").get("companion_message_total") or 0
                )
                return report
            self.wait_for(
                lambda: first_connect.search(container_logs(MEDRE_FAULT)) is not None,
                120,
                interval=2.0,
            )
            deadline = time.monotonic() + seconds
            for round_index in range(rounds):
                report["rounds_used"] = round_index + 1
                # A fresh arrival is announced (a real MESSAGES_WAITING
                # push), and the wake-driven drain serves oldest-first:
                # the batch empties the companion's queue for this client,
                # stale rows included.
                batch = self.peer_send(wake_batch, "p4drainw")
                report["wake_batches"].append(
                    {
                        "pass": batch.get("pass"),
                        "accepted_count": batch.get("accepted_count"),
                        "nonces": batch.get("nonces") or [],
                    }
                )
                batch_deadline = time.monotonic() + 60.0
                while time.monotonic() < batch_deadline:
                    events = (
                        state_in(MEDRE_FAULT, "medre", "p4drainw").get(
                            "canonical_events"
                        )
                        or []
                    )
                    delivered = {
                        marker
                        for marker in (
                            nonce_marker(json.dumps(event, sort_keys=True))
                            for event in events
                        )
                        if marker is not None
                    }
                    if wake_batch_arrived_whole(batch, delivered):
                        rows = (
                            state_in(REPEATER_A, "repeater").get("companion_messages")
                            or []
                        )
                        report["wake_batch_arrived_whole"] = True
                        report["remaining_rows"] = len(rows)
                        report["inert_rows"] = inert_rows_by_identity(rows)
                        return report
                    time.sleep(3)
                if time.monotonic() >= deadline:
                    break
            report["remaining_rows"] = (
                state_in(REPEATER_A, "repeater").get("companion_message_total") or 0
            )
            return report
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
        fault = dict(fault)
        if self.selective_drop_passthrough and fault.get("drop_message_frames"):
            # Pass-through control: the same link, armed without the frame
            # drop, so nothing is lost and the verdict fails on the missing
            # proof by design.  The evidence records the control shape.
            fault = {
                key: value
                for key, value in fault.items()
                if key != DROP_MESSAGE_FRAMES_KEY
            }
        delay_armed_at = None
        fault_path = LAB_ROOT / "lab/state/openhop-fault/fault.json"
        delay_on_established_link = verdict == "mild" and not expect_recovery
        try:
            queue_snapshots: list[dict] = []
            if verdict == "bounded":
                # Clean-queue step: this case leaves preserved rows behind
                # BY DESIGN and the next case's client would pop them
                # first, so the drain runs immediately before the case
                # (and again at the end of the invocation).  Fail closed
                # only when the drain's wake batch does not arrive whole:
                # then the queue state is unknown and the case cannot
                # start; inert rows of another identity are not a reason
                # to refuse.
                queue_snapshots.append(
                    self.companion_queue_snapshot("queue_before_drain")
                )
                drain_report = self.drain_companion_a()
                queue_snapshots.append(
                    self.companion_queue_snapshot("queue_after_drain")
                )
                if not drain_report.get("wake_batch_arrived_whole"):
                    self.not_run(
                        name,
                        "the pre-case queue drain's wake batch did not arrive "
                        "whole; the queue state is unknown and the case "
                        "cannot start",
                        {
                            "queue_drain": drain_report,
                            "queue_snapshots": queue_snapshots,
                        },
                    )
                    return
            self.stop_bridge_medre()
            self.stop_fault_medre()
            self.start_proxy({} if delay_on_established_link else fault)
            # The client resolves the proxy's NAME once, at connect time, so
            # the proxy must be up and resolvable before the client starts.
            if not self.wait_for(lambda: container_running(PROXY), 60, interval=1.0):
                self.not_run(name, "the fault proxy did not start")
                return
            if verdict != "bounded":
                if not self.start_fault_medre():
                    self.not_run(name, "the fault MEDRE container did not start")
                    return
                if not self.wait_for_parked_link():
                    self.not_run(
                        name,
                        "the fault MEDRE did not connect and finish its initial drain",
                    )
                    return
                if delay_on_established_link:
                    delay_armed_at = time.time()
                    write_fault(fault_path, {"live_delay_ms": fault["delay_ms"]})
                send = self.peer_send(count, prefix)
                if send.get("pass") is not True or len(sent_markers(send)) != count:
                    self.not_run(
                        name, "the peer did not accept the complete traffic batch"
                    )
                    return
                accepted = int(send.get("accepted_count") or 0)
                mine = sent_markers(send)
                last_send = max(
                    wall_timestamp(entry["finished_at"]) for entry in send["sends"]
                )
                drain_bound_s = RECOVERY_DRAIN_BOUND_S
                if delay_armed_at is not None:
                    delay_s = fault["delay_ms"] / 1000
                    # Account for one queued message per delayed reply,
                    # plus a notification, an empty response and a trailing
                    # notification, then the fetch and scheduling margin.
                    drain_bound_s = (accepted + 3) * delay_s + FETCH_WINDOW_S + 4.0
                    if not self.wait_for(
                        lambda: scoped_log_line(
                            timestamped_logs(MEDRE_FAULT),
                            "fetch window elapsed",
                            delay_armed_at,
                        ),
                        2 * delay_s + FETCH_WINDOW_S + 4.0,
                        interval=0.5,
                    ):
                        self.not_run(
                            name,
                            "no fetch window elapsed on the established delayed link",
                        )
                        return
                if not self.wait_for_drain(
                    prefix,
                    mine,
                    since=last_send,
                    expected=accepted,
                    timeout=drain_bound_s,
                ):
                    self.not_run(
                        name, "the delayed link did not finish the traffic batch"
                    )
                    return
            else:
                # (a) ORDER, bounded only: the link is armed but NO client
                # is attached, so the peer queues the whole batch at
                # companion A and the rows ACCUMULATE (nothing drains
                # them); the case confirms the batch queued BEFORE the
                # fault MEDRE attaches - the attach is the observation
                # window's start.  The BEFORE_BATCH snapshot is taken
                # before the send, so it shows exactly what the queue
                # held before this case's own rows arrived.
                queue_snapshots.append(self.companion_queue_snapshot("before_batch"))
                send = self.peer_send(count, prefix)
                if send.get("pass") is not True or len(sent_markers(send)) != count:
                    self.not_run(
                        name, "the peer did not accept the complete traffic batch"
                    )
                    return
                accepted = int(send.get("accepted_count") or 0)
                # The batch gate counts only THIS run's markers: a prefix
                # read alone also matches an earlier run's leftovers, and
                # the record carries how many of those it saw (zero after
                # the clean-queue drain).
                mine = sent_markers(send)
                batch_queued = self.wait_for(
                    lambda: this_run_rows(
                        state_in(REPEATER_A, "repeater", prefix).get(
                            "companion_messages"
                        )
                        or [],
                        mine,
                    )[0]
                    >= accepted,
                    180,
                    interval=2.0,
                )
                _, foreign_prefix_rows = this_run_rows(
                    state_in(REPEATER_A, "repeater", prefix).get("companion_messages")
                    or [],
                    mine,
                )
                queue_snapshots.append(self.companion_queue_snapshot("batch_confirmed"))
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
                first_connect = re.compile(
                    r"started \(mode=tcp, connected=True\)|reconnected successfully",
                    re.IGNORECASE,
                )
                self.wait_for(
                    lambda: first_connect.search(container_logs(MEDRE_FAULT))
                    is not None,
                    120,
                    interval=2.0,
                )
                time.sleep(3)
                queue_snapshots.append(self.companion_queue_snapshot("attach_plus_3s"))
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
                    if proof_report["same_connection_proof"]:
                        break
                    if time.monotonic() >= observation_deadline:
                        break
                    time.sleep(2.0)
                # One probe interval of margin before the accounting reads,
                # so a second consumed message (the next probe's fetch) is
                # spanned too.
                time.sleep(PROBE_INTERVAL_S)
                # FREEZE before accounting: the observation ends when the
                # fault MEDRE stops.  Capture its log and its store while
                # it is up, stop it, take the store snapshot that ends the
                # observation, and read the splice log ONCE; every count
                # below derives from those reads, and splice events after
                # the freeze are not accounted.  Accounting from reads
                # taken at the attach would miss everything the link
                # consumed DURING the observation.
                stamped_medre_log = timestamped_logs(MEDRE_FAULT)
                events = (
                    state_in(MEDRE_FAULT, "medre", prefix).get("canonical_events") or []
                )
                freeze_at = time.time()
                self.stop_fault_medre()
                observation_end = self.companion_queue_snapshot("observation_end")
                queue_snapshots.append(observation_end)
                link_events = [
                    event
                    for event in splice_link_events(timestamped_logs(PROXY))
                    if event["at"] <= freeze_at
                ]
                stalls = [
                    event
                    for event in link_events
                    if event["event"] in ("stalled", "message_frame_dropped")
                ]
                log = stamped_medre_log
                churn = count_reconnects(log)
                mine = sent_markers(send)
                indices = [
                    marker
                    for marker in (
                        nonce_marker(json.dumps(event, sort_keys=True))
                        for event in events
                    )
                    if marker is not None and marker in mine
                ]
                distinct = len(set(indices))
                duplicates = len(indices) - distinct
                # The ONE companion read the accounting derives from: the
                # snapshot that ended the observation.
                persisted_markers = {
                    marker
                    for marker in (row.get("marker") for row in observation_end["rows"])
                    if marker is not None and marker in mine
                }
                preserved = len(persisted_markers)
                persisted_at_companion = len(persisted_markers)
                lost_messages = sorted(mine - set(indices) - persisted_markers)
                # Per-connection attribution: the answered probes are the
                # splice's own forwarded probe replies, counted per
                # connection; a LOST message belongs to the connection of
                # the swallowed frame that carried its marker - the
                # send-time connection has no relation to the fetch that
                # popped the row later.
                answered_per_connection = probe_replies_forwarded_per_connection(
                    link_events
                )
                frame_connections = swallowed_marker_connections(link_events)
                consumed_per_connection: dict[int, int] = {}
                unattributed_consumed = 0
                for marker in lost_messages:
                    connection = frame_connections.get(marker)
                    if connection is None:
                        unattributed_consumed += 1
                    else:
                        consumed_per_connection[connection] = (
                            consumed_per_connection.get(connection, 0) + 1
                        )
                cross_check = selective_drop_cross_check(link_events, lost_messages)
                # ONE proof field, from the splice's own events: one
                # connection both forwarded a probe reply and swallowed a
                # message-carrying frame.  The marker-based attribution
                # lives in the cross-check alone.
                failures = selective_drop_failures(
                    accepted=int(send.get("accepted_count") or 0),
                    delivered_distinct=distinct,
                    preserved_distinct=preserved,
                    duplicate_wakes=duplicates,
                    consumed_per_connection=consumed_per_connection,
                    answered_per_connection=answered_per_connection,
                    unattributed_consumed=unattributed_consumed,
                    same_connection_proof=proof_report["same_connection_proof"],
                    accounting_cross_check=cross_check,
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
                    "foreign_prefix_rows_at_batch_gate": foreign_prefix_rows,
                    **proof_report,
                    "splice_frame_events": [
                        {
                            "event": event["event"],
                            "connection": event["connection"],
                            "at": time.strftime(
                                "%Y-%m-%dT%H:%M:%S+00:00",
                                time.gmtime(event["at"]),
                            ),
                            "packet_type": (
                                "0x%02x" % event["packet_type"]
                                if isinstance(event["packet_type"], int)
                                else event["packet_type"]
                            ),
                            "marker": event.get("marker"),
                        }
                        for event in stalls
                    ],
                    "answered_probes_per_connection": answered_per_connection,
                    "consumed_per_connection": consumed_per_connection,
                    "unattributed_consumed": unattributed_consumed,
                    "queue_drain": drain_report,
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
                    "at least two probe intervals; the accounting is frozen "
                    "at the observation's end (the fault MEDRE's log and "
                    "store captured, the client stopped, the companion "
                    "store read once after the stop, splice events counted "
                    "only up to the stop), every lost message attributed to "
                    "the connection of the swallowed frame that carried its "
                    "marker, and the splice's swallowed message frames "
                    "cross-checked against the lost rows per connection; "
                    "every other accepted message is delivered or still "
                    "preserved at the companion; zero duplicates"
                )
            else:
                log = container_logs(MEDRE_FAULT)
                churn = count_reconnects(log)
                mine = sent_markers(send)
                events = (
                    state_in(MEDRE_FAULT, "medre", prefix).get("canonical_events") or []
                )
                indices = [
                    marker
                    for marker in (
                        nonce_marker(json.dumps(event, sort_keys=True))
                        for event in events
                    )
                    if marker is not None and marker in mine
                ]
                distinct = len(set(indices))
                duplicates = len(indices) - distinct
                persisted = (
                    state_in(REPEATER_A, "repeater", prefix).get("companion_messages")
                    or []
                )
                persisted_indices = [
                    marker
                    for marker in (
                        nonce_marker(row.get("text") or "") for row in persisted
                    )
                    if marker is not None and marker in mine
                ]
                preserved = len(set(persisted_indices))
                persisted_at_companion = len(persisted_indices)
                lost_messages: list[str] = []
                cross_check = None
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
            lost_wakes = (
                len(lost_messages)
                if verdict == "bounded"
                else max(0, preserved - distinct)
            )
            if verdict == "bounded":
                # The END snapshot is part of the record: take it BEFORE
                # the case file is written, or the label never reaches it.
                queue_snapshots.append(self.companion_queue_snapshot("end"))
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
                    "delay_armed_after_initial_drain": delay_armed_at,
                    "passthrough_control": self.selective_drop_passthrough,
                    "verdict_rule": verdict_rule,
                    "requested_messages": count,
                    "peer_accepted_sends": send.get("accepted_count"),
                    "queue_snapshots": queue_snapshots,
                    "reconnect_churn": churn,
                    "answered_probes": (
                        sum(answered_per_connection.values())
                        if verdict == "bounded"
                        else None
                    ),
                    **connection_evidence,
                    "arrivals_at_medre": len(indices),
                    "distinct_at_medre": distinct,
                    "duplicate_wakes": duplicates,
                    "persisted_at_companion": persisted_at_companion,
                    "distinct_persisted": preserved,
                    "consumed_wakes": consumed,
                    "lost_wakes": lost_wakes,
                    "lost_messages": lost_messages,
                    "accounting_cross_check": cross_check,
                    "probe": send,
                },
            )
            if verdict == "bounded":
                self.capture_case_logs(name, captured={MEDRE_FAULT: stamped_medre_log})
            elif verdict == "mild":
                self.capture_case_logs(name)
        except Exception as exc:  # pragma: no cover - defensive
            self.not_run(name, f"{type(exc).__name__}: {exc}")
        finally:
            if delay_armed_at is not None:
                write_fault(fault_path, {})

    # -- the time-window stall cases --------------------------------------
    def run_window_case(
        self,
        name: str,
        window_fault: dict,
        window_seconds: float,
        count: int,
        prefix: str,
        window_note: str,
        constructed_window: bool = False,
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

        With ``constructed_window`` the window is CONSTRUCTED to cover
        the batch instead of predicted from it: the stall is armed with a
        far deadline that only guards against a hang, the batch is sent,
        and the stall holds one headroom interval past the last accepted
        send before the window ends.  ``window_seconds`` then carries the
        predicted figure (recorded as ``predicted_window_s``), while the
        record's ``window_seconds`` is the measured arm-to-end span.  The
        fixed-window cases keep the abort - their window is the subject -
        but a NOT RUN record then carries the send result, so the pacing
        facts survive.
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
            if not self.wait_for_parked_link():
                self.not_run(
                    name, "the initial receive drain did not reach an empty queue"
                )
                return
            state_dir = LAB_ROOT / "lab/state/openhop-fault"
            armed_at = time.time()
            # The window keys carry unix deadlines: the fault is armed now
            # and persists until the deadline passes, across every
            # connection established inside the window.  A constructed
            # window arms the far guard deadline instead - the batch is
            # still in flight when the arm happens, so the real end is
            # derived from the batch below.
            arm_deadline = (
                armed_at + SATURATION_WINDOW_GUARD_S
                if constructed_window
                else armed_at + window_seconds
            )
            fault = {key: arm_deadline for key in window_fault}
            fault_path = state_dir / "fault.json"
            write_fault(fault_path, fault)
            send = self.peer_send(count, prefix)
            if send.get("pass") is not True or len(sent_markers(send)) != count:
                self.not_run(name, "the peer did not accept the complete traffic batch")
                return
            # The splice itself stops faulting the moment the deadline
            # passes (it re-reads the file per chunk), so the deadline is
            # the authoritative window end even if queueing overran it.
            if constructed_window:
                batch_stamps = [
                    stamp
                    for entry in send["sends"]
                    if (stamp := wall_timestamp(entry.get("finished_at"))) is not None
                ]
                if not batch_stamps:
                    self.not_run(
                        name,
                        "no accepted send carried a finished_at stamp; the "
                        "constructed window has no end to derive",
                        {"probe": send},
                    )
                    return
                batch_end = max(batch_stamps)
                batch_seconds = batch_end - armed_at
                window_end = batch_end + SATURATION_WINDOW_HEADROOM_S
                measured_window_s = window_end - armed_at
            else:
                window_end = armed_at + window_seconds
                if any(
                    (stamp := wall_timestamp(entry.get("finished_at"))) is None
                    or stamp >= window_end
                    for entry in send["sends"]
                ):
                    self.not_run(
                        name,
                        "the peer traffic batch did not finish inside the fault "
                        "window",
                        {"probe": send},
                    )
                    return
                measured_window_s = window_seconds
            if time.time() < window_end:
                time.sleep(window_end - time.time())
            write_fault(state_dir / "fault.json", {})
            mine = sent_markers(send)
            if not self.wait_for_drain(prefix, mine, since=window_end, expected=count):
                self.not_run(
                    name, "did not observe recovery and an empty companion queue"
                )
                return
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
            self.capture_case_logs(name)
            self.write(
                name,
                {
                    "case": name,
                    "pass": not failures,
                    "failures": failures,
                    "fault": {
                        "keys": sorted(window_fault),
                        "window_seconds": measured_window_s,
                        "constructed_window": constructed_window,
                        "batch_seconds": (
                            batch_seconds if constructed_window else None
                        ),
                        "predicted_window_s": (
                            window_seconds if constructed_window else None
                        ),
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
                    "window_seconds": measured_window_s,
                    "batch_seconds": batch_seconds if constructed_window else None,
                    "predicted_window_s": (
                        window_seconds if constructed_window else None
                    ),
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
            self.capture_case_logs(name)
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
        """A deep backlog waits behind a reply-stall window CONSTRUCTED to
        cover the batch: the stall is armed with a far guard deadline, the
        batch is sent, and the stall holds one headroom interval past the
        last accepted send before the window ends - the whole backlog must
        come across once the window ends."""
        window = saturation_window_for(SATURATION_BATCH, self.peer_pacing_s)
        self.run_window_case(
            "fault-saturation",
            {"stall_server_to_client_until": True},
            window,
            SATURATION_BATCH,
            "p4qs",
            f"reply-stall window CONSTRUCTED to cover the batch: armed with "
            f"a far guard deadline ({SATURATION_WINDOW_GUARD_S:.0f} s, hang "
            "protection only), held until one headroom interval past the "
            f"last accepted send; the predicted figure ({SATURATION_BATCH} "
            f"messages x peer pacing x 1.5 + 10 s = {window:.0f} s; pacing "
            f"fallback {SATURATION_PACING_FALLBACK_S:.1f} s/send, measured "
            f"{self.peer_pacing_s if self.peer_pacing_s else 'n/a'}) is "
            "recorded for information, the measured window governs",
            constructed_window=True,
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
            if not self.wait_for_parked_link():
                self.not_run(
                    name, "the initial receive drain did not reach an empty queue"
                )
                return
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
            mine = sent_markers(send)
            if not self.wait_for_drain(
                prefix, mine, since=window_end, expected=count - 1
            ):
                self.not_run(
                    name, "did not observe the interrupted backlog drain finish"
                )
                return
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
            self.capture_case_logs(name)
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
            self.capture_case_logs(name)
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
            if not self.wait_for_parked_link():
                self.not_run(
                    name, "the initial receive drain did not reach an empty queue"
                )
                return
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

            if not self.wait_for(
                lambda: reconnect_after(timestamped_logs(MEDRE_FAULT), window_end),
                RECOVERY_DRAIN_BOUND_S,
                interval=0.5,
            ):
                self.not_run(name, "did not observe a reconnect after the fault window")
                return
            send = self.peer_send(count, prefix)
            mine = sent_markers(send)
            if send.get("pass") is not True or len(mine) != count:
                self.not_run(name, "the peer did not accept the complete traffic batch")
                return
            accepted = int(send.get("accepted_count") or 0)
            last_send = max(
                wall_timestamp(entry["finished_at"]) or window_end
                for entry in send["sends"]
            )
            if not self.wait_for_drain(prefix, mine, since=last_send, expected=count):
                self.not_run(
                    name, "the replacement connection did not drain the traffic batch"
                )
                return
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
            self.capture_case_logs(name)
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
            self.capture_case_logs(name)
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
            if not self.wait_for_parked_link():
                self.not_run(
                    name, "the initial receive drain did not reach an empty queue"
                )
                return
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

            # The first TCP retry is scheduled immediately after retirement
            # with a one-second base and up to 25% jitter.
            def attempted_while_armed() -> bool:
                return any(
                    RECONNECT.search(line)
                    and (at := line_time(line)) is not None
                    and at >= armed_at
                    for line in timestamped_logs(MEDRE_FAULT).splitlines()
                )

            if not self.wait_for(
                attempted_while_armed, FETCH_WINDOW_S + 2, interval=0.5
            ):
                self.not_run(name, "no reconnect attempt was seen while the fault held")
                return
            cleared_at = time.time()
            write_fault(state_dir / "fault.json", {})
            if not self.wait_for_drain(
                prefix, sent_markers(send), since=cleared_at, expected=3
            ):
                self.not_run(
                    name, "did not observe the blackholed batch drain after restoration"
                )
                return
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
            self.capture_case_logs(name)
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
            self.capture_case_logs(name)
            self.not_run(name, f"{type(exc).__name__}: {exc}")
        finally:
            if fault_path is not None:
                write_fault(fault_path, {})


# fault dict -> what the splice injects on the server -> client direction,
# then whether the case must fully recover (mild fault, healthy link), and
# which judge scores the run ("mild" healthy-link rule, "bounded" for the
# per-connection worst case whose documented bound is asserted).
CASES = {
    "latency_under": ({"delay_ms": 900}, 6, "p4qu", True, "mild"),
    "latency_over": ({"delay_ms": 4000}, 6, "p4qo", False, "mild"),
    "selective_drop": (
        {"drop_message_frames": True},
        6,
        "p4qd",
        False,
        "bounded",
    ),
    "repeated_waiting": ({"delay_ms": 200}, 20, "p4qr", True, "mild"),
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
    parser.add_argument(
        "--selective-drop-passthrough",
        action="store_true",
        help="arm the selective-drop case as a pass-through control: no "
        "message frames are dropped, so the proof fails by design",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop after the first case that does not pass",
    )
    args = parser.parse_args(argv)

    matrix = FaultMatrix(Path(args.evidence_dir))
    matrix.selective_drop_passthrough = args.selective_drop_passthrough
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
    selected = (
        ["idle_blackhole", *WINDOW_RUNNERS, *CASES]
        if args.case == "all"
        else [args.case]
    )
    for key in selected:
        if key == "idle_blackhole":
            matrix.case_idle_blackhole()
        elif key in WINDOW_RUNNERS:
            getattr(matrix, WINDOW_RUNNERS[key])()
        else:
            fault, count, prefix, expect_recovery, verdict = CASES[key]
            matrix.run_case(
                f"fault-{key.replace('_', '-')}",
                fault,
                count,
                prefix,
                expect_recovery,
                verdict,
            )
        if args.fail_fast and any(row["status"] != "PASS" for row in matrix.results):
            break
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
    try:
        drain_report = matrix.drain_companion_a()
    except (RuntimeError, OSError) as exc:
        print(f"companion A drain could not run: {exc}")
        drain_report = None
    if drain_report is not None and not drain_report.get("wake_batch_arrived_whole"):
        print(
            "companion A drain failed: its wake batch did not arrive whole "
            f"({drain_report.get('remaining_rows')} row(s) still held); a "
            "later listener at companion A may receive stale rows"
        )
    elif drain_report is not None and drain_report.get("inert_rows"):
        print(
            "companion A drain left "
            f"{drain_report.get('remaining_rows')} inert row(s) for another "
            "lab identity (hash and ids in the drain's inert_rows); not "
            "leftovers"
        )
    return (
        0
        if matrix.results and all(row["status"] == "PASS" for row in matrix.results)
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
