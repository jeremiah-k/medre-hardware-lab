"""Opt-in mixed-transport continuity case: the A-3 proof.

One MEDRE owner process holds FOUR adapters — two Matrix instances (the
healthy route), one MeshCore BLE board (the faulted transport), and one
Meshtastic serial board (owned, unrouted: this bench has no second
Meshtastic endpoint, so Meshtastic carries no route and makes no delivery
claim). Routes:

``mix-mesh-m0``
    MeshCore board inbound -> Matrix ``matrix0`` room A (unaffected
    destination, affected source).
``mix-m0-mesh``
    Matrix ``matrix0`` room A inbound -> MeshCore board outbound (affected
    destination; per-route retry so fault-window failures schedule rather
    than dead-letter).
``mix-m0-m1``
    Matrix ``matrix0`` room A inbound -> Matrix ``matrix1`` room B (the
    healthy route; unaffected end to end).

The A-3 assertion lives in :mod:`medre_lab.a3_eval`: with the MeshCore host
link disconnected by the worker that owns it, fresh room-A messages must be
observed by the INDEPENDENT room-B recipient strictly BEFORE the MeshCore
session rejoins. Healthy traffic observed only after the rejoin is a
failure. Every room-A message fans out to both the healthy route and the
faulted route, so one burst carries both route classes.

The baseline/reopen machinery extends the committed meshcore:lxmf case
(``medre_lab.continuity``'s two-phase shape): the pending capture keeps the
event-gated, durable-outbox-state design, keyed on the MeshCore outbox rows
(sibling Matrix receipts from the fan-out are expected and recorded, never
treated as boundary violations). The sustained-load sampler shells the
RELEASED core's read-only CLI (``medre inspect work-status``; ``medre
inspect pressure`` substitutes the requested ``medre storage status``, which
does not exist in the 0.1.2 CLI) throughout every phase.

Time discipline: verdicts record monotonic offsets and durations only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from .a3_eval import evaluate_a3
from .config import path_from
from .continuity import (
    CONTINUITY_MESSAGE_DELAY_SECONDS,
    DEFAULT_INTERRUPT_WAIT_SECONDS,
    RECONCILE_WAIT_SECONDS,
    _child_pids,
    _cmdline,
    _disarm_listeners,
    _disconnect_owned,
    _drain_send,
    _nonce,
    _now,
    _observe_window,
    _pre_handoff,
    _sent_receipt_event_id,
    _sent_receipt_identity,
    _session_diag,
    _stale_backlog_observation,
    _uncertain_settled,
    _wait_for,
    _write_json,
    capture_nonce_state,
    capture_nonce_states,
)
from .native.live_peer_common import async_peer_context, owned_blocking
from .ownership import own, own_run

#: Bounded wait for the independent room-B recipient inside the declared
#: fault window. The window itself is [disconnect observed, rejoin observed];
#: observations must land inside it, and the rejoin takes tens of seconds,
#: so this budget only bounds how long we WAIT for the recipient before
#: waiting out the rejoin (the evaluator still fails if any nonce arrived
#: after the rejoin).
FAULT_WINDOW_WITNESS_WAIT_SECONDS = 60.0

#: Bounded wait for the fault-window affected (MeshCore) rows to reach their
#: post-rejoin sent receipts. Attempts serialize behind the adapter's
#: pre-send pacing, so this covers several paced attempts plus retries.
FAULT_RECOVERY_WAIT_SECONDS = 300.0

#: Bounded far-side collection for the post-rejoin affected deliveries.
FAULT_RECOVERY_OBSERVE_SECONDS = 120.0

#: Bounded wait for the burst head's claimed, reserved, pre-handoff mesh row.
_HEAD_BOUNDARY_WAIT_SECONDS = 15.0

#: Sampler cadence for the released-core work-status/pressure shells.
SAMPLER_INTERVAL_SECONDS = 5.0

MESH_ADAPTER = "meshcore"
HEALTHY_SOURCE = "matrix0"
HEALTHY_DEST = "matrix1"


class MixedConfigError(ValueError):
    """Invalid mixed campaign configuration."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_mixed(config_path: Path, matrix_path: Path) -> dict:
    """Load and validate the private mixed campaign configuration.

    ``config_path`` is a lab-style TOML limited to the hardware table, where
    ``meshcore`` keeps exactly two BLE addresses (owner + peer) and
    ``meshtastic`` carries the ONE owned serial port (no peer endpoint exists
    on this bench). ``matrix_path`` is the private campaign matrix file
    written by provisioning. Neither file is committed; both bytes feed the
    configuration fingerprint that binds the two phases together.
    """
    import tomllib

    config_path = config_path.resolve()
    matrix_path = matrix_path.resolve()
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)
    if set(raw) - {"hardware"}:
        raise MixedConfigError("unknown configuration section")
    hw = raw.get("hardware", {})
    if not isinstance(hw, dict):
        raise MixedConfigError("hardware must be a table")
    allowed = {
        "meshcore",
        "meshtastic",
        "ble_adapter",
        "startup_timeout",
        "send_timeout",
        "observation_timeout",
    }
    unknown = set(hw) - allowed
    if unknown:
        raise MixedConfigError(f"unknown hardware options: {sorted(unknown)}")
    if "meshcore" not in hw or "meshtastic" not in hw:
        raise MixedConfigError(
            "mixed campaign requires meshcore and meshtastic sections"
        )
    for key, default in (
        ("startup_timeout", 60),
        ("send_timeout", 100),
        ("observation_timeout", 50),
    ):
        value = hw.setdefault(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise MixedConfigError(f"{key} must be finite and positive")
    adapter = hw.get("ble_adapter")
    if adapter is not None and not str(adapter).startswith("hci"):
        raise MixedConfigError("ble_adapter must be an HCI name such as hci0")

    mesh = hw["meshcore"]
    if set(mesh) != {"addresses", "channel"}:
        raise MixedConfigError("meshcore requires exactly addresses and channel")
    addresses = [a.upper() for a in mesh["addresses"]]
    if (
        len(addresses) != 2
        or len(set(addresses)) != 2
        or any(not isinstance(a, str) or a.count(":") != 5 for a in addresses)
    ):
        raise MixedConfigError("meshcore requires two distinct BLE addresses")
    mesh["addresses"] = addresses
    if not isinstance(mesh["channel"], int) or not 0 <= mesh["channel"] <= 255:
        raise MixedConfigError("invalid meshcore channel")

    mast = hw["meshtastic"]
    if set(mast) != {"ports", "channel"}:
        raise MixedConfigError("meshtastic requires exactly ports and channel")
    ports = mast["ports"]
    if (
        not isinstance(ports, list)
        or not 1 <= len(ports) <= 2
        or len(set(ports)) != len(ports)
    ):
        raise MixedConfigError("meshtastic requires one or two distinct ports")
    resolved = []
    for port in ports:
        path = path_from(config_path.parent, str(port))
        if not path.exists():
            raise MixedConfigError(f"meshtastic port does not exist: {path}")
        resolved.append(str(path))
    mast["ports"] = resolved
    if not isinstance(mast["channel"], int) or not 0 <= mast["channel"] <= 7:
        raise MixedConfigError("invalid meshtastic channel")

    with matrix_path.open("rb") as stream:
        matrix = json.load(stream)
    try:
        rooms = matrix["rooms"]
        accounts = matrix["accounts"]
        homeserver = matrix["homeserver"]
        for key in ("a", "b"):
            rooms[key]["room_id"]
        for key in ("m0", "m1", "sender", "witness"):
            accounts[key]["user_id"]
            accounts[key]["access_token"]
    except (KeyError, TypeError) as exc:
        raise MixedConfigError(f"campaign matrix file incomplete: {exc}") from exc
    fingerprint = hashlib.sha256()
    fingerprint.update(config_path.read_bytes())
    fingerprint.update(matrix_path.read_bytes())
    return {
        "hardware": hw,
        "matrix": {
            "homeserver": homeserver,
            "rooms": {k: v["room_id"] for k, v in rooms.items()},
            "accounts": accounts,
            # Persistent campaign-owned nio crypto store: a fresh per-run
            # store regenerates one-time keys under the same indices the
            # homeserver already holds, and synapse rejects the re-upload
            # (400 "One time key ... already exists"), which fails the
            # adapter's sync health. The owner's device identity must stay
            # stable across campaign runs, exactly like the rig's
            # persistent medre-normal volume.
            "store_dir": str(matrix_path.parent / "matrix-stores"),
            "fingerprint": fingerprint.hexdigest(),
        },
    }


# ---------------------------------------------------------------------------
# Runtime assembly (released core builder: four adapters, three routes)
# ---------------------------------------------------------------------------


def build_mixed(config: dict, role: int, run: Path):
    """Assemble the mixed runtime: matrix0, matrix1, meshcore, meshtastic."""
    from medre.config.adapters.matrix import MatrixConfig
    from medre.config.adapters.matrix_runtime import MatrixRuntimeConfig
    from medre.config.adapters.meshcore import MeshCoreConfig
    from medre.config.adapters.meshcore_runtime import MeshCoreRuntimeConfig
    from medre.config.adapters.meshtastic import MeshtasticConfig
    from medre.config.adapters.meshtastic_runtime import MeshtasticRuntimeConfig
    from medre.config.model import (
        AdapterConfigSet,
        LoggingConfig,
        RetryConfig,
        RuntimeConfig,
        RuntimeOptions,
        StorageConfig,
    )
    from medre.config.paths import MedrePaths
    from medre.config.routes import RouteConfig, RouteConfigSet, RouteRetryConfig
    from medre.runtime.builder import RuntimeBuilder

    hw = config["hardware"]
    matrix = config["matrix"]
    rooms = matrix["rooms"]
    accounts = matrix["accounts"]

    def matrix_runtime(
        adapter_id: str, room_id: str, account: dict
    ) -> MatrixRuntimeConfig:
        settings = MatrixConfig(
            adapter_id=adapter_id,
            homeserver=matrix["homeserver"],
            user_id=account["user_id"],
            access_token=account["access_token"],
            encryption_mode="plaintext",
            room_allowlist={room_id},
            auto_join_rooms=(room_id,),
            relay_prefix="{sender}: ",
            store_path=str(Path(matrix["store_dir"]) / adapter_id),
        ).validate()
        return MatrixRuntimeConfig(
            adapter_id=adapter_id, enabled=True, adapter_kind="real", config=settings
        )

    mesh_settings = MeshCoreConfig(
        adapter_id=MESH_ADAPTER,
        message_delay_seconds=CONTINUITY_MESSAGE_DELAY_SECONDS,
        relay_prefix="{sender}: ",
        connection_type="ble",
        ble_address=hw["meshcore"]["addresses"][role],
        default_channel=hw["meshcore"]["channel"],
        max_text_bytes=160,
        identity=f"MEDRE-MIXED-{role}",
    ).validate()
    mast_settings = MeshtasticConfig(
        adapter_id="meshtastic",
        relay_prefix="{sender}: ",
        connection_type="serial",
        serial_port=hw["meshtastic"]["ports"][0],
        default_channel=hw["meshtastic"]["channel"],
        max_text_bytes=200,
    ).validate()
    adapters = {
        "matrix": {
            HEALTHY_SOURCE: matrix_runtime(HEALTHY_SOURCE, rooms["a"], accounts["m0"]),
            HEALTHY_DEST: matrix_runtime(HEALTHY_DEST, rooms["b"], accounts["m1"]),
        },
        MESH_ADAPTER: {
            MESH_ADAPTER: MeshCoreRuntimeConfig(
                adapter_id=MESH_ADAPTER,
                enabled=True,
                adapter_kind="real",
                config=mesh_settings,
            )
        },
        "meshtastic": {
            "meshtastic": MeshtasticRuntimeConfig(
                adapter_id="meshtastic",
                enabled=True,
                adapter_kind="real",
                config=mast_settings,
            )
        },
    }
    routes = [
        RouteConfig(
            route_id="mix-mesh-m0",
            source_adapters=(MESH_ADAPTER,),
            dest_adapters=(HEALTHY_SOURCE,),
            source_channel=str(hw["meshcore"]["channel"]),
            dest_channel=rooms["a"],
        ),
        RouteConfig(
            route_id="mix-m0-mesh",
            source_adapters=(HEALTHY_SOURCE,),
            dest_adapters=(MESH_ADAPTER,),
            source_channel=rooms["a"],
            dest_channel=str(hw["meshcore"]["channel"]),
            retry=RouteRetryConfig(
                enabled=True,
                max_attempts=6,
                backoff_base=2.0,
                max_delay_seconds=30.0,
            ),
        ),
        RouteConfig(
            route_id="mix-m0-m1",
            source_adapters=(HEALTHY_SOURCE,),
            dest_adapters=(HEALTHY_DEST,),
            source_channel=rooms["a"],
            dest_channel=rooms["b"],
        ),
    ]
    routing = RouteConfigSet(routes=tuple(routes))
    routing.validate()
    db = run / "state.sqlite"
    cfg = RuntimeConfig(
        runtime=RuntimeOptions(name="medre-mixed-campaign"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path=str(db)),
        retry=RetryConfig(enabled=True, interval_seconds=5.0, max_attempts=6),
        adapters=AdapterConfigSet(**adapters),
        routes=routing,
    )
    paths = MedrePaths(
        config_dir=run / "config",
        config_file=run / "config/config.yaml",
        state_dir=run / "state",
        data_dir=run / "data",
        cache_dir=run / "cache",
        log_dir=run / "logs",
        database_path=db,
    )
    return RuntimeBuilder(cfg, paths).build()


def provenance_mixed() -> dict:
    """Installed-distribution provenance for the mixed source tuple."""
    import importlib.metadata

    records = {}
    for name in (
        "medre",
        "medre-adapter-matrix",
        "medre-adapter-meshcore",
        "medre-adapter-meshtastic",
        "meshcore",
        "mtjk",
        "bleak",
    ):
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            records[name] = {"installed": False}
            continue
        record = {"version": dist.version, "installed": True}
        raw = dist.read_text("direct_url.json")
        if raw:
            source = json.loads(raw)
            record["editable"] = bool(source.get("dir_info", {}).get("editable"))
            record["commit_id"] = source.get("vcs_info", {}).get("commit_id")
        records[name] = record
    return records


# ---------------------------------------------------------------------------
# Peer configuration (independent endpoints only)
# ---------------------------------------------------------------------------


def configure_mixed(config: dict, role: int, run: Path):
    """Listeners, senders, destinations and owned endpoints for the mixed case.

    The owner's endpoints (its MeshCore board and its Meshtastic serial port)
    are locked exclusively; the independent peers use the OTHER MeshCore
    board and their own Matrix sessions. Matrix holds no local endpoint.
    """
    hw = config["hardware"]
    matrix = config["matrix"]
    os.environ["MEDRE_LAB_RUN_DIR"] = str(run)
    peer_address = hw["meshcore"]["addresses"][1 - role]
    os.environ["MESHCORE_PEER_BLE_ADDRESS"] = peer_address
    os.environ["MESHCORE_PEER_CHANNEL"] = str(hw["meshcore"]["channel"])
    if hw.get("ble_adapter"):
        os.environ["MESHCORE_PEER_BLE_ADAPTER"] = hw["ble_adapter"]
    os.environ["MEDRE_LAB_OWNED_MESHCORE_ADDRESS"] = hw["meshcore"]["addresses"][role]
    os.environ["MEDRE_LAB_OWNED_MESHTASTIC_PORT"] = hw["meshtastic"]["ports"][0]

    from .matrix_live_peer import MatrixPeerListener, run_matrix_send
    from .native.meshcore_live_peer import MeshCorePeerListener, run_meshcore_peer

    rooms = matrix["rooms"]
    sender = matrix["accounts"]["sender"]
    witness = matrix["accounts"]["witness"]

    def matrix_sender(nonce: str) -> dict:
        return run_matrix_send(
            matrix["homeserver"],
            sender["access_token"],
            rooms["a"],
            nonce,
            timeout=hw["send_timeout"],
        )

    listeners = {
        "mesh": MeshCorePeerListener,
        "witness": lambda lifetime: MatrixPeerListener(
            matrix["homeserver"],
            witness["access_token"],
            (rooms["a"], rooms["b"]),
            witness["user_id"],
            lifetime=lifetime,
        ),
    }
    # Sender keys are ADAPTER IDs (legs index senders by source adapter id);
    # the matrix sender posts into room A as the independent campaign
    # sender account.
    senders = {
        MESH_ADAPTER: lambda nonce: run_meshcore_peer(
            ["sendn", peer_address, json.dumps([nonce])], hw["send_timeout"]
        ),
        HEALTHY_SOURCE: matrix_sender,
    }
    destinations = {
        MESH_ADAPTER: str(hw["meshcore"]["channel"]),
        HEALTHY_SOURCE: rooms["a"],
        HEALTHY_DEST: rooms["b"],
    }
    endpoints = hw["meshcore"]["addresses"] + hw["meshtastic"]["ports"]
    return listeners, senders, destinations, endpoints


# ---------------------------------------------------------------------------
# Durable-state helpers (mixed, fan-out aware)
# ---------------------------------------------------------------------------


def _rows_for(snapshot: dict, adapter: str) -> list[dict]:
    return [
        row
        for event in snapshot["events"]
        for row in event["outbox"]
        if row["target_adapter"] == adapter
    ]


def _single_row(snapshot: dict, adapter: str) -> dict | None:
    rows = _rows_for(snapshot, adapter)
    return rows[0] if len(rows) == 1 else None


def _mesh_receipts(snapshot: dict) -> list[dict]:
    return [
        receipt
        for event in snapshot["events"]
        for receipt in event["receipts"]
        if receipt["target_adapter"] == MESH_ADAPTER
    ]


def _head_boundary_mixed(snapshot: dict) -> bool:
    """Burst head claimed, reserved and pre-handoff at the MeshCore row.

    Sibling Matrix receipts from the fan-out are expected while the head
    holds the pacing window; only MeshCore receipts violate the boundary.
    """
    head = _single_row(snapshot, MESH_ADAPTER)
    return (
        head is not None
        and head["status"] == "in_progress"
        and head["active_attempt"] == 1
        and head["attempt_number"] == 0
        and _pre_handoff(head)
        and not _mesh_receipts(snapshot)
    )


def _tail_pending_mixed(snapshot: dict) -> bool:
    tail = _single_row(snapshot, MESH_ADAPTER)
    return (
        tail is not None
        and tail["status"] == "pending"
        and tail["attempt_number"] == 0
        and tail["active_attempt"] is None
        and tail["locked_at"] is None
        and tail["worker_id"] is None
        and tail["lease_until"] is None
        and not tail["radio_attempts"]
        and not _mesh_receipts(snapshot)
    )


def _final_sent_mixed(snapshot: dict, adapter: str) -> bool:
    """One sent receipt at *adapter*, one sent destination row, no permanent
    failure anywhere in the event (transient fault-window failures excluded)."""
    rows = _rows_for(snapshot, adapter)
    sent = [
        receipt
        for event in snapshot["events"]
        for receipt in event["receipts"]
        if receipt["target_adapter"] == adapter
        and receipt["receipt_kind"] == "attempt"
        and receipt["status"] == "sent"
    ]
    permanent = [
        receipt
        for event in snapshot["events"]
        for receipt in event["receipts"]
        if receipt["failure_kind"] not in (None, "transient")
        or receipt["status"] in ("dead_lettered", "denied")
    ]
    return (
        len(rows) == 1
        and rows[0]["status"] == "sent"
        and len(sent) == 1
        and not permanent
    )


def bind_generation(db: Path, nonce: str) -> dict:
    """Bind one nonce to its exact canonical event and destination rows."""
    snapshot = capture_nonce_state(db, nonce)
    return {
        "nonce": nonce,
        "event_ids": [event["event_id"] for event in snapshot["events"]],
        "dest_outbox_ids": {
            row["target_adapter"]: row["outbox_id"]
            for event in snapshot["events"]
            for row in event["outbox"]
        },
        "single_event": len(snapshot["events"]) == 1,
        "state": snapshot,
    }


# ---------------------------------------------------------------------------
# Sustained-load sampler (released core read-only CLI)
# ---------------------------------------------------------------------------


def _medre_binary() -> Path:
    candidate = Path(sys.executable).with_name("medre")
    if not candidate.exists():
        raise RuntimeError(f"released medre CLI not found next to {sys.executable}")
    return candidate


async def run_sampler(run: Path, db: Path, phases: dict, interval: float) -> None:
    """Shell the released core's read-only inspection CLI on a cadence.

    One JSONL line per sample: phase, phase-relative offset, the work-status
    document, and (every other cycle) the pressure aggregates that stand in
    for the requested ``medre storage status`` (no such command exists in the
    released 0.1.2 CLI; the substitution is recorded here and in the
    verdict). Sampler failures are recorded and skipped — one failed shell
    never fabricates coverage.
    """
    medre = _medre_binary()
    path = run / "work-status-series.jsonl"
    version = subprocess.run(
        [str(medre), "version"], capture_output=True, text=True, timeout=30
    )
    header = {
        "sampler": "released-core-read-only",
        "medre_binary": str(medre),
        "medre_version_first_line": (
            version.stdout.splitlines()[0] if version.stdout else ""
        ),
        "work_status_command": "medre inspect work-status --json --storage-path",
        "pressure_command": "medre inspect pressure --storage-path",
        "storage_status_note": "medre storage status does not exist in 0.1.2; "
        "inspect pressure recorded instead",
    }
    with path.open("a") as stream:
        stream.write(json.dumps({"header": header}) + "\n")
        stream.flush()
    cycle = 0
    while True:
        started = time.monotonic()
        sample: dict = {
            "cycle": cycle,
            "phase": phases.get("current"),
            "phase_elapsed": round(
                time.monotonic() - phases.get("phase_started_monotonic", started), 3
            ),
            "ok": False,
        }
        try:
            work = subprocess.run(
                [
                    str(medre),
                    "inspect",
                    "work-status",
                    "--json",
                    "--storage-path",
                    str(db),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            sample["work_status"] = json.loads(work.stdout)
            sample["ok"] = True
        except Exception as exc:  # noqa: BLE001 - record, never fabricate
            sample["error"] = type(exc).__name__ + ": " + str(exc)[-200:]
        if cycle % 2 == 0:
            try:
                pressure = subprocess.run(
                    [str(medre), "inspect", "pressure", "--storage-path", str(db)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                sample["pressure"] = json.loads(pressure.stdout)
            except Exception as exc:  # noqa: BLE001
                sample["pressure_error"] = type(exc).__name__
        with path.open("a") as stream:
            stream.write(json.dumps(sample, default=str) + "\n")
        cycle += 1
        await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))


# ---------------------------------------------------------------------------
# Shared phase helpers
# ---------------------------------------------------------------------------


async def _arm(listeners: dict, selected: dict, lifetime: float) -> dict:
    armed: dict = {}
    try:
        for kind, factory in selected.items():
            cm = async_peer_context(factory(lifetime))
            peer = await cm.__aenter__()
            armed[kind] = {"cm": cm, "peer": peer}
        return armed
    except BaseException:
        await _disarm_listeners(armed)
        raise


async def teardown_owner_mixed(app, result: dict, armed: dict) -> None:
    """Bounded stop with the committed bounded-drain contract."""
    peer_pids = set()
    for entry in armed.values():
        owner = getattr(entry["peer"], "_owner", None)
        if owner is not None and getattr(owner, "proc", None) is not None:
            peer_pids.add(owner.proc.pid)
    started = time.monotonic()
    await asyncio.wait_for(
        app.stop(drain_deadline=time.monotonic() + 1.0),
        30.0,
    )
    result["stop_seconds"] = round(time.monotonic() - started, 3)
    result["stop_drain_deadline_seconds"] = 1.0
    retire_started = time.monotonic()
    while True:
        survivors = sorted(set(_child_pids()) - peer_pids)
        if not survivors or time.monotonic() - retire_started >= 5.0:
            break
        await asyncio.sleep(0.2)
    result["surviving_sdk_children"] = survivors
    result["surviving_sdk_children_cmdlines"] = {
        str(pid): _cmdline(pid) for pid in survivors
    }


def _phase_offset(timeline: list) -> float:
    """Phase-relative monotonic offset anchored at the case-start entry."""
    anchor = next(
        (
            entry
            for entry in timeline
            if entry.get("stage") in ("mixed-baseline-start", "mixed-reopen-start")
        ),
        None,
    )
    base = anchor["monotonic"] if anchor else time.monotonic()
    return round(time.monotonic() - base, 3)


def _leg_record(source, destination, nonce, sent, seen_packets) -> dict:
    text_hits = [
        p for p in seen_packets if nonce in str(p.get("text", p.get("content", "")))
    ]
    observed_once = (
        bool(text_hits)
        and all(
            str(p.get("text", p.get("content", ""))).count(nonce) == 1
            for p in text_hits
        )
        and len(text_hits) == 1
    )
    return {
        "source": source,
        "destination": destination,
        "nonce": nonce,
        "native_send": sent,
        "observed_count": len(text_hits),
        "observed_once": observed_once,
        "observed_offsets": [p.get("observed_offset") for p in text_hits],
    }


async def _wait_leg_committed(
    db: Path, leg: dict, destination: str, timeout: float
) -> None:
    snapshot: dict = {}

    def committed() -> bool:
        nonlocal snapshot
        snapshot = capture_nonce_state(db, leg["nonce"])
        return _sent_receipt_identity(snapshot, destination) is not None

    ok, waited = await _wait_for(committed, timeout, 0.2)
    identity = _sent_receipt_identity(snapshot, destination)
    if not ok or identity is None:
        raise AssertionError(
            f"leg {leg['source']}->{leg['destination']} lacks one failure-free "
            f"sent receipt at {destination} (waited {waited:.1f}s)"
        )
    leg["event_id"], leg["receipt_id"] = identity
    leg["durable"] = snapshot


async def _run_leg_mixed(
    db: Path,
    hw: dict,
    senders: dict,
    peer,
    source: str,
    destination: str,
    nonce: str,
    extra_window: float = 0.0,
    observation_room: str | None = None,
) -> dict:
    """One leg: independent native send, far-side observation, durable bind.

    ``observation_room`` scopes the far-side count for Matrix-observed legs:
    the witness session watches both campaign rooms, so a healthy-route
    leg legitimately sees the sender's room-A ingress echo plus its own
    room-B delivery. Only the destination room's packet is the correlated
    far-side observation.
    """
    sent = await owned_blocking(senders[source], nonce)
    packets = await owned_blocking(
        _observe_window,
        peer,
        hw["observation_timeout"] + extra_window,
    )
    if observation_room is not None:
        packets = [p for p in packets if p.get("room_id") == observation_room]
    leg = _leg_record(source, destination, nonce, sent, packets)
    await _wait_leg_committed(db, leg, destination, hw["observation_timeout"])
    return leg


def _listener_lifetime(hw: dict) -> float:
    return (
        hw["startup_timeout"]
        + 3 * (hw["send_timeout"] + hw["observation_timeout"])
        + 700.0
    )


def _sampler_counts(run: Path) -> dict:
    path = run / "work-status-series.jsonl"
    counts: dict = {}
    if not path.exists():
        return counts
    with path.open() as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if "header" in record:
                continue
            phase = record.get("phase")
            if record.get("ok") and phase:
                counts.setdefault(phase, []).append(record.get("phase_elapsed"))
    return counts


# ---------------------------------------------------------------------------
# Baseline phase
# ---------------------------------------------------------------------------


async def _fault_action(app, run: Path, hw: dict, timeline: list) -> dict:
    """Worker-owned disconnect of the MeshCore host link; disconnect evidence."""
    meshcore = app.adapters.get(MESH_ADAPTER)
    if meshcore is None:
        raise RuntimeError("mixed fault stage requires the meshcore adapter")
    owned_address = os.environ.get("MEDRE_LAB_OWNED_MESHCORE_ADDRESS", "")
    diag_before = meshcore.diagnostics()
    if (diag_before.get("session") or {}).get("connected") is not True:
        raise RuntimeError("meshcore session not connected before the fault stage")
    evidence: dict = {
        "occurred": False,
        "action": "worker-owned busctl disconnect of the MEDRE-owned board",
        "owned_address": owned_address,
        "controller": hw.get("ble_adapter"),
        "pre_interrupt_diagnostics": diag_before.get("session", {}),
    }
    action = asyncio.create_task(
        owned_blocking(_disconnect_owned, run, owned_address, hw.get("ble_adapter"))
    )
    disconnected: dict = {}

    def disconnected_now() -> bool:
        nonlocal disconnected
        if action.done():
            action.result()
        disconnected = _session_diag(meshcore)
        return disconnected.get("connected") is False

    try:
        ok, waited = await _wait_for(disconnected_now, 60.0)
        if not ok:
            raise RuntimeError("owned meshcore link did not disconnect within 60s")
        evidence["disconnect_offset"] = _phase_offset(timeline)
        evidence["disconnect_wait_seconds"] = round(waited, 3)
        evidence["disconnect_diagnostics"] = disconnected
        evidence["occurred"] = True
        timeline.append({"stage": "link-disconnected", "at": _now()})
    except BaseException:
        action.cancel()
        try:
            await action
        except (asyncio.CancelledError, Exception):
            pass
        raise
    return evidence


async def _observe_rejoin(adapter, timeout: float, timeline: list):
    """Poll the session diagnostics until the first connected observation.

    The MeshCore session resets its reconnect-attempt counter to zero on
    success, so the peak observed counter is recorded as evidence only
    (never asserted); the rejoin moment is the first connected observation,
    expressed on the shared phase clock.
    """
    started = time.monotonic()
    peak = 0
    while True:
        diag = _session_diag(adapter)
        try:
            peak = max(peak, int(diag.get("reconnect_attempts") or 0))
        except (TypeError, ValueError):
            pass
        if diag.get("connected") is True and diag.get("reconnecting") is False:
            return _phase_offset(timeline), peak
        if time.monotonic() - started > timeout:
            raise RuntimeError("meshcore session did not rejoin after the burst")
        await asyncio.sleep(0.5)


async def run_mixed_baseline(
    config: dict,
    role: int,
    run: Path,
    burst_count: int = 3,
    interrupt_wait: float = DEFAULT_INTERRUPT_WAIT_SECONDS,
) -> bool:
    """Baseline owner phase: healthy legs, MeshCore fault, in-window proof."""

    if not math.isfinite(interrupt_wait) or interrupt_wait <= 0:
        raise ValueError("interrupt_wait must be finite and positive")
    if not 1 <= int(burst_count) <= 6:
        raise ValueError("burst_count must be between 1 and 6")
    burst_count = int(burst_count)
    with own_run(run):
        if (run / "continuity-state.json").exists():
            raise RuntimeError("baseline requires a fresh run directory")
        listeners, senders, destinations, endpoints = configure_mixed(config, role, run)
        with own(endpoints):
            return await _run_mixed_baseline_owned(
                config, role, run, listeners, senders, burst_count
            )


async def _run_mixed_baseline_owned(
    config, role, run, listeners, senders, burst_count
) -> bool:
    from .worker import wait_startup_drain_retired

    hw = config["hardware"]
    db = run / "state.sqlite"
    timeline: list = [
        {
            "stage": "mixed-baseline-start",
            "at": _now(),
            "monotonic": time.monotonic(),
            "pid": os.getpid(),
        }
    ]
    result: dict = {
        "phase": "mixed-baseline",
        "role": "a" if role == 0 else "b",
        "pass": False,
        "legs": [],
        "interrupt": {},
        "a3": {},
        "pending_capture": {},
        "packages": provenance_mixed(),
        "scope": "Mixed-transport A-3 continuity baseline; not the short check.",
    }
    app = None
    armed: dict = {}
    tail_send: "asyncio.Future[dict] | None" = None
    sampler_task: "asyncio.Task | None" = None
    phases = {"current": "baseline", "phase_started_monotonic": time.monotonic()}
    state: dict = {
        "phase": "mixed-baseline",
        "role": result["role"],
        "config_sha256": config["matrix"]["fingerprint"],
        "started_at": _now(),
    }
    _write_json(run / "continuity-state.json", state)
    try:
        app = build_mixed(config, role, run)
        await asyncio.wait_for(app.start(), hw["startup_timeout"])
        for adapter_id, adapter in app.adapters.items():
            health = await asyncio.wait_for(adapter.health_check(), 30)
            result.setdefault("startup_health", {})[adapter_id] = health.health
            if health.health != "healthy":
                raise RuntimeError(
                    f"adapter {adapter_id} did not report healthy at startup"
                )
        result["startup_drain_wait"] = await wait_startup_drain_retired(app)
        timeline.append({"stage": "owner-started", "at": _now()})
        sampler_task = asyncio.create_task(
            run_sampler(run, db, phases, SAMPLER_INTERVAL_SECONDS)
        )

        # -- healthy baseline legs -----------------------------------------
        # Never contend with a sender for the same board: the mesh peer
        # listener (peer board) arms only for the mesh-destination leg;
        # leg1's mesh SENDER needs that board free.
        armed = await _arm(
            listeners, {"witness": listeners["witness"]}, _listener_lifetime(hw)
        )
        witness = armed["witness"]["peer"]

        leg1 = await _run_leg_mixed(
            db,
            hw,
            senders,
            witness,
            MESH_ADAPTER,
            HEALTHY_SOURCE,
            _nonce(),
            observation_room=config["matrix"]["rooms"]["a"],
        )
        result["legs"].append(leg1)
        timeline.append({"stage": "leg1-mesh-m0", "at": _now()})
        mesh_armed = await _arm(
            listeners, {"mesh": listeners["mesh"]}, _listener_lifetime(hw)
        )
        mesh_peer = mesh_armed["mesh"]["peer"]
        leg2 = await _run_leg_mixed(
            db,
            hw,
            senders,
            mesh_peer,
            HEALTHY_SOURCE,
            MESH_ADAPTER,
            _nonce(),
            extra_window=CONTINUITY_MESSAGE_DELAY_SECONDS,
        )
        result["legs"].append(leg2)
        timeline.append({"stage": "leg2-m0-mesh", "at": _now()})
        leg3 = await _run_leg_mixed(
            db,
            hw,
            senders,
            witness,
            HEALTHY_SOURCE,
            HEALTHY_DEST,
            _nonce(),
            observation_room=config["matrix"]["rooms"]["b"],
        )
        result["legs"].append(leg3)
        timeline.append({"stage": "leg3-m0-m1", "at": _now()})
        await _disarm_listeners(mesh_armed)

        state["completed_legs"] = [
            {
                "nonce": leg["nonce"],
                "source": leg["source"],
                "destination": leg["destination"],
                "event_id": leg.get("event_id"),
                "receipt_id": leg.get("receipt_id"),
                "durable": leg.get("durable"),
            }
            for leg in result["legs"]
        ]
        _write_json(run / "continuity-state.json", state)

        # -- declared fault window -----------------------------------------
        await _disarm_listeners(armed)
        armed = {}
        phases.update(current="interruption", phase_started_monotonic=time.monotonic())
        result["interrupt"] = await _fault_action(app, run, hw, timeline)
        # Observe the rejoin continuously from the disconnect: the session's
        # own recovery loop runs concurrently with the burst, so the honest
        # rejoin moment is the FIRST connected observation after the fault,
        # polled on the same phase clock the witness offsets use.
        meshcore_adapter = app.adapters[MESH_ADAPTER]
        rejoin_watch = asyncio.create_task(
            _observe_rejoin(meshcore_adapter, hw["startup_timeout"] + 300.0, timeline)
        )

        # Independent recipient armed INSIDE the window, before the burst.
        # Its arm time anchors its packet offsets in the phase clock, so
        # witness observations share one clock with the fault offsets.
        armed = await _arm(
            listeners, {"witness": listeners["witness"]}, _listener_lifetime(hw)
        )
        witness_arm_phase = _phase_offset(timeline)
        witness = armed["witness"]["peer"]
        burst = [_nonce() for _ in range(burst_count)]
        posted = {}
        for nonce in burst:
            sent = await owned_blocking(senders[HEALTHY_SOURCE], nonce)
            posted[nonce] = {
                "posted_offset": _phase_offset(timeline),
                "send_event_id": sent.get("event_id"),
            }
        timeline.append({"stage": "fault-window-burst", "at": _now()})

        # Bounded wait for the independent recipient; the fault window itself
        # stays open until the rejoin is observed below.
        witness_packets = await owned_blocking(
            _observe_window, witness, FAULT_WINDOW_WITNESS_WAIT_SECONDS
        )
        unaffected_obs = [
            {
                "nonce": nonce,
                # Convert the listener-relative offset into the shared phase
                # clock: witness armed at witness_arm_phase, packet seen
                # observed_offset after that arm. Only room-B packets count:
                # the assertion room is the independent recipient's room
                # (the healthy route's delivery surface). Room-A sightings
                # of the same nonce are the sender's ingress echo and are
                # recorded separately, never as healthy-route deliveries.
                "observed_offset": round(
                    witness_arm_phase + packet.get("observed_offset", 0), 3
                ),
                "listener_offset": packet.get("observed_offset"),
                "event_id": packet.get("event_id"),
                "room_id": packet.get("room_id"),
            }
            for packet in witness_packets
            for nonce in burst
            if nonce in str(packet.get("text", ""))
            and packet.get("room_id") == config["matrix"]["rooms"]["b"]
        ]
        ingress_echo = [
            {
                "nonce": nonce,
                "observed_offset": round(
                    witness_arm_phase + packet.get("observed_offset", 0), 3
                ),
                "room_id": packet.get("room_id"),
            }
            for packet in witness_packets
            for nonce in burst
            if nonce in str(packet.get("text", ""))
            and packet.get("room_id") == config["matrix"]["rooms"]["a"]
        ]
        # The mesh peer arms before the rejoin so the affected route's
        # silence inside the window is observable on the shared phase clock.
        mesh_arm_phase = _phase_offset(timeline)
        mesh_entry = await _arm(
            listeners, {"mesh": listeners["mesh"]}, _listener_lifetime(hw)
        )
        mesh_peer = mesh_entry["mesh"]["peer"]

        # Rejoin completes the declared window: first connected observation
        # after the disconnect, from the continuous watcher.
        phases.update(current="recovery", phase_started_monotonic=time.monotonic())
        try:
            rejoin_offset, rejoin_peak = await asyncio.wait_for(
                rejoin_watch, hw["startup_timeout"] + 60.0
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                "meshcore session did not rejoin after the burst"
            ) from exc
        timeline.append({"stage": "link-rejoined", "at": _now()})
        result["interrupt"].update(
            {
                "rejoin_offset": rejoin_offset,
                "rejoined": True,
                "rejoin_peak_reconnect_attempts": rejoin_peak,
                "rejoin_window_total_seconds": round(
                    rejoin_offset - result["interrupt"]["disconnect_offset"], 3
                ),
            }
        )

        # Generation binding from durable state (exact event + outbox rows).
        generation = {}
        for nonce in burst:
            binding = bind_generation(db, nonce)
            generation[nonce] = {
                "event_id": (
                    binding["event_ids"][0] if binding["single_event"] else None
                ),
                "dest_outbox_ids": binding["dest_outbox_ids"],
                "single_event": binding["single_event"],
            }
        result["a3"] = {
            "burst": burst,
            "posted": posted,
            "assertion_room": config["matrix"]["rooms"]["b"],
            "ingress_echo": ingress_echo,
            "unaffected": {
                "manifest": [
                    {
                        "nonce": nonce,
                        "event_id": generation[nonce]["event_id"],
                        "outbox_ids": {
                            HEALTHY_DEST: generation[nonce]["dest_outbox_ids"].get(
                                HEALTHY_DEST
                            )
                        },
                    }
                    for nonce in burst
                ],
                "observations": [
                    obs
                    for obs in unaffected_obs
                    if generation[obs["nonce"]]["single_event"]
                ],
            },
            "affected": {
                "manifest": [
                    {
                        "nonce": nonce,
                        "event_id": generation[nonce]["event_id"],
                        "outbox_ids": {
                            MESH_ADAPTER: generation[nonce]["dest_outbox_ids"].get(
                                MESH_ADAPTER
                            )
                        },
                    }
                    for nonce in burst
                ],
                "observations_during_fault": [],
            },
            "generation_binding": generation,
            "rejoin_offset": rejoin_offset,
            "interrupt": result["interrupt"],
        }

        # Post-rejoin recovery of the affected rows (recorded, never assumed).
        def affected_settled() -> bool:
            snapshots = capture_nonce_states(db, *burst)
            return all(
                _final_sent_mixed(snapshot, MESH_ADAPTER) for snapshot in snapshots
            )

        ok, waited = await _wait_for(affected_settled, FAULT_RECOVERY_WAIT_SECONDS, 0.2)
        recovery_packets = await owned_blocking(
            _observe_window, mesh_peer, FAULT_RECOVERY_OBSERVE_SECONDS
        )
        delivered_after = sorted(
            {
                nonce
                for packet in recovery_packets
                for nonce in burst
                if nonce in str(packet.get("text", ""))
            }
        )
        during_fault = [
            {
                "nonce": nonce,
                "observed_offset": round(mesh_arm_phase + packet["observed_offset"], 3),
            }
            for packet in recovery_packets
            for nonce in burst
            if nonce in str(packet.get("text", ""))
            and isinstance(packet.get("observed_offset"), (int, float))
            and mesh_arm_phase + packet["observed_offset"] < rejoin_offset
        ]
        result["a3"]["affected"].update(
            {
                "post_rejoin_delivered_nonces": delivered_after,
                "post_rejoin_outcome": (
                    "all_delivered"
                    if ok and len(delivered_after) == burst_count
                    else "partial" if delivered_after else "not_delivered"
                ),
                "observations_during_fault": during_fault,
                "recovery_wait_seconds": round(waited, 3),
                "mesh_peer_window_packets": len(recovery_packets),
            }
        )

        # The A-3 evaluation from captured offsets (pure evaluator).
        transcript = {
            "fault": {
                "occurred": result["interrupt"].get("occurred") is True,
                "action": result["interrupt"].get("action"),
                "disconnect_offset": result["interrupt"].get("disconnect_offset"),
                "rejoin_offset": result["interrupt"].get("rejoin_offset"),
            },
            "unaffected": result["a3"]["unaffected"],
            "affected": result["a3"]["affected"],
            "generation_binding": generation,
            "sampler": _sampler_counts(run),
        }
        result["a3"]["evaluation"] = evaluate_a3(transcript)
        state["a3_transcript"] = transcript
        _write_json(run / "continuity-state.json", state)

        # -- pending capture for the reopen phase ---------------------------
        await _disarm_listeners(armed)
        armed = dict(mesh_entry)
        phases.update(
            current="pending-capture", phase_started_monotonic=time.monotonic()
        )
        result["pending_capture"], tail_send = await _pending_capture_mixed(
            run, hw, senders, timeline
        )
        state["pending_capture"] = result["pending_capture"]
        _write_json(run / "continuity-state.json", state)

        await teardown_owner_mixed(app, result, armed)
        app = None
        result["pending_capture"]["pending"]["native_send"] = await tail_send
        tail_send = None
        _verify_stopped_capture_mixed(run, result["pending_capture"])
        state["pending_capture"] = result["pending_capture"]
        _write_json(run / "continuity-state.json", state)
        timeline.append({"stage": "owner-stopped", "at": _now()})
        await _disarm_listeners(armed)
        armed = {}

        if sampler_task is not None:
            sampler_task.cancel()
            try:
                await sampler_task
            except asyncio.CancelledError:
                pass
            sampler_task = None

        result["timeline"] = timeline
        result["pass"] = (
            all(leg.get("observed_once") for leg in result["legs"])
            and result["a3"]["evaluation"]["pass"] is True
            and result["interrupt"].get("rejoined") is True
            and result["pending_capture"].get("captured") is True
            and not result.get("surviving_sdk_children")
            and not _child_pids()
        )
    except BaseException as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[-1600:]
        result["cancelled"] = isinstance(exc, asyncio.CancelledError)
        result["timeline"] = timeline
        if sampler_task is not None:
            sampler_task.cancel()
            try:
                await sampler_task
            except asyncio.CancelledError:
                pass
            sampler_task = None
        if tail_send is not None and not tail_send.done():
            await _drain_send(tail_send)
            tail_send = None
        try:
            if app is not None:
                await teardown_owner_mixed(app, result, armed)
                app = None
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
        phases["current"] = "baseline-done"
        state["baseline_finished_at"] = _now()
        _write_json(run / "continuity-state.json", state)
        _write_json(run / "mixed-baseline-verdict.json", result)
    return bool(result.get("pass"))


# ---------------------------------------------------------------------------
# Pending capture (mixed, MeshCore-row keyed) and stopped verification
# ---------------------------------------------------------------------------


async def _pending_capture_mixed(run: Path, hw: dict, senders: dict, timeline: list):
    """Create and capture one reconcilable pending case before shutdown.

    Same event-gated shape as the committed pair case, keyed on the
    MeshCore outbox rows: the burst head is claimed, reserved and
    pre-handoff while holding the adapter's serialized pre-send pacing; the
    tail is released only after that boundary is observed and lands in the
    outbox never claimed behind the head. Sibling Matrix receipts from the
    fan-out are expected and recorded.
    """
    db = run / "state.sqlite"
    head_nonce = _nonce()
    tail_nonce = _nonce()
    sent_head = await owned_blocking(senders[HEALTHY_SOURCE], head_nonce)

    ok, waited = await _wait_for(
        lambda: _head_boundary_mixed(capture_nonce_state(db, head_nonce)),
        _HEAD_BOUNDARY_WAIT_SECONDS,
        0.2,
    )
    if not ok:
        raise AssertionError(
            "mixed burst head was never observed claimed and pre-handoff at "
            "the meshcore row"
        )
    timeline.append(
        {
            "stage": "head-claimed-pre-handoff",
            "at": _now(),
            "wait_seconds": round(waited, 3),
        }
    )
    tail_send = asyncio.create_task(owned_blocking(senders[HEALTHY_SOURCE], tail_nonce))
    head_state, tail_state = {}, {}

    def captured() -> bool:
        nonlocal head_state, tail_state
        head_state, tail_state = capture_nonce_states(db, head_nonce, tail_nonce)
        return _head_boundary_mixed(head_state) and _tail_pending_mixed(tail_state)

    try:
        ok, waited = await _wait_for(captured, hw["observation_timeout"], 0.2)
    except BaseException:
        await _drain_send(tail_send)
        raise
    if not ok:
        tail_failure = await _drain_send(tail_send)
        detail = "" if tail_failure is None else f"; tail send failed: {tail_failure}"
        raise AssertionError(
            "mixed burst was not observed pending behind an active head" + detail
        )
    timeline.append(
        {"stage": "pending-captured", "at": _now(), "wait_seconds": round(waited, 3)}
    )
    capture = {
        "captured": True,
        "destination": MESH_ADAPTER,
        "pending": {"nonce": tail_nonce, "state": tail_state},
        "uncertain": {
            "nonce": head_nonce,
            "native_send": sent_head,
            "state": head_state,
        },
    }
    return capture, tail_send


def _verify_stopped_capture_mixed(run: Path, capture: dict) -> None:
    head_snap, tail_snap = capture_nonce_states(
        run / "state.sqlite", capture["uncertain"]["nonce"], capture["pending"]["nonce"]
    )
    original_head = _single_row(capture["uncertain"]["state"], MESH_ADAPTER)
    original_tail = _single_row(capture["pending"]["state"], MESH_ADAPTER)
    current_head = _single_row(head_snap, MESH_ADAPTER)
    current_tail = _single_row(tail_snap, MESH_ADAPTER)
    if (
        current_head is None
        or current_tail is None
        or original_head is None
        or original_tail is None
        or current_head["outbox_id"] != original_head["outbox_id"]
        or current_tail["outbox_id"] != original_tail["outbox_id"]
        or not _tail_pending_mixed(tail_snap)
        or current_head["status"] in ("sent", "queued")
        or not _pre_handoff(current_head)
        or _mesh_receipts(head_snap)
        or _mesh_receipts(tail_snap)
    ):
        raise AssertionError(
            "stopped mixed burst no longer retains the captured head/tail "
            "identities and unfinished tail at the meshcore rows"
        )
    capture["stopped"] = {"uncertain": head_snap, "pending": tail_snap}


# ---------------------------------------------------------------------------
# Reopen phase
# ---------------------------------------------------------------------------


async def run_mixed_reopen(config: dict, role: int, run: Path) -> bool:
    """Fresh-process reopen on the same campaign database and profiles."""
    with own_run(run):
        listeners, senders, destinations, endpoints = configure_mixed(config, role, run)
        with own(endpoints):
            return await _run_mixed_reopen_owned(config, role, run, listeners, senders)


async def _run_mixed_reopen_owned(config, role, run, listeners, senders) -> bool:
    from .worker import wait_startup_drain_retired

    hw = config["hardware"]
    db = run / "state.sqlite"
    timeline: list = [
        {
            "stage": "mixed-reopen-start",
            "at": _now(),
            "monotonic": time.monotonic(),
            "pid": os.getpid(),
        }
    ]
    result: dict = {
        "phase": "mixed-reopen",
        "role": "a" if role == 0 else "b",
        "pass": False,
        "legs": [],
        "persisted": {},
        "disposition": {},
        "packages": provenance_mixed(),
        "scope": "Fresh-process reopen of the mixed campaign baseline state.",
    }
    app = None
    armed: dict = {}
    sampler_task: "asyncio.Task | None" = None
    phases = {"current": "reopen", "phase_started_monotonic": time.monotonic()}
    state = _read_state_mixed(run)
    try:
        baseline = json.loads((run / "mixed-baseline-verdict.json").read_text())
        if (
            baseline.get("phase") != "mixed-baseline"
            or baseline.get("pass") is not True
            or state.get("config_sha256") != config["matrix"]["fingerprint"]
        ):
            raise RuntimeError(
                "reopen requires a matching successful mixed baseline and configuration"
            )
        if baseline.get("packages") != result["packages"]:
            raise RuntimeError("installed package provenance changed between phases")
        completed = state.get("completed_legs") or []
        if not completed or "pending_capture" not in state:
            raise RuntimeError(
                "reopen requires the baseline's completed legs and captured pending case"
            )
        pending_nonce = state["pending_capture"]["pending"]["nonce"]
        uncertain_nonce = state["pending_capture"]["uncertain"]["nonce"]

        result["process"] = {"pid": os.getpid()}
        before = {
            leg["nonce"]: capture_nonce_state(db, leg["nonce"]) for leg in completed
        }
        before[pending_nonce] = capture_nonce_state(db, pending_nonce)
        before[uncertain_nonce] = capture_nonce_state(db, uncertain_nonce)
        persisted_ok = True
        for leg in completed:
            snapshot = before[leg["nonce"]]
            identity = _sent_receipt_identity(snapshot, leg["destination"])
            if identity is None or identity != (leg["event_id"], leg["receipt_id"]):
                persisted_ok = False
        result["persisted"] = {
            "completed_identities_retained": persisted_ok,
            "completed_count": len(completed),
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

        armed = await _arm(
            listeners,
            {
                "witness": listeners["witness"],
                "mesh": lambda lifetime: listeners["mesh"](lifetime),
            },
            _listener_lifetime(hw),
        )
        try:
            app = build_mixed(config, role, run)
            await asyncio.wait_for(app.start(), hw["startup_timeout"])
            for adapter_id, adapter in app.adapters.items():
                health = await asyncio.wait_for(adapter.health_check(), 30)
                if health.health != "healthy":
                    raise RuntimeError(
                        f"adapter {adapter_id} did not report healthy after reopen"
                    )
            result["startup_drain_wait"] = await wait_startup_drain_retired(app)
            timeline.append({"stage": "owner-restarted", "at": _now()})
            sampler_task = asyncio.create_task(
                run_sampler(run, db, phases, SAMPLER_INTERVAL_SECONDS)
            )

            pending_row = _single_row(before[pending_nonce], MESH_ADAPTER)
            if pending_row is None:
                raise AssertionError("stopped pending generation is missing")
            result["disposition"] = await _disposition_mixed(
                run,
                hw,
                armed,
                pending_row["outbox_id"],
                pending_nonce,
                uncertain_nonce,
                timeline,
            )

            mesh_peer = armed["mesh"]["peer"]
            witness = armed["witness"]["peer"]
            # Never contend with a sender for the same board: disarm the mesh
            # listener for the mesh-source leg, re-arm it for the
            # mesh-destination leg.
            await _disarm_listeners({"mesh": armed.pop("mesh")})
            leg = await _run_leg_mixed(
                db,
                hw,
                senders,
                witness,
                MESH_ADAPTER,
                HEALTHY_SOURCE,
                _nonce(),
                observation_room=config["matrix"]["rooms"]["a"],
            )
            result["legs"].append(leg)
            mesh_rearmed = await _arm(
                listeners, {"mesh": listeners["mesh"]}, _listener_lifetime(hw)
            )
            armed["mesh"] = mesh_rearmed["mesh"]
            mesh_peer = mesh_rearmed["mesh"]["peer"]
            leg = await _run_leg_mixed(
                db,
                hw,
                senders,
                mesh_peer,
                HEALTHY_SOURCE,
                MESH_ADAPTER,
                _nonce(),
                extra_window=CONTINUITY_MESSAGE_DELAY_SECONDS,
            )
            result["legs"].append(leg)

            await teardown_owner_mixed(app, result, armed)
            app = None
            timeline.append({"stage": "owner-restopped", "at": _now()})
        finally:
            if app is not None:
                try:
                    await teardown_owner_mixed(app, result, armed)
                    app = None
                    result["stop_on_error"] = True
                finally:
                    await _disarm_listeners(armed)
            else:
                await _disarm_listeners(armed)

        if sampler_task is not None:
            sampler_task.cancel()
            try:
                await sampler_task
            except asyncio.CancelledError:
                pass
            sampler_task = None

        # Completed deliveries stay settled: no durable change after the fact.
        all_completed = completed + [
            {
                "nonce": leg["nonce"],
                "destination": leg["destination"],
                "durable": leg.get("durable"),
            }
            for leg in result["legs"]
        ]
        _stale_backlog_observation(run, all_completed)
        result["pending_after"] = capture_nonce_state(db, pending_nonce)
        if result["pending_after"] != result["disposition"]["pending_after"]:
            raise AssertionError(
                "pending replay durable state changed after observation"
            )
        result["uncertain_after"] = capture_nonce_state(db, uncertain_nonce)

        # The final stopped snapshot matches all completed deliveries.
        outstanding = []
        for leg in all_completed:
            snapshot = capture_nonce_state(db, leg["nonce"])
            if _sent_receipt_identity(snapshot, leg["destination"]) is None:
                outstanding.append(leg["nonce"])
        result["final_reconciliation"] = {
            "completed_count": len(all_completed),
            "outstanding": outstanding,
            "final_snapshot_matches_completed": not outstanding,
        }
        result["timeline"] = timeline
        result["pass"] = (
            result["disposition"].get("pending_reconciled") is True
            and all(leg.get("observed_once") for leg in result["legs"])
            and result["final_reconciliation"]["final_snapshot_matches_completed"]
            and not result.get("surviving_sdk_children")
            and not _child_pids()
        )
    except BaseException as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[-1600:]
        result["cancelled"] = isinstance(exc, asyncio.CancelledError)
        result["timeline"] = timeline
        if sampler_task is not None:
            sampler_task.cancel()
            try:
                await sampler_task
            except asyncio.CancelledError:
                pass
            sampler_task = None
        try:
            if app is not None:
                await teardown_owner_mixed(app, result, armed)
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
        phases["current"] = "reopen-done"
        state["reopen_finished_at"] = _now()
        _write_state_mixed(run, state)
        _write_json(run / "mixed-reopen-verdict.json", result)
    return bool(result.get("pass"))


async def _disposition_mixed(
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
    initial work; core's dispatcher may claim and deliver it. ``uncertain``
    was claimed but interrupted before its native send boundary: whether
    core progresses it is core's own replay authority, recorded without
    assertion.
    """
    db = run / "state.sqlite"
    mesh_peer = armed["mesh"]["peer"]

    ok, waited = await _wait_for(
        lambda: _sent_receipt_event_id(
            capture_nonce_state(db, pending_nonce),
            MESH_ADAPTER,
            outbox_id=pending_outbox_id,
        )
        is not None,
        RECONCILE_WAIT_SECONDS,
        0.2,
    )
    pending_reconciled = ok
    if pending_reconciled:
        observed = await owned_blocking(
            _observe_window, mesh_peer, hw["observation_timeout"]
        )
        hits = [p for p in observed if pending_nonce in str(p.get("text", ""))]
        pending_reconciled = len(hits) == 1
    timeline.append(
        {
            "stage": "pending-disposition",
            "at": _now(),
            "reconciled": pending_reconciled,
            "wait_seconds": round(waited, 3),
        }
    )
    await _wait_for(
        lambda: _uncertain_settled(capture_nonce_state(db, uncertain_nonce)),
        RECONCILE_WAIT_SECONDS,
        0.2,
    )
    pending_after, uncertain_after = capture_nonce_states(
        db, pending_nonce, uncertain_nonce
    )
    pending_reconciled = pending_reconciled and (
        _sent_receipt_event_id(pending_after, MESH_ADAPTER, outbox_id=pending_outbox_id)
        is not None
    )
    return {
        "pending_reconciled": pending_reconciled,
        "pending_after": pending_after,
        "uncertain_after": uncertain_after,
        "uncertain_progressed": _sent_receipt_event_id(uncertain_after, MESH_ADAPTER)
        is not None,
        "uncertain_final_statuses": sorted(
            {
                row["status"]
                for event in uncertain_after["events"]
                for row in event["outbox"]
            }
        ),
    }


def _read_state_mixed(run: Path) -> dict:
    path = run / "continuity-state.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _write_state_mixed(run: Path, state: dict) -> None:
    _write_json(run / "continuity-state.json", state)
