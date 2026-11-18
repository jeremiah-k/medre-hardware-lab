"""Opt-in continuity case: declared-link interruption and MEDRE process reopen.

This module extends the existing pair runner with an explicit, separately
invoked continuity case. It never changes the ordinary ``hardware run``
short-check semantics. The case runs in two owned worker processes:

``baseline``
    One MEDRE runtime process completes one delivery per direction, then
    waits for the declared host link to be disconnected (MeshCore BLE only:
    ``bluetoothctl disconnect`` of the MEDRE-owned board, performed by the
    lock-owning worker or the operator), records actual disconnect/rejoin evidence,
    sends fresh markers on the rejoined link, and finally captures a
    reconcilable pending outbox case before a bounded graceful stop.

``reopen``
    A fresh process reopens the same campaign database and profiles only
    after the baseline owner retired (enforced by a run lock), verifies the
    persisted identities, records the disposition of the captured pending
    and uncertain work, and proves peer observation still functions with a
    fresh marker per direction.

Physical interruption and MEDRE shutdown are separate, individually
evidenced actions. The interrupt stage polls real adapter diagnostics and
times out honestly; nothing tunes deadlines to obtain a pass. The pending
capture uses the lab runtime's real send pacing: the burst tail is released
only after the burst head's claimed, reserved, pre-handoff outbox row is
observed, and the capture then polls durable state concurrently with the
still-running tail send, so the tail lands in the outbox while the head
still holds the MeshCore session's serialized pre-send pacing even when the
independent sender spends seconds on its own teardown. At stop one burst
message is pending (never claimed, no native handoff evidence) and the
other is genuinely uncertain (claimed, pacing interrupted before the native
send boundary). Core's own replay authority decides their post-reopen
disposition; the case records what actually happens and asserts only
against the committed contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

from .config import pair as pair_selection
from .native.live_peer_common import async_peer_context, owned_blocking
from .ownership import own, own_run
from .runtime import build

#: Send pacing used while the continuity runtime is assembled. The real
#: adapter pacing (sleep before the native send, serialized per session)
#: creates the bounded pre-handoff window the pending capture needs; the
#: ordinary short check keeps its own 2.5 s value. The continuity value is
#: sized so the window outlasts the burst tail's measured route ingestion
#: (~7-11 s over the two-transport link) plus the stop's own bounded
#: teardown (drain abandonment plus adapter/pipeline shutdown), which is
#: what keeps the captured head mid-window until the stop cancels it.
CONTINUITY_MESSAGE_DELAY_SECONDS = 24.0

#: How long the baseline waits for someone to disconnect the declared link.
DEFAULT_INTERRUPT_WAIT_SECONDS = 300.0

#: Bounded post-reopen wait for the captured pending case to progress.
RECONCILE_WAIT_SECONDS = 180.0

#: Bounded post-reopen wait for the captured uncertain case to settle.
UNCERTAIN_SETTLE_WAIT_SECONDS = 120.0

_STOP_BUDGET_SECONDS = 30.0

#: Bounded in-flight drain for the owner stop. Core's shutdown drain waits
#: for in-flight deliveries to complete (up to its configured limit), which
#: would finish the captured burst head's native send during the stop and
#: erase the uncertain state the case preserves. The bounded deadline ends
#: that wait while the burst head is still mid-window, so the pipeline
#: teardown cancels it claimed and pre-handoff, resumable after reopen.
_STOP_DRAIN_DEADLINE_SECONDS = 1.0

#: Bounded post-stop window in which SDK children may finish their own
#: asynchronous retirement (e.g. the LXMF native worker's SDK/RNS
#: teardown) before the leak measurement reports survivors.
_SDK_CHILD_RETIRE_WAIT_SECONDS = 5.0
_DB_POLL_INTERVAL = 0.2

#: Bounded wait for the burst head's claimed, reserved, pre-handoff boundary.
_HEAD_BOUNDARY_WAIT_SECONDS = 15.0

_PENDING_STATUSES = ("pending", "retry_wait")


def _nonce() -> str:
    return "MLAB-" + uuid.uuid4().hex[:12]


def _now() -> float:
    return time.time()


def _config_fingerprint(config: dict) -> str:
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_state(run: Path) -> dict:
    path = run / "continuity-state.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2, default=str) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_state(run: Path, state: dict) -> None:
    _write_json(run / "continuity-state.json", state)


def capture_nonce_state(db: Path, nonce: str) -> dict:
    """Read-only durable-state snapshot for every canonical event with *nonce*.

    Uses a read-only connection (never ``immutable=1`` on a live database)
    so the snapshot cannot disturb the running owner's writer.
    """
    return capture_nonce_states(db, nonce)[0]


def capture_nonce_states(db: Path, *nonces: str) -> list[dict]:
    """Pin one read snapshot across all events and their durable evidence."""
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN")
        return [_capture_nonce_state(con, nonce) for nonce in nonces]
    finally:
        con.close()


def _capture_nonce_state(con: sqlite3.Connection, nonce: str) -> dict:
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
                        "SELECT receipt_id,outbox_id,delivery_plan_id,target_adapter,"
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
                        "target_channel,status,attempt_number,active_attempt,locked_at,lease_until,"
                        "worker_id,updated_at FROM delivery_outbox "
                        "WHERE event_id=? ORDER BY created_at,outbox_id",
                        (event_id,),
                    ).fetchall()
                ],
            }
        )
        event = out["events"][-1]
        for row in event["outbox"]:
            row["radio_attempts"] = [
                {
                    "attempt_number": progress["attempt_number"],
                    "parts": json.loads(progress["parts_json"]),
                }
                for progress in con.execute(
                    "SELECT attempt_number,parts_json FROM radio_attempt_progress WHERE outbox_id=? ORDER BY attempt_number",
                    (row["outbox_id"],),
                )
            ]
    return out


def _sent_receipt_event_id(
    snapshot: dict, destination: str, *, outbox_id: str | None = None
) -> str | None:
    """Return the canonical event id with one failure-free sent receipt."""
    if len(snapshot["events"]) != 1:
        return None
    event = snapshot["events"][0]
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
    row = _single_destination_row(snapshot, destination)
    if len(sent) != 1 or bad or row is None or row["status"] != "sent":
        return None
    receipt = sent[0]
    if outbox_id is not None and row["outbox_id"] != outbox_id:
        return None
    if any(
        receipt[key] != row[key]
        for key in ("outbox_id", "delivery_plan_id", "target_channel", "attempt_number")
    ):
        return None
    return event["event_id"]


def _sent_receipt_identity(snapshot: dict, destination: str) -> tuple[str, str] | None:
    event_id = _sent_receipt_event_id(snapshot, destination)
    if event_id is None:
        return None
    receipt = next(
        r
        for r in snapshot["events"][0]["receipts"]
        if r["receipt_kind"] == "attempt"
        and r["status"] == "sent"
        and r["target_adapter"] == destination
    )
    return event_id, receipt["receipt_id"]


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
    """Arm the selected independent recipient(s), owning partial setup."""
    armed: dict = {}
    try:
        for kind, factory in listeners.items():
            cm = async_peer_context(factory(lifetime))
            peer = await cm.__aenter__()
            armed[kind] = {"cm": cm, "peer": peer}
        return armed
    except BaseException:
        await _disarm_listeners(armed)
        raise


async def _disarm_listeners(armed: dict) -> None:
    async with AsyncExitStack() as stack:
        for kind in list(armed):
            entry = armed.pop(kind)
            stack.push_async_callback(entry["cm"].__aexit__, None, None, None)


async def _select_listener(
    armed: dict, listeners: dict, destination: str, lifetime: float
) -> None:
    """Never contend with a sender for the same serial/BLE/RNS endpoint."""
    if set(armed) == {destination}:
        return
    await _disarm_listeners(armed)
    armed.update(await _arm_listeners({destination: listeners[destination]}, lifetime))


def _observe_window(peer, timeout: float) -> list[dict]:
    # Native peers return early on a true predicate. Keep the complete
    # declared window to detect delayed duplicates; a dead observer cannot
    # prove absence, even if it collected one packet before exiting.
    packets = peer.packets_until(lambda _: False, timeout)
    owner = getattr(peer, "_owner", None)
    if owner is not None and owner.poll() is not None:
        raise AssertionError("native observer retired before observation completed")
    return packets


def _disconnect_owned(run: Path, address: str, controller: str | None) -> None:
    """Perform the fault action inside the worker that owns the endpoints."""
    if not address:
        raise ValueError("owned MeshCore address is missing")
    argv = (
        [
            "busctl",
            "call",
            "org.bluez",
            f"/org/bluez/{controller}/dev_{address.upper().replace(':', '_')}",
            "org.bluez.Device1",
            "Disconnect",
        ]
        if controller
        else ["bluetoothctl", "disconnect", address]
    )
    with (run / "disconnect-action.log").open("w") as log:
        result = subprocess.run(
            argv, stdout=log, stderr=subprocess.STDOUT, timeout=30, check=False
        )
        log.write(f"\nexit={result.returncode}\n")
    if result.returncode != 0:
        raise RuntimeError(f"owned BLE disconnect failed ({result.returncode})")


async def _run_leg(
    run: Path,
    hw: dict,
    senders: dict,
    armed: dict,
    listeners: dict,
    source: str,
    destination: str,
    nonce: str,
    timeline: list,
) -> dict:
    """One native send from the independent peer plus far-side observation."""
    await _select_listener(armed, listeners, destination, _listener_lifetime(hw, 0))
    receiver = armed[destination]["peer"]
    before = time.monotonic()
    sent = await owned_blocking(senders[source], nonce)
    observed = await owned_blocking(
        _observe_window,
        receiver,
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


async def _leg_state(run: Path, leg: dict, destination: str, timeout: float) -> dict:
    """Attach the durable DB identity of one completed leg."""
    snapshot = {}

    def committed() -> bool:
        nonlocal snapshot
        snapshot = capture_nonce_state(run / "state.sqlite", leg["nonce"])
        return _sent_receipt_event_id(snapshot, destination) is not None

    await _wait_for(committed, timeout, _DB_POLL_INTERVAL)
    identity = _sent_receipt_identity(snapshot, destination)
    event_id = identity[0] if identity else None
    if event_id is None:
        raise AssertionError(
            f"{leg['nonce']} lacks one failure-free sent receipt at {destination}"
        )
    leg.update(event_id=event_id, receipt_id=identity[1], durable=snapshot)
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


def _cmdline(pid: int) -> str:
    """Read one process command line for survivor evidence; never raises."""
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()[:200]
    except OSError:
        return ""


async def _teardown_owner(app, result: dict, armed: dict) -> None:
    """Bounded stop of the MEDRE owner plus SDK-child teardown evidence.

    The stop passes a bounded in-flight drain deadline: core's default
    shutdown drain waits for in-flight deliveries to complete, which would
    finish the captured burst head's native send during the stop instead of
    freezing it claimed and pre-handoff. With the bounded deadline the stop
    cancels that delivery mid-window and leaves the row resumable for the
    reopen phase. SDK children may retire asynchronously shortly after
    ``stop()`` returns (the LXMF native worker finishes its own SDK/RNS
    teardown), so the leak measurement gives that retirement a bounded,
    recorded window before reporting survivors with their command lines.
    """
    if app is None:
        return
    peer_pids = {
        owner.proc.pid
        for entry in armed.values()
        if (owner := getattr(entry["peer"], "_owner", None)) is not None
        and owner.proc is not None
    }
    started = time.monotonic()
    await asyncio.wait_for(
        app.stop(drain_deadline=time.monotonic() + _STOP_DRAIN_DEADLINE_SECONDS),
        _STOP_BUDGET_SECONDS,
    )
    result["stop_seconds"] = round(time.monotonic() - started, 3)
    result["stop_drain_deadline_seconds"] = _STOP_DRAIN_DEADLINE_SECONDS
    retire_started = time.monotonic()
    survivors: list[int] = []
    while True:
        survivors = sorted(set(_child_pids()) - peer_pids)
        if not survivors or time.monotonic() - retire_started >= (
            _SDK_CHILD_RETIRE_WAIT_SECONDS
        ):
            break
        await asyncio.sleep(0.2)
    result["sdk_children_retire_wait_seconds"] = round(
        time.monotonic() - retire_started, 3
    )
    result["surviving_sdk_children"] = survivors
    result["surviving_sdk_children_cmdlines"] = {
        str(pid): _cmdline(pid) for pid in survivors
    }


async def run_baseline(
    config: dict,
    selection: str,
    role: int,
    run: Path,
    interrupt_wait: float,
    *,
    auto_disconnect: bool = False,
) -> bool:
    """Baseline owner phase: deliveries, link interruption, pending capture."""
    from .worker import configure

    if not math.isfinite(interrupt_wait) or interrupt_wait <= 0:
        raise ValueError("interrupt_wait must be finite and positive")
    with own_run(run):
        if (run / "continuity-state.json").exists():
            raise RuntimeError("baseline requires a fresh run directory")
        configured = configure(config, pair_selection(selection), role, run)
        with own(configured[3]):
            return await _run_baseline_owned(
                config,
                selection,
                role,
                run,
                interrupt_wait,
                configured,
                auto_disconnect=auto_disconnect,
            )


async def _run_baseline_owned(
    config, selection, role, run, interrupt_wait, configured, *, auto_disconnect=False
) -> bool:

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
    tail_send: "asyncio.Future[dict] | None" = None
    state = _read_state(run)
    state.update(
        phase="baseline",
        pair=list(transports),
        role=result["role"],
        message_delay_seconds=CONTINUITY_MESSAGE_DELAY_SECONDS,
        config_sha256=_config_fingerprint(config),
        started_at=_now(),
    )
    _write_state(run, state)
    try:
        from .worker import provenance, wait_startup_drain_retired

        result["packages"] = provenance(transports)
        listeners, senders, destinations, _ = configured
        if "meshcore" in hw:
            os.environ["MEDRE_LAB_OWNED_MESHCORE_ADDRESS"] = hw["meshcore"][
                "addresses"
            ][role]
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
            {transports[1]: listeners[transports[1]]},
            _listener_lifetime(hw, interrupt_wait),
        )
        try:
            # Completed delivery per direction on the healthy link.
            leg1 = await _run_leg(
                run,
                hw,
                senders,
                armed,
                listeners,
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
                listeners,
                transports[1],
                transports[0],
                _nonce(),
                timeline,
            )
            await _leg_state(run, leg1, transports[1], hw["observation_timeout"])
            await _leg_state(run, leg2, transports[0], hw["observation_timeout"])
            result["legs"] += [leg1, leg2]
            state["completed_legs"] = [leg1, leg2]
            _write_state(run, state)

            # Declared-link interruption (owned action or manual operator).
            await _disarm_listeners(armed)
            result["interrupt"] = await _interrupt_stage(
                app, run, hw, interrupt_wait, timeline, auto_disconnect=auto_disconnect
            )

            # Fresh markers on the rejoined link.
            leg3 = await _run_leg(
                run,
                hw,
                senders,
                armed,
                listeners,
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
                listeners,
                transports[1],
                transports[0],
                _nonce(),
                timeline,
            )
            await _leg_state(run, leg3, transports[1], hw["observation_timeout"])
            await _leg_state(run, leg4, transports[0], hw["observation_timeout"])
            result["legs"] += [leg3, leg4]
            result["stale_backlog"] = _stale_backlog_observation(run, [leg1, leg2])
            state["completed_legs"] = result["legs"]
            _write_state(run, state)

            # Reconcilable pending capture before shutdown.
            await _select_listener(
                armed, listeners, "meshcore", _listener_lifetime(hw, interrupt_wait)
            )
            result["pending_capture"], tail_send = await _pending_capture(
                run, hw, senders, transports, timeline
            )
            state["pending_capture"] = result["pending_capture"]
            _write_state(run, state)

            # Owner stop precedes peer-listener teardown on purpose: the
            # pending capture's truthfulness depends on stopping the
            # owner inside the destination adapter's pre-send pacing
            # window, and the independent peer listeners are not MEDRE
            # observers. Teardown remains bounded either way.
            await _teardown_owner(app, result, armed)
            app = None
            # The stop froze the burst rows; the independent tail sender may
            # still be finishing its own teardown. Collect its evidence now —
            # the capture itself never waited for the sender to retire.
            result["pending_capture"]["pending"]["native_send"] = await tail_send
            tail_send = None
            _stale_backlog_observation(run, result["legs"])
            _verify_stopped_capture(run, result["pending_capture"])
            state["pending_capture"] = result["pending_capture"]
            _write_state(run, state)
            timeline.append({"stage": "owner-stopped", "at": _now()})
        finally:
            if app is not None:
                try:
                    await _teardown_owner(app, result, armed)
                    app = None
                    result["stop_on_error"] = True
                finally:
                    await _disarm_listeners(armed)
            else:
                await _disarm_listeners(armed)
        result["timeline"] = timeline
        result["pass"] = (
            all(leg.get("observed_once") for leg in result["legs"])
            and result["interrupt"].get("rejoined") is True
            and result["pending_capture"].get("captured") is True
            and not result.get("surviving_sdk_children")
            and not _child_pids()
        )
    except BaseException as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[-1600:]
        result["cancelled"] = isinstance(exc, asyncio.CancelledError)
        result["timeline"] = timeline
        if tail_send is not None and not tail_send.done():
            # The burst capture released the tail but never reached its
            # collector; retire the owned send before releasing endpoints.
            await _drain_send(tail_send)
            tail_send = None
        try:
            if app is not None:
                await _teardown_owner(app, result, armed)
                result["stop_on_error"] = True
        except BaseException as cleanup_error:
            result["cleanup_error"] = (
                type(cleanup_error).__name__ + ": " + str(cleanup_error)[-800:]
            )
        finally:
            try:
                await _disarm_listeners(armed)
            except BaseException as cleanup_error:
                result["peer_cleanup_error"] = (
                    type(cleanup_error).__name__ + ": " + str(cleanup_error)[-800:]
                )
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        state["baseline_finished_at"] = _now()
        _write_state(run, state)
        _write_json(run / "baseline-verdict.json", result)
    return bool(result.get("pass"))


async def _interrupt_stage(
    app,
    run: Path,
    hw: dict,
    interrupt_wait: float,
    timeline: list,
    *,
    auto_disconnect: bool = False,
) -> dict:
    """Wait for the declared-link disconnect, then for the rejoin.

    The owned worker performs the automatic action concurrently with polling,
    or the operator acts on the sentinel in manual mode. The action is drained
    before leaving the endpoint-ownership scope.
    """
    meshcore = app.adapters.get("meshcore")
    if meshcore is None:
        raise RuntimeError("continuity interrupt stage requires the meshcore adapter")
    sentinel = run / "interrupt-armed.json"
    owned_address = os.environ.get("MEDRE_LAB_OWNED_MESHCORE_ADDRESS", "")
    diag_before = meshcore.diagnostics()
    session_before = diag_before.get("session", {})
    if session_before.get("connected") is not True:
        raise RuntimeError("meshcore session not connected before interrupt stage")
    _write_json(
        sentinel,
        {
            "instruction": "disconnect the MEDRE-owned MeshCore host link",
            "owned_address": owned_address,
            "controller": hw.get("ble_adapter"),
            "instruction_detail": "Use the selected controller and disconnect only the owned link.",
        },
    )
    evidence: dict = {"owned_address": owned_address}
    action = None
    try:
        if auto_disconnect:
            action = asyncio.create_task(
                owned_blocking(
                    _disconnect_owned, run, owned_address, hw.get("ble_adapter")
                )
            )
        last_inbound_before = session_before.get("last_message_time")
        evidence["pre_interrupt_diagnostics"] = session_before
        disconnected = {}

        def disconnected_now():
            nonlocal disconnected
            if action is not None and action.done():
                action.result()
            disconnected = _session_diag(meshcore)
            return disconnected.get("connected") is False

        ok, waited = await _wait_for(
            disconnected_now,
            interrupt_wait,
        )
        if not ok:
            raise RuntimeError(
                f"declared link was not disconnected within {interrupt_wait:g}s"
            )
        evidence["disconnect_observed_after_s"] = round(waited, 3)
        evidence["disconnect_diagnostics"] = disconnected
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
        if action is not None:
            await asyncio.shield(action)
        evidence["rejoin_observed_after_s"] = round(waited, 3)
        evidence["rejoin_diagnostics"] = _session_diag(meshcore)
        evidence["rejoin_peak_reconnect_attempts"] = peak_attempts
        evidence["rejoined"] = True
        evidence["relayed_before"] = diag_before.get("classifier_packets_relayed")
        timeline.append({"stage": "link-rejoined", "at": _now()})
    except BaseException:
        if action is not None:
            # Preserve the original failure/cancellation, but do not detach
            # the fault action from its endpoint owner.
            action.cancel()
            try:
                await action
            except (asyncio.CancelledError, Exception):
                pass
        raise
    finally:
        sentinel.unlink(missing_ok=True)
    return evidence


def _stale_backlog_observation(run: Path, legs: list) -> dict:
    """Detect duplicate durable effects; no claim of ingress re-observation."""
    db = run / "state.sqlite"
    detail = []
    for leg in legs:
        snapshot = capture_nonce_state(db, leg["nonce"])
        if snapshot != leg["durable"]:
            detail.append({"nonce": leg["nonce"], "durable_state_changed": True})
    if detail:
        raise AssertionError(f"pre-interrupt durable marker state changed: {detail}")
    return {
        "ingress_reobservation": "not_instrumented",
        "duplicate_durable_effects": False,
        "checked_markers": len(legs),
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


def _single_destination_row(snapshot: dict, destination: str) -> dict | None:
    if len(snapshot["events"]) != 1:
        return None
    rows = [
        row
        for row in snapshot["events"][0]["outbox"]
        if row["target_adapter"] == destination
    ]
    return rows[0] if len(rows) == 1 else None


def _pre_handoff(row: dict) -> bool:
    attempts = row["radio_attempts"]
    return (
        len(attempts) == 1
        and attempts[0]["attempt_number"] == 1
        and bool(attempts[0]["parts"])
        and all(
            part["state"] == "unsent" and part["handoff_id"] is None
            for part in attempts[0]["parts"]
        )
    )


def _never_claimed(row: dict) -> bool:
    return (
        row["status"] == "pending"
        and row["attempt_number"] == 0
        and row["active_attempt"] is None
        and row["locked_at"] is None
        and row["worker_id"] is None
        and row["lease_until"] is None
        and not row["radio_attempts"]
    )


def _head_boundary_state(snapshot: dict, destination: str) -> bool:
    """True while the burst head is claimed, reserved and pre-handoff.

    The single destination row must be ``in_progress`` with one active
    attempt reserved and radio progress that has not handed off yet: the
    exact durable shape of a send holding the meshcore adapter's serialized
    pre-send pacing window.
    """
    head = _single_destination_row(snapshot, destination)
    return (
        head is not None
        and head["status"] == "in_progress"
        and head["active_attempt"] == 1
        and head["attempt_number"] == 0
        and _pre_handoff(head)
        and not snapshot["events"][0]["receipts"]
    )


def _tail_pending_state(snapshot: dict, destination: str) -> bool:
    """True while the burst tail is staged pending behind the active head."""
    tail = _single_destination_row(snapshot, destination)
    return (
        tail is not None
        and _never_claimed(tail)
        and not snapshot["events"][0]["receipts"]
    )


async def _drain_send(task: "asyncio.Future[dict]") -> dict | BaseException | None:
    """Retire an owned send on every exit path; never leave it detached.

    Cancels the awaiting side while the native operation drains to
    completion (the same ownership contract as :func:`owned_blocking`).
    Returns the send's result, its exception when it failed, or ``None``
    when it retired cancelled.
    """
    task.cancel()
    try:
        return await task
    except asyncio.CancelledError:
        return None
    except Exception as exc:
        return exc


def _verify_stopped_capture(run: Path, capture: dict) -> None:
    head, tail = capture_nonce_states(
        run / "state.sqlite", capture["uncertain"]["nonce"], capture["pending"]["nonce"]
    )
    original_head = _single_destination_row(capture["uncertain"]["state"], "meshcore")
    original_tail = _single_destination_row(capture["pending"]["state"], "meshcore")
    current_head = _single_destination_row(head, "meshcore")
    current_tail = _single_destination_row(tail, "meshcore")
    if (
        current_head is None
        or current_tail is None
        or original_head is None
        or original_tail is None
        or current_head["outbox_id"] != original_head["outbox_id"]
        or current_tail["outbox_id"] != original_tail["outbox_id"]
        or not _never_claimed(current_tail)
        or current_head["status"] in ("sent", "queued")
        or not _pre_handoff(current_head)
        or any(r["status"] == "sent" for r in head["events"][0]["receipts"])
        or bool(tail["events"][0]["receipts"])
    ):
        raise AssertionError(
            "stopped burst no longer retains the captured head/tail identities and unfinished tail"
        )
    capture["stopped"] = {"uncertain": head, "pending": tail}


async def _pending_capture(
    run: Path, hw: dict, senders: dict, transports: tuple[str, str], timeline: list
) -> tuple[dict, "asyncio.Future[dict] | None"]:
    """Create and capture one reconcilable pending case before shutdown.

    Two burst messages are sent toward the MeshCore destination back to
    back. The MeshCore session's serialized pacing sleeps before the native
    send boundary while holding its send lock, so the first burst message is
    claimed but pre-handoff, and the second lands in the outbox as genuine
    pending work (the dispatcher never claims rows behind an active send
    for the same adapter).

    Both waits are event-driven on durable state. The burst tail is released
    only after the head's claimed, reserved, pre-handoff row is observed,
    and the capture then polls the database concurrently with the
    still-running tail send: the honest boundary (tail ingested while the
    head holds its pacing window) is observable while the independent
    sender is finishing its own teardown, instead of only after the sender
    retires — a sender that returns seconds after delivery must not consume
    the head's pacing window. The caller stops the owner while the capture
    holds and collects the tail sender's evidence afterwards; the tail task
    is drained on every exit path here. Both durable states are captured
    while the owner is still alive; the disposition authority after reopen
    is core's own replay contract.
    """
    if "meshcore" not in transports:
        raise ValueError("pending capture requires the meshcore transport")
    db = run / "state.sqlite"
    destination = "meshcore"
    source = _sender_for_destination(transports, destination)
    burst_uncertain = _nonce()
    burst_pending = _nonce()

    sent_first = await owned_blocking(senders[source], burst_uncertain)

    def head_boundary() -> bool:
        return _head_boundary_state(
            capture_nonce_state(db, burst_uncertain), destination
        )

    ok, waited = await _wait_for(
        head_boundary, _HEAD_BOUNDARY_WAIT_SECONDS, _DB_POLL_INTERVAL
    )
    if not ok:
        raise AssertionError(
            "burst head was never observed claimed and pre-handoff "
            f"(statuses {_outbox_statuses(capture_nonce_state(db, burst_uncertain))})"
        )
    timeline.append(
        {
            "stage": "head-claimed-pre-handoff",
            "at": _now(),
            "wait_seconds": round(waited, 3),
        }
    )

    # Release the tail behind the observed boundary without waiting for the
    # sender process to retire. The owned send is drained on every exit path.
    tail_send = asyncio.create_task(owned_blocking(senders[source], burst_pending))

    head_state, pending_state = {}, {}

    def captured() -> bool:
        nonlocal head_state, pending_state
        head_state, pending_state = capture_nonce_states(
            db, burst_uncertain, burst_pending
        )
        return _head_boundary_state(head_state, destination) and _tail_pending_state(
            pending_state, destination
        )

    try:
        ok, waited = await _wait_for(
            captured, hw["observation_timeout"], _DB_POLL_INTERVAL
        )
    except BaseException:
        await _drain_send(tail_send)
        raise
    if not ok:
        tail_failure = await _drain_send(tail_send)
        detail = "" if tail_failure is None else f"; tail send failed: {tail_failure}"
        raise AssertionError(
            "burst was not observed pending behind an active head "
            f"(head statuses {_outbox_statuses(head_state)}, "
            f"tail statuses {_outbox_statuses(pending_state)}){detail}"
        )
    timeline.append(
        {
            "stage": "pending-captured",
            "at": _now(),
            "wait_seconds": round(waited, 3),
            "pending_statuses": _outbox_statuses(pending_state),
            "head_statuses": _outbox_statuses(head_state),
        }
    )
    capture = {
        "captured": True,
        "destination": destination,
        "pending": {
            "nonce": burst_pending,
            "state": pending_state,
        },
        "uncertain": {
            "nonce": burst_uncertain,
            "native_send": sent_first,
            "state": head_state,
        },
    }
    return capture, tail_send


async def run_reopen(config: dict, selection: str, role: int, run: Path) -> bool:
    """Fresh-process reopen phase on the same private campaign state."""
    from .worker import configure

    with own_run(run):
        configured = configure(config, pair_selection(selection), role, run)
        with own(configured[3]):
            return await _run_reopen_owned(config, selection, role, run, configured)


async def _run_reopen_owned(config, selection, role, run, configured) -> bool:

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
        from .worker import provenance, wait_startup_drain_retired

        if "pending_capture" not in state or "completed_legs" not in state:
            raise RuntimeError(
                "reopen requires a baseline state file with the captured "
                "pending case and completed legs; baseline did not record one"
            )
        baseline = json.loads((run / "baseline-verdict.json").read_text())
        if (
            baseline.get("phase") != "baseline"
            or baseline.get("pass") is not True
            or state.get("pair") != list(transports)
            or state.get("role") != result["role"]
            or state.get("config_sha256") != _config_fingerprint(config)
            or state.get("completed_legs") != baseline.get("legs")
            or state.get("pending_capture") != baseline.get("pending_capture")
        ):
            raise RuntimeError(
                "reopen requires matching successful baseline evidence and configuration"
            )
        result["packages"] = provenance(transports)
        if result["packages"] != baseline.get("packages"):
            raise RuntimeError(
                "installed package provenance changed between continuity phases"
            )
        pending_nonce = state["pending_capture"]["pending"]["nonce"]
        uncertain_nonce = state["pending_capture"]["uncertain"]["nonce"]
        completed = state["completed_legs"]

        listeners, senders, destinations, _ = configured
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
                _sent_receipt_identity(snapshot, leg["destination"])
                != (
                    leg["event_id"],
                    leg["receipt_id"],
                )
                or snapshot != leg["durable"]
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
        stopped_capture = state["pending_capture"].get("stopped")
        if (
            stopped_capture is None
            or before[pending_nonce] != stopped_capture["pending"]
            or before[uncertain_nonce] != stopped_capture["uncertain"]
        ):
            raise AssertionError(
                "stopped burst evidence missing or changed before reopen"
            )

        # The pending case re-dispatches shortly after start; the peer
        # listeners must already be armed when that fresh native send
        # happens. MeshCore exposes no startup drain, and the drain wait
        # below still precedes the first leg send.
        armed = await _arm_listeners(
            {"meshcore": listeners["meshcore"]}, _listener_lifetime(hw, 0.0)
        )
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
                    raise RuntimeError("adapter did not report healthy after reopen")
            result["startup_drain_wait"] = await wait_startup_drain_retired(app)
            timeline.append({"stage": "owner-restarted", "at": _now()})

            pending_row = _single_destination_row(before[pending_nonce], "meshcore")
            assert pending_row is not None, "stopped pending generation is missing"
            result["disposition"] = await _disposition(
                run,
                hw,
                armed,
                pending_row["outbox_id"],
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
                listeners,
                transports[0],
                transports[1],
                _nonce(),
                timeline,
            )
            await _leg_state(run, leg7, transports[1], hw["observation_timeout"])
            result["legs"].append(leg7)
            leg8 = await _run_leg(
                run,
                hw,
                senders,
                armed,
                listeners,
                transports[1],
                transports[0],
                _nonce(),
                timeline,
            )
            await _leg_state(run, leg8, transports[0], hw["observation_timeout"])
            result["legs"].append(leg8)

            await _teardown_owner(app, result, armed)
            app = None
            timeline.append({"stage": "owner-restopped", "at": _now()})
        finally:
            if app is not None:
                try:
                    await _teardown_owner(app, result, armed)
                    app = None
                    result["stop_on_error"] = True
                finally:
                    await _disarm_listeners(armed)
            else:
                await _disarm_listeners(armed)
        # Recheck after every later send and owner retirement. A delayed
        # duplicate/receipt change must not be hidden by the earlier snapshot.
        _stale_backlog_observation(run, completed + result["legs"])
        result["pending_after"] = capture_nonce_state(db, pending_nonce)
        if result["pending_after"] != result["disposition"]["pending_after"]:
            raise AssertionError(
                "pending replay durable state changed after observation"
            )
        result["uncertain_after"] = capture_nonce_state(db, uncertain_nonce)
        result["timeline"] = timeline
        result["pass"] = (
            result["disposition"].get("pending_reconciled") is True
            and all(leg.get("observed_once") for leg in result["legs"])
            and not result.get("surviving_sdk_children")
            and not _child_pids()
        )
    except BaseException as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[-1600:]
        result["cancelled"] = isinstance(exc, asyncio.CancelledError)
        result["timeline"] = timeline
        try:
            if app is not None:
                await _teardown_owner(app, result, armed)
                result["stop_on_error"] = True
        except BaseException as cleanup_error:
            result["cleanup_error"] = (
                type(cleanup_error).__name__ + ": " + str(cleanup_error)[-800:]
            )
        finally:
            try:
                await _disarm_listeners(armed)
            except BaseException as cleanup_error:
                result["peer_cleanup_error"] = (
                    type(cleanup_error).__name__ + ": " + str(cleanup_error)[-800:]
                )
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        state["reopen_finished_at"] = _now()
        _write_state(run, state)
        _write_json(run / "reopen-verdict.json", result)
    return bool(result.get("pass"))


async def _disposition(
    run: Path,
    hw: dict,
    armed: dict,
    pending_outbox_id: str,
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
            capture_nonce_state(db, pending_nonce),
            destination,
            outbox_id=pending_outbox_id,
        )
        is not None,
        RECONCILE_WAIT_SECONDS,
        _DB_POLL_INTERVAL,
    )
    pending_reconciled = ok
    if pending_reconciled:
        observed = await owned_blocking(
            _observe_window,
            receiver,
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
    pending_after, uncertain_after = capture_nonce_states(
        db, pending_nonce, uncertain_nonce
    )
    pending_reconciled = (
        pending_reconciled
        and _sent_receipt_event_id(
            pending_after, destination, outbox_id=pending_outbox_id
        )
        is not None
    )
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
