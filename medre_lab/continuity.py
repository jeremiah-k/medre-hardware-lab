"""Opt-in continuity case: declared-link interruption and MEDRE process reopen.

This module extends the existing pair runner with an explicit, separately
invoked continuity case. It never changes the ordinary ``hardware run``
short-check semantics. The case runs in two owned worker processes:

``baseline``
    One MEDRE runtime process completes one delivery per direction, then
    waits for the declared host link to be disconnected (MeshCore BLE only:
    ``bluetoothctl disconnect`` of the MEDRE-owned board, performed by the
    CLI watcher or the operator), records actual disconnect/rejoin evidence,
    sends fresh markers on the rejoined link, and finally captures a
    reconcilable pending outbox case before a bounded graceful stop.

``reopen``
    A fresh process reopens the same campaign database and profiles only
    after the baseline owner retired (enforced by a run lock), verifies the
    persisted identities, records the disposition of the captured pending
    and uncertain work, and proves peer observation still functions with a
    fresh marker.

Physical interruption and MEDRE shutdown are separate, individually
evidenced actions. The interrupt stage polls real adapter diagnostics and
times out honestly; nothing tunes deadlines to obtain a pass. The pending
capture uses the lab runtime's real send pacing: a second burst message
lands in the outbox while the first one still holds the MeshCore session's
serialized pre-send pacing, so at stop one burst message is pending (never
claimed, no native handoff evidence) and the other is genuinely uncertain
(claimed, pacing interrupted before the native send boundary). Core's own
replay authority decides their post-reopen disposition; the case records
what actually happens and asserts only against the committed contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .config import pair as pair_selection
from .ownership import own, own_run
from .runtime import build

#: Send pacing used while the continuity runtime is assembled. The real
#: adapter pacing (sleep before the native send, serialized per session)
#: creates the bounded pre-handoff window the pending capture needs; the
#: ordinary short check keeps its own 2.5 s value.
CONTINUITY_MESSAGE_DELAY_SECONDS = 12.0

#: How long the baseline waits for someone to disconnect the declared link.
DEFAULT_INTERRUPT_WAIT_SECONDS = 300.0

#: Bounded post-reopen wait for the captured pending case to progress.
RECONCILE_WAIT_SECONDS = 180.0

#: Bounded post-reopen wait for the captured uncertain case to settle.
UNCERTAIN_SETTLE_WAIT_SECONDS = 120.0

_STOP_BUDGET_SECONDS = 30.0
_DB_POLL_INTERVAL = 0.2

_PENDING_STATUSES = ("pending", "retry_wait")


def _nonce() -> str:
    return "MLAB-" + uuid.uuid4().hex[:12]


def _now() -> float:
    return time.time()


def _read_state(run: Path) -> dict:
    path = run / "continuity-state.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _write_state(run: Path, state: dict) -> None:
    (run / "continuity-state.json").write_text(
        json.dumps(state, indent=2, default=str) + "\n"
    )


def capture_nonce_state(db: Path, nonce: str) -> dict:
    """Read-only durable-state snapshot for every canonical event with *nonce*.

    Uses a read-only connection (never ``immutable=1`` on a live database)
    so the snapshot cannot disturb the running owner's writer.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        events = con.execute(
            "SELECT DISTINCT event_id FROM event_body_content WHERE instr(body,?)>0",
            (nonce,),
        ).fetchall()
        out: dict = {"nonce": nonce, "events": []}
        for row in events:
            event_id = row["event_id"]
            out["events"].append(
                {
                    "event_id": event_id,
                    "receipts": [
                        dict(r)
                        for r in con.execute(
                            "SELECT receipt_id,delivery_plan_id,target_adapter,"
                            "target_channel,status,receipt_kind,failure_kind,"
                            "attempt_number,confirmation_level,created_at "
                            "FROM delivery_receipts WHERE event_id=? ORDER BY sequence",
                            (event_id,),
                        ).fetchall()
                    ],
                    "outbox": [
                        dict(r)
                        for r in con.execute(
                            "SELECT outbox_id,delivery_plan_id,target_adapter,"
                            "target_channel,status,attempt_number,lease_until,"
                            "worker_id,updated_at FROM delivery_outbox "
                            "WHERE event_id=? ORDER BY created_at",
                            (event_id,),
                        ).fetchall()
                    ],
                }
            )
        return out
    finally:
        con.close()


def _sent_receipt_event_id(snapshot: dict, destination: str) -> str | None:
    """Return the canonical event id with one failure-free sent receipt."""
    for event in snapshot["events"]:
        sent = [
            r
            for r in event["receipts"]
            if r["receipt_kind"] == "attempt"
            and r["status"] == "sent"
            and r["target_adapter"] == destination
        ]
        bad = [
            r
            for r in event["receipts"]
            if r["failure_kind"] is not None
            or r["status"] in ("failed", "dead_lettered", "denied")
        ]
        if len(sent) == 1 and not bad:
            return event["event_id"]
    return None


def _outbox_statuses(capture: dict) -> list[str]:
    return sorted({row["status"] for e in capture["events"] for row in e["outbox"]})


def _session_diag(adapter) -> dict:
    diag = adapter.diagnostics() if hasattr(adapter, "diagnostics") else {}
    session = diag.get("session")
    return session if isinstance(session, dict) else {}


async def _wait_for(predicate, timeout: float, interval: float = 0.5):
    """Poll ``predicate`` until true or timeout; return (ok, elapsed)."""
    started = time.monotonic()
    while True:
        if predicate():
            return True, time.monotonic() - started
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return False, time.monotonic() - started
        await asyncio.sleep(min(interval, max(0.05, remaining)))


def _listener_lifetime(hw: dict, interrupt_wait: float) -> float:
    return (
        hw["startup_timeout"]
        + 3 * (hw["send_timeout"] + hw["observation_timeout"])
        + interrupt_wait
        + 400.0
    )


async def _arm_listeners(listeners: dict, lifetime: float) -> dict:
    """Arm every configured peer listener once for the whole phase."""
    from .native.live_peer_common import async_peer_context

    armed: dict = {}
    for kind, factory in listeners.items():
        cm = async_peer_context(factory(lifetime))
        peer = await cm.__aenter__()
        armed[kind] = {"cm": cm, "peer": peer}
    return armed


async def _disarm_listeners(armed: dict) -> None:
    for kind in sorted(armed, reverse=True):
        entry = armed.pop(kind)
        await entry["cm"].__aexit__(None, None, None)


async def _run_leg(
    run: Path,
    hw: dict,
    senders: dict,
    armed: dict,
    source: str,
    destination: str,
    nonce: str,
    timeline: list,
) -> dict:
    """One native send from the independent peer plus far-side observation."""
    receiver = armed[destination]["peer"]
    before = time.monotonic()
    sent = await asyncio.to_thread(senders[source], nonce)
    observed = await asyncio.to_thread(
        receiver.packets_until,
        lambda ps: any(nonce in str(p.get("text", p.get("content", ""))) for p in ps),
        hw["observation_timeout"],
    )
    seen = [p for p in observed if nonce in str(p.get("text", p.get("content", "")))]
    leg = {
        "source": source,
        "destination": destination,
        "nonce": nonce,
        "native_send": sent,
        "observed": seen,
        "observed_once": len(seen) == 1
        and str(seen[0].get("text", seen[0].get("content", ""))).count(nonce) == 1,
        "elapsed_seconds": round(time.monotonic() - before, 3),
    }
    timeline.append(
        {
            "stage": f"leg:{source}->{destination}",
            "at": _now(),
            "nonce": nonce,
            "observed_once": leg["observed_once"],
        }
    )
    if not leg["observed_once"]:
        raise AssertionError(
            f"leg {source}->{destination}: expected exactly one correlated "
            f"far-side delivery of {nonce}"
        )
    return leg


def _leg_state(run: Path, leg: dict, destination: str) -> dict:
    """Attach the durable DB identity of one completed leg."""
    snapshot = capture_nonce_state(run / "state.sqlite", leg["nonce"])
    event_id = _sent_receipt_event_id(snapshot, destination)
    if event_id is None:
        raise AssertionError(
            f"{leg['nonce']} lacks one failure-free sent receipt at {destination}"
        )
    leg.update(event_id=event_id, durable=snapshot)
    return leg


def _child_pids() -> list[int]:
    children = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if ppid == os.getpid():
            children.append(int(entry.name))
    return children


async def _teardown_owner(app, result: dict) -> None:
    """Bounded stop of the MEDRE owner plus SDK-child teardown evidence."""
    if app is None:
        return
    children_before = _child_pids()
    started = time.monotonic()
    await asyncio.wait_for(app.stop(), _STOP_BUDGET_SECONDS)
    result["stop_seconds"] = round(time.monotonic() - started, 3)
    result["surviving_sdk_children"] = sorted(set(_child_pids()) - set(children_before))


async def run_baseline(
    config: dict, selection: str, role: int, run: Path, interrupt_wait: float
) -> bool:
    """Baseline owner phase: deliveries, link interruption, pending capture."""
    transports = pair_selection(selection)
    hw = config["hardware"]
    timeline: list = [{"stage": "baseline-start", "at": _now(), "pid": os.getpid()}]
    result: dict = {
        "phase": "baseline",
        "pair": list(transports),
        "role": "a" if role == 0 else "b",
        "pass": False,
        "legs": [],
        "interrupt": {},
        "pending_capture": {},
        "packages": {},
        "scope": "Opt-in continuity baseline; not the ordinary short check.",
    }
    app = None
    armed: dict = {}
    state = _read_state(run)
    state.update(
        phase="baseline",
        pair=list(transports),
        role=result["role"],
        message_delay_seconds=CONTINUITY_MESSAGE_DELAY_SECONDS,
        started_at=_now(),
    )
    _write_state(run, state)
    try:
        from .worker import configure, provenance, wait_startup_drain_retired

        result["packages"] = provenance(transports)
        listeners, senders, destinations, endpoints = configure(
            config, transports, role, run
        )
        if "meshcore" in hw:
            os.environ["MEDRE_LAB_OWNED_MESHCORE_ADDRESS"] = hw["meshcore"][
                "addresses"
            ][role]
        with own(endpoints), own_run(run):
            result["process"] = {"pid": os.getpid()}
            app = build(
                config,
                transports,
                role,
                run,
                destinations,
                message_delay_seconds=CONTINUITY_MESSAGE_DELAY_SECONDS,
            )
            await asyncio.wait_for(app.start(), hw["startup_timeout"])
            for adapter in app.adapters.values():
                health = await asyncio.wait_for(adapter.health_check(), 15)
                if health.health != "healthy":
                    raise RuntimeError("adapter did not report healthy at startup")
            result["startup_drain_wait"] = await wait_startup_drain_retired(app)
            timeline.append({"stage": "owner-started", "at": _now()})

            armed = await _arm_listeners(
                listeners, _listener_lifetime(hw, interrupt_wait)
            )
            try:
                # Completed delivery per direction on the healthy link.
                leg1 = await _run_leg(
                    run,
                    hw,
                    senders,
                    armed,
                    transports[0],
                    transports[1],
                    _nonce(),
                    timeline,
                )
                leg2 = await _run_leg(
                    run,
                    hw,
                    senders,
                    armed,
                    transports[1],
                    transports[0],
                    _nonce(),
                    timeline,
                )
                _leg_state(run, leg1, transports[1])
                _leg_state(run, leg2, transports[0])
                result["legs"] += [leg1, leg2]
                state["completed_legs"] = [leg1, leg2]
                _write_state(run, state)

                # Declared-link interruption (separate watcher/operator action).
                result["interrupt"] = await _interrupt_stage(
                    app, run, hw, interrupt_wait, timeline
                )

                # Fresh markers on the rejoined link.
                leg3 = await _run_leg(
                    run,
                    hw,
                    senders,
                    armed,
                    transports[0],
                    transports[1],
                    _nonce(),
                    timeline,
                )
                leg4 = await _run_leg(
                    run,
                    hw,
                    senders,
                    armed,
                    transports[1],
                    transports[0],
                    _nonce(),
                    timeline,
                )
                _leg_state(run, leg3, transports[1])
                _leg_state(run, leg4, transports[0])
                result["legs"] += [leg3, leg4]
                result["stale_backlog"] = _stale_backlog_observation(
                    run, result["legs"]
                )
                state["completed_legs"] = result["legs"]
                _write_state(run, state)

                # Reconcilable pending capture before shutdown.
                result["pending_capture"] = await _pending_capture(
                    run, hw, senders, transports, timeline
                )
                state["pending_capture"] = result["pending_capture"]
                _write_state(run, state)

                # Owner stop precedes peer-listener teardown on purpose: the
                # pending capture's truthfulness depends on stopping the
                # owner inside the destination adapter's pre-send pacing
                # window, and the independent peer listeners are not MEDRE
                # observers. Teardown remains bounded either way.
                await _teardown_owner(app, result)
                app = None
                timeline.append({"stage": "owner-stopped", "at": _now()})
            finally:
                await _disarm_listeners(armed)
                armed = {}
            result["timeline"] = timeline
            result["pass"] = (
                all(leg.get("observed_once") for leg in result["legs"])
                and result["interrupt"].get("rejoined") is True
                and result["pending_capture"].get("captured") is True
            )
    except BaseException as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[-1600:]
        result["timeline"] = timeline
        if armed:
            try:
                await _disarm_listeners(armed)
            except BaseException:
                pass
        if app is not None:
            try:
                await asyncio.wait_for(app.stop(), _STOP_BUDGET_SECONDS)
            except BaseException:
                pass
            result["stop_on_error"] = True
    finally:
        state["baseline_finished_at"] = _now()
        _write_state(run, state)
        (run / "baseline-verdict.json").write_text(
            json.dumps(result, indent=2, default=str) + "\n"
        )
    return bool(result.get("pass"))


async def _interrupt_stage(
    app, run: Path, hw: dict, interrupt_wait: float, timeline: list
) -> dict:
    """Wait for the declared-link disconnect, then for the rejoin.

    The disconnect itself is a separate action: the CLI watcher or the
    operator performs it while this stage polls real adapter diagnostics.
    The stage arms a sentinel file the watcher can look for, and times out
    honestly if nobody acts.
    """
    meshcore = app.adapters.get("meshcore")
    if meshcore is None:
        raise RuntimeError("continuity interrupt stage requires the meshcore adapter")
    sentinel = run / "interrupt-armed.json"
    owned_address = os.environ.get("MEDRE_LAB_OWNED_MESHCORE_ADDRESS", "")
    sentinel.write_text(
        json.dumps(
            {
                "instruction": "disconnect the MEDRE-owned MeshCore host link",
                "owned_address": owned_address,
                "suggested_command": (
                    f"bluetoothctl disconnect {owned_address}"
                    if owned_address
                    else None
                ),
            },
            indent=2,
        )
        + "\n"
    )
    evidence: dict = {"owned_address": owned_address}
    try:
        if _session_diag(meshcore).get("connected") is not True:
            raise RuntimeError("meshcore session not connected before interrupt stage")

        diag_before = meshcore.diagnostics()
        last_inbound_before = _session_diag(meshcore).get("last_message_time")
        ok, waited = await _wait_for(
            lambda: _session_diag(meshcore).get("connected") is False,
            interrupt_wait,
        )
        if not ok:
            raise RuntimeError(
                f"declared link was not disconnected within {interrupt_wait:g}s"
            )
        evidence["disconnect_observed_after_s"] = round(waited, 3)
        evidence["disconnect_diagnostics"] = _session_diag(meshcore)
        evidence["last_inbound_before"] = str(last_inbound_before)
        timeline.append({"stage": "link-disconnected", "at": _now()})

        # Rejoin completion: connected again with the recovery loop finished.
        # The session resets its reconnect-attempt counter to zero on success,
        # so the counter cannot prove a rejoin; the peak observed counter is
        # recorded as evidence that the recovery loop ran, never asserted.
        peak_attempts = 0

        def rejoin_complete() -> bool:
            nonlocal peak_attempts
            diag = _session_diag(meshcore)
            try:
                peak_attempts = max(
                    peak_attempts, int(diag.get("reconnect_attempts") or 0)
                )
            except (TypeError, ValueError):
                pass
            return diag.get("connected") is True and diag.get("reconnecting") is False

        ok, waited = await _wait_for(
            rejoin_complete,
            hw["startup_timeout"] + 300.0,
        )
        if not ok:
            raise RuntimeError("meshcore session did not rejoin after the disconnect")
        evidence["rejoin_observed_after_s"] = round(waited, 3)
        evidence["rejoin_diagnostics"] = _session_diag(meshcore)
        evidence["rejoin_peak_reconnect_attempts"] = peak_attempts
        evidence["rejoined"] = True
        evidence["relayed_before"] = diag_before.get("classifier_packets_relayed")
        timeline.append({"stage": "link-rejoined", "at": _now()})
    finally:
        sentinel.unlink(missing_ok=True)
    return evidence


def _stale_backlog_observation(run: Path, legs: list) -> dict:
    """Observe whether pre-disconnect markers were replayed after rejoin.

    Suppression is asserted only for markers actually re-observed at the
    boundary; a lossy or quiet link may legitimately replay nothing.
    """
    db = run / "state.sqlite"
    detail = []
    for leg in legs:
        snapshot = capture_nonce_state(db, leg["nonce"])
        event_count = len(snapshot["events"])
        if event_count != 1:
            detail.append({"nonce": leg["nonce"], "canonical_events": event_count})
    return {
        "replayed_markers_observed": bool(detail),
        "detail": detail,
        "assertion": (
            "duplicate canonical events per nonce would indicate unsuppressed "
            "backlog replay; none is asserted without an observed replay"
        ),
    }


def _sender_for_destination(transports: tuple[str, str], destination: str) -> str:
    """The independent sender whose MEDRE route terminates at *destination*."""
    others = [t for t in transports if t != destination]
    if len(others) != 1:
        raise ValueError(f"no single sender for destination {destination}")
    return others[0]


def _has_pending_outbox(capture: dict) -> bool:
    return any(
        row["status"] in _PENDING_STATUSES
        for event in capture["events"]
        for row in event["outbox"]
    )


async def _pending_capture(
    run: Path, hw: dict, senders: dict, transports: tuple[str, str], timeline: list
) -> dict:
    """Create and capture one reconcilable pending case before shutdown.

    Two burst messages are sent toward the MeshCore destination back to
    back. The MeshCore session's serialized pacing sleeps before the native
    send boundary while holding its send lock, so the first burst message is
    claimed but pre-handoff, and the second lands in the outbox as genuine
    pending work (the dispatcher never claims rows behind an active send
    for the same adapter). Both durable states are captured while the owner
    is still alive; the disposition authority after reopen is core's own
    replay contract.
    """
    if "meshcore" not in transports:
        raise ValueError("pending capture requires the meshcore transport")
    db = run / "state.sqlite"
    destination = "meshcore"
    source = _sender_for_destination(transports, destination)
    burst_uncertain = _nonce()
    burst_pending = _nonce()

    sent_first = await asyncio.to_thread(senders[source], burst_uncertain)
    ok, _ = await _wait_for(
        lambda: len(capture_nonce_state(db, burst_uncertain)["events"]) > 0, 15.0
    )
    head_state = capture_nonce_state(db, burst_uncertain)
    if len(head_state["events"]) == 0:
        raise AssertionError("burst head was never ingested by the owner")

    sent_second = await asyncio.to_thread(senders[source], burst_pending)
    ok, _ = await _wait_for(
        lambda: _has_pending_outbox(capture_nonce_state(db, burst_pending)),
        hw["observation_timeout"],
        _DB_POLL_INTERVAL,
    )
    pending_state = capture_nonce_state(db, burst_pending)
    if not _has_pending_outbox(pending_state):
        raise AssertionError(
            "pending burst was not observed pending before shutdown; "
            f"state: {json.dumps(pending_state)[:400]}"
        )
    timeline.append(
        {
            "stage": "pending-captured",
            "at": _now(),
            "pending_statuses": _outbox_statuses(pending_state),
            "head_statuses": _outbox_statuses(head_state),
        }
    )
    return {
        "captured": True,
        "destination": destination,
        "pending": {
            "nonce": burst_pending,
            "native_send": sent_second,
            "state": pending_state,
        },
        "uncertain": {
            "nonce": burst_uncertain,
            "native_send": sent_first,
            "state": head_state,
        },
    }


async def run_reopen(config: dict, selection: str, role: int, run: Path) -> bool:
    """Fresh-process reopen phase on the same private campaign state."""
    transports = pair_selection(selection)
    hw = config["hardware"]
    timeline: list = [{"stage": "reopen-start", "at": _now(), "pid": os.getpid()}]
    result: dict = {
        "phase": "reopen",
        "pair": list(transports),
        "role": "a" if role == 0 else "b",
        "pass": False,
        "legs": [],
        "persisted": {},
        "disposition": {},
        "packages": {},
        "scope": "Fresh-process reopen of the baseline campaign state.",
    }
    app = None
    armed: dict = {}
    state = _read_state(run)
    db = run / "state.sqlite"
    try:
        from .worker import configure, provenance, wait_startup_drain_retired

        if "pending_capture" not in state or "completed_legs" not in state:
            raise RuntimeError(
                "reopen requires a baseline state file with the captured "
                "pending case and completed legs; baseline did not record one"
            )
        result["packages"] = provenance(transports)
        pending_nonce = state["pending_capture"]["pending"]["nonce"]
        uncertain_nonce = state["pending_capture"]["uncertain"]["nonce"]
        completed = state["completed_legs"]

        listeners, senders, destinations, endpoints = configure(
            config, transports, role, run
        )
        with own(endpoints), own_run(run):
            result["process"] = {"pid": os.getpid()}
            # Persisted-state verification happens while no owner lives and
            # before any new send.
            before = {
                leg["nonce"]: capture_nonce_state(db, leg["nonce"]) for leg in completed
            }
            before[pending_nonce] = capture_nonce_state(db, pending_nonce)
            before[uncertain_nonce] = capture_nonce_state(db, uncertain_nonce)
            persisted_ok = True
            for leg in completed:
                snapshot = before[leg["nonce"]]
                if (
                    _sent_receipt_event_id(snapshot, leg["destination"])
                    != leg["event_id"]
                ):
                    persisted_ok = False
            result["persisted"] = {
                "completed_identities_retained": persisted_ok,
                "pending_before": before[pending_nonce],
                "uncertain_before": before[uncertain_nonce],
            }
            timeline.append(
                {"stage": "persisted-verified", "at": _now(), "ok": persisted_ok}
            )
            if not persisted_ok:
                raise AssertionError("completed sibling identities did not persist")

            # The pending case re-dispatches shortly after start; the peer
            # listeners must already be armed when that fresh native send
            # happens. MeshCore exposes no startup drain, and the drain wait
            # below still precedes the first leg send.
            armed = await _arm_listeners(listeners, _listener_lifetime(hw, 0.0))
            try:
                app = build(
                    config,
                    transports,
                    role,
                    run,
                    destinations,
                    message_delay_seconds=CONTINUITY_MESSAGE_DELAY_SECONDS,
                )
                await asyncio.wait_for(app.start(), hw["startup_timeout"])
                for adapter in app.adapters.values():
                    health = await asyncio.wait_for(adapter.health_check(), 15)
                    if health.health != "healthy":
                        raise RuntimeError(
                            "adapter did not report healthy after reopen"
                        )
                result["startup_drain_wait"] = await wait_startup_drain_retired(app)
                timeline.append({"stage": "owner-restarted", "at": _now()})

                result["disposition"] = await _disposition(
                    run,
                    hw,
                    armed,
                    transports,
                    pending_nonce,
                    uncertain_nonce,
                    timeline,
                )
                # Functioning peer observation after reopen.
                leg7 = await _run_leg(
                    run,
                    hw,
                    senders,
                    armed,
                    transports[0],
                    transports[1],
                    _nonce(),
                    timeline,
                )
                _leg_state(run, leg7, transports[1])
                result["legs"].append(leg7)

                await _teardown_owner(app, result)
                app = None
                timeline.append({"stage": "owner-restopped", "at": _now()})
            finally:
                await _disarm_listeners(armed)
                armed = {}
            result["pending_after"] = capture_nonce_state(db, pending_nonce)
            result["uncertain_after"] = capture_nonce_state(db, uncertain_nonce)
            result["timeline"] = timeline
            result["pass"] = result["disposition"].get(
                "pending_reconciled"
            ) is True and all(leg.get("observed_once") for leg in result["legs"])
    except BaseException as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[-1600:]
        result["timeline"] = timeline
        if armed:
            try:
                await _disarm_listeners(armed)
            except BaseException:
                pass
        if app is not None:
            try:
                await asyncio.wait_for(app.stop(), _STOP_BUDGET_SECONDS)
            except BaseException:
                pass
            result["stop_on_error"] = True
    finally:
        state["reopen_finished_at"] = _now()
        _write_state(run, state)
        (run / "reopen-verdict.json").write_text(
            json.dumps(result, indent=2, default=str) + "\n"
        )
    return bool(result.get("pass"))


async def _disposition(
    run: Path,
    hw: dict,
    armed: dict,
    transports: tuple[str, str],
    pending_nonce: str,
    uncertain_nonce: str,
    timeline: list,
) -> dict:
    """Record the actual post-reopen disposition of the captured work.

    ``pending`` was never claimed at stop: proven unfinished, authorized
    initial work. Core's dispatcher may claim and deliver it; the case
    waits for that progression and requires independent observation of the
    fresh native send. ``uncertain`` was claimed but interrupted before its
    native send boundary: whether core progresses it is core's own replay
    authority, and both outcomes are recorded without assertion.
    """
    db = run / "state.sqlite"
    destination = "meshcore"
    receiver = armed[destination]["peer"]

    ok, waited = await _wait_for(
        lambda: _sent_receipt_event_id(
            capture_nonce_state(db, pending_nonce), destination
        )
        is not None,
        RECONCILE_WAIT_SECONDS,
        _DB_POLL_INTERVAL,
    )
    pending_reconciled = ok
    pending_after = capture_nonce_state(db, pending_nonce)
    if pending_reconciled:
        observed = await asyncio.to_thread(
            receiver.packets_until,
            lambda ps: any(
                pending_nonce in str(p.get("text", p.get("content", ""))) for p in ps
            ),
            hw["observation_timeout"],
        )
        seen = [
            p
            for p in observed
            if pending_nonce in str(p.get("text", p.get("content", "")))
        ]
        pending_reconciled = (
            len(seen) == 1
            and str(seen[0].get("text", seen[0].get("content", ""))).count(
                pending_nonce
            )
            == 1
        )
    timeline.append(
        {
            "stage": "pending-disposition",
            "at": _now(),
            "reconciled": pending_reconciled,
            "wait_seconds": round(waited, 3),
        }
    )

    # Let the uncertain case settle into whatever terminal or retained state
    # core's replay authority assigns, then record it without assertion.
    await _wait_for(
        lambda: _uncertain_settled(capture_nonce_state(db, uncertain_nonce)),
        UNCERTAIN_SETTLE_WAIT_SECONDS,
        _DB_POLL_INTERVAL,
    )
    uncertain_after = capture_nonce_state(db, uncertain_nonce)
    return {
        "pending_reconciled": pending_reconciled,
        "pending_after": pending_after,
        "uncertain_after": uncertain_after,
        "uncertain_progressed": _sent_receipt_event_id(uncertain_after, destination)
        is not None,
        "uncertain_final_statuses": _outbox_statuses(uncertain_after),
    }


def _uncertain_settled(capture: dict) -> bool:
    """True once no outbox row is still mid-flight (claimed/queued)."""
    statuses = _outbox_statuses(capture)
    if not statuses:
        return False
    return all(status not in ("in_progress", "queued") for status in statuses)
