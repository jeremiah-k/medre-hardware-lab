"""Run one transport pair/role in an isolated, bounded hardware process."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from .config import load, pair
from .ownership import own
from .runtime import build


async def bounded(awaitable, timeout):
    return await asyncio.wait_for(awaitable, timeout)


_DRAIN_POLL_INTERVAL = 0.25
_DRAIN_WINDOW_FACTOR = 3.0
_DRAIN_WINDOW_SLACK = 5.0
# The drain deadline is armed from a monotonic clock at (wall-clock)
# adapter_start_epoch; crossing the two clocks needs a small read margin.
_DRAIN_EPOCH_MARGIN = 0.1


async def wait_startup_drain_retired(
    app,
    poll_interval: float = _DRAIN_POLL_INTERVAL,
    window_factor: float = _DRAIN_WINDOW_FACTOR,
    window_slack: float = _DRAIN_WINDOW_SLACK,
) -> list[dict]:
    """Wait out each running adapter's initial startup drain before first send.

    The Meshtastic adapter's content-independent startup drain drops
    relay-classified peer traffic while it is live, and the standard short
    check starts its first leg immediately after adapter startup — sending
    into a live drain window would false-fail the check. This observes each
    adapter's public diagnostics: an adapter that exposes no diagnostics or
    reports no ``startup_packet_drain_seconds`` window is skipped, as is a
    non-positive window; otherwise the wait proceeds until the wall clock
    passes ``adapter_start_epoch + window``, re-reading both values every
    poll so a re-armed window extends the wait. The adapter retires the
    window lazily on later packets, so deadline passage — not an observed
    state flip — is the retirement criterion.

    Raises RuntimeError with a diagnostics snapshot when the window has not
    retired within ``window_factor * window + window_slack`` seconds; leg
    deadlines and verification are never adjusted to obtain a pass.
    """
    records: list[dict] = []
    for adapter in app.adapters.values():
        adapter_id = getattr(adapter, "adapter_id", "unknown")
        diag = adapter.diagnostics() if hasattr(adapter, "diagnostics") else {}
        window = diag.get("startup_packet_drain_seconds")
        record: dict = {"adapter": adapter_id, "window_seconds": window}
        if window is None:
            record.update(
                outcome="skipped", reason="adapter reports no startup drain window"
            )
            records.append(record)
            continue
        window = float(window)
        record["window_seconds"] = window
        if window <= 0:
            record.update(outcome="skipped", reason="startup drain disabled")
            records.append(record)
            continue
        started = time.monotonic()
        bound = started + window * window_factor + window_slack
        armed = None
        while True:
            diag = adapter.diagnostics()
            window = diag.get("startup_packet_drain_seconds")
            if window is None or float(window) <= 0:
                armed = diag.get("adapter_start_epoch")
                break
            armed = diag.get("adapter_start_epoch")
            if armed is not None and time.time() >= (
                armed + float(window) + _DRAIN_EPOCH_MARGIN
            ):
                break
            if time.monotonic() >= bound:
                raise RuntimeError(
                    f"adapter {adapter_id} startup drain (window={window}s,"
                    f" armed_epoch={armed}) did not retire within"
                    f" {window * window_factor + window_slack:.1f}s;"
                    f" diagnostics: {json.dumps(diag, default=str)[-800:]}"
                )
            await asyncio.sleep(poll_interval)
        record.update(
            outcome="retired",
            armed_epoch=armed,
            waited_seconds=round(time.monotonic() - started, 3),
        )
        records.append(record)
    return records


def provenance(transports):
    names = ["medre"] + [f"medre-adapter-{t}" for t in transports]
    names += [
        dict(meshtastic="mtjk", meshcore="meshcore", lxmf="lxmf")[t] for t in transports
    ]
    if "lxmf" in transports:
        names.append("rns")
    records = {}
    for name in names:
        dist = importlib.metadata.distribution(name)
        raw = dist.read_text("direct_url.json")
        record = {"version": dist.version}
        if raw:
            source = json.loads(raw)
            # Never copy URLs that might carry credentials into a report.
            record["editable"] = bool(source.get("dir_info", {}).get("editable"))
            vcs = source.get("vcs_info", {})
            record["commit_id"] = vcs.get("commit_id")
            if source.get("url", "").startswith("file://"):
                import subprocess

                path = source["url"][7:]
                head = subprocess.run(
                    ["git", "-C", path, "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                )
                dirty = subprocess.run(
                    [
                        "git",
                        "-C",
                        path,
                        "status",
                        "--porcelain",
                        "--untracked-files=no",
                    ],
                    capture_output=True,
                    text=True,
                )
                if head.returncode == 0:
                    record["commit_id"] = head.stdout.strip()
                    record["tracked_changes"] = bool(dirty.stdout.strip())
        records[name] = record
    return records


def configure(config, transports, role, run):
    hw = config["hardware"]
    peer = 1 - role
    os.environ["MEDRE_LAB_RUN_DIR"] = str(run)
    listeners, senders, destinations, endpoints = {}, {}, {}, []
    if "meshtastic" in transports:
        entry = hw["meshtastic"]
        os.environ["MESHTASTIC_PEER_SERIAL_PORT"] = entry["ports"][peer]
        os.environ["MESHTASTIC_PEER_CHANNEL"] = str(entry["channel"])
        from .native.meshtastic_live_peer import (
            MeshtasticPeerListener,
            run_meshtastic_peer,
        )

        listeners["meshtastic"] = MeshtasticPeerListener
        senders["meshtastic"] = lambda nonce, port=entry["ports"][
            peer
        ]: run_meshtastic_peer(["sendn", port, json.dumps([nonce])], hw["send_timeout"])
        endpoints += entry["ports"]
        destinations["meshtastic"] = str(entry["channel"])
    if "meshcore" in transports:
        entry = hw["meshcore"]
        os.environ["MESHCORE_PEER_BLE_ADDRESS"] = entry["addresses"][peer]
        os.environ["MESHCORE_PEER_CHANNEL"] = str(entry["channel"])
        if hw.get("ble_adapter"):
            os.environ["MESHCORE_PEER_BLE_ADAPTER"] = hw["ble_adapter"]
        from .native.meshcore_live_peer import MeshCorePeerListener, run_meshcore_peer

        listeners["meshcore"] = MeshCorePeerListener
        # Bind values now: other transports also use a local entry variable.
        address = entry["addresses"][peer]
        senders["meshcore"] = lambda nonce: run_meshcore_peer(
            ["sendn", address, json.dumps([nonce])], hw["send_timeout"]
        )
        endpoints += entry["addresses"]
        destinations["meshcore"] = str(entry["channel"])
    if "lxmf" in transports:
        entry = hw["lxmf"]
        os.environ["LXMF_PEER_RNS_CONFIG"] = entry["profiles"][peer]
        os.environ["LXMF_PEER_IDENTITY"] = entry["identities"][peer]
        os.environ["LXMF_PEER_DELIVERY_TIMEOUT_SECONDS"] = str(
            hw["observation_timeout"]
        )
        from .native.lxmf_live_peer import (
            LxmfPeerListener,
            delivery_dest_hash,
            run_lxmf_peer,
        )

        listeners["lxmf"] = LxmfPeerListener
        target = delivery_dest_hash(entry["identities"][role])
        senders["lxmf"] = lambda nonce: run_lxmf_peer(
            ["send", target, json.dumps([nonce])], hw["send_timeout"]
        )
        from .radio_resources import rnode_ports

        ports = [rnode_ports(profile) for profile in entry["profiles"]]
        if set(ports[0]) & set(ports[1]):
            raise ValueError("both RNS profiles select the same physical RNode")
        endpoints += entry["profiles"] + entry["identities"] + ports[0] + ports[1]
        destinations["lxmf"] = delivery_dest_hash(entry["identities"][peer])
    return listeners, senders, destinations, endpoints


def verify_receipts(run, legs):
    with sqlite3.connect(
        f"file:{run / 'state.sqlite'}?mode=ro&immutable=1", uri=True
    ) as db:
        for leg in legs:
            rows = db.execute(
                "SELECT DISTINCT event_id FROM event_body_content WHERE instr(body,?)>0",
                (leg["nonce"],),
            ).fetchall()
            if len(rows) != 1:
                raise AssertionError("nonce did not resolve to one canonical event")
            event_id = rows[0][0]
            rows = db.execute(
                "SELECT status,failure_kind FROM delivery_receipts WHERE event_id=? AND target_adapter=?",
                (event_id, leg["destination"]),
            ).fetchall()
            if ("sent", None) not in rows or any(
                f is not None or s in ("failed", "dead_lettered", "denied")
                for s, f in rows
            ):
                raise AssertionError(
                    "destination lacks successful failure-free receipt"
                )
            leg.update(event_id=event_id, sent_receipt=True)


async def run_pair(config, transports, role, run):
    from .native.live_peer_common import async_peer_context

    listeners, senders, destinations, endpoints = configure(
        config, transports, role, run
    )
    result = {
        "pair": list(transports),
        "role": "a" if role == 0 else "b",
        "pass": False,
        "legs": [],
        "max_loop_lag_seconds": 0.0,
        "packages": provenance(transports),
        "scope": "One ordinary text message per direction; not full release qualification.",
    }
    app = None
    hw = config["hardware"]

    async def heartbeat():
        while True:
            before = time.monotonic()
            await asyncio.sleep(0.05)
            result["max_loop_lag_seconds"] = max(
                result["max_loop_lag_seconds"], time.monotonic() - before - 0.05
            )

    monitor = asyncio.create_task(heartbeat())
    try:
        with own(endpoints):
            try:
                app = build(config, transports, role, run, destinations)
                await bounded(app.start(), hw["startup_timeout"])
                for adapter in app.adapters.values():
                    health = await bounded(adapter.health_check(), 15)
                    if health.health != "healthy":
                        raise RuntimeError("adapter did not report healthy at startup")
                # Receiver lifetimes must not include adapter startup or an
                # arbitrarily long configured drain. Arm each receiver only
                # after the initial drain has passed, before its leg sends.
                result["startup_drain_wait"] = await wait_startup_drain_retired(app)
                for source, destination in (transports, transports[::-1]):
                    async with async_peer_context(
                        listeners[destination](
                            hw["startup_timeout"]
                            + hw["send_timeout"]
                            + hw["observation_timeout"]
                            + 20
                        )
                    ) as receiver:
                        nonce = "MLAB-" + uuid.uuid4().hex[:12]
                        before = time.monotonic()
                        sent = await asyncio.to_thread(senders[source], nonce)
                        observed = await asyncio.to_thread(
                            receiver.packets_until,
                            lambda ps: any(
                                nonce in str(p.get("text", p.get("content", "")))
                                for p in ps
                            ),
                            hw["observation_timeout"],
                        )
                        seen = [
                            p
                            for p in observed
                            if nonce in str(p.get("text", p.get("content", "")))
                        ]
                        leg = {
                            "source": source,
                            "destination": destination,
                            "nonce": nonce,
                            "native_send": sent,
                            "observed": seen,
                            "elapsed_seconds": time.monotonic() - before,
                        }
                        result["legs"].append(leg)
                        if (
                            len(seen) != 1
                            or str(
                                seen[0].get("text", seen[0].get("content", ""))
                            ).count(nonce)
                            != 1
                        ):
                            raise AssertionError(
                                "expected exactly one correlated far-side delivery"
                            )
                        if source == "lxmf" and (
                            len(sent.get("sent", [])) != 1
                            or not sent["sent"][0].get("delivered")
                            or sent["sent"][0].get("state") != 8
                        ):
                            raise AssertionError(
                                "LXMF sender did not reach terminal DELIVERED"
                            )
            finally:
                if app is not None:
                    await bounded(app.stop(), 20)
                    app = None
            verify_receipts(run, result["legs"])
            result["pass"] = True
    except BaseException as exc:
        # Logs and detailed evidence are private; stdout only reports status/path.
        result["error"] = type(exc).__name__ + ": " + str(exc)[-1600:]
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        (run / "verdict.json").write_text(
            json.dumps(result, indent=2, default=str) + "\n"
        )
    return result["pass"]


def main():
    path, selection, role, output = sys.argv[1:]
    ok = asyncio.run(
        run_pair(load(Path(path)), pair(selection), int(role), Path(output))
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
