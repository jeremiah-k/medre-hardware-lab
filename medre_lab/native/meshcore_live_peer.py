"""Native MeshCore peer harness shared by live hardware tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from medre_lab.ble_link import BlueZLink
from medre_lab.native.live_peer_common import (
    PeerProcess,
    poll_packets_until,
    read_jsonl,
)

_PEER_BLE = os.environ.get("MESHCORE_PEER_BLE_ADDRESS", "")
_PEER_READY_TIMEOUT: float = 60.0

_PEER_SCRIPT = r'''
import asyncio, json, subprocess, sys, time, os

ADDRESS = sys.argv[2]
MODE = sys.argv[1]


async def _connect():
    from meshcore import EventType
    from meshcore import MeshCore
    last = None
    # Sizing from measured board behavior: the boards resume advertising
    # ~1.1 s after a clean disconnect and connect in ~1.5-1.8 s, so a scan
    # finds a healthy board in under a second. The retry budget exists to
    # ride out a transient advertising gap of about half a minute before
    # failing honestly; each attempt reports its failure to stderr.
    for attempt in range(4):
        if attempt:  # remedy only after a failed attempt, not proactively
            pass  # caller owns controller selection and recovery
            await asyncio.sleep(1.5)
        try:
            # Match the real adapter's pre-discovered-device path. Address-only
            # factories can select a stale/wrong controller on multi-HCI hosts.
            import os
            from bleak import BleakScanner
            scan = {"timeout": 8.0}
            if os.environ.get("MESHCORE_PEER_BLE_ADAPTER"):
                scan["adapter"] = os.environ["MESHCORE_PEER_BLE_ADAPTER"]
            device = await BleakScanner.find_device_by_address(ADDRESS, **scan)
            if device is None:
                raise RuntimeError("owned MeshCore peer was not advertised")
            mc = await asyncio.wait_for(
                MeshCore.create_ble(
                    address=ADDRESS, device=device, auto_reconnect=False,
                    default_timeout=10,
                ),
                timeout=25,
            )
            if mc is not None:
                return mc
            last = "None"
        except Exception as exc:
            last = type(exc).__name__
        print(f"peer connect attempt {attempt}: {last}", file=sys.stderr, flush=True)
    raise RuntimeError(f"peer connect failed: {last}")


async def _own_pubkey_prefix(mc):
    """First 12 hex of this board's public key via self contact export."""
    import re as _re
    try:
        ec = await asyncio.wait_for(mc.commands.export_contact(), timeout=10)
        blob = json.dumps(ec.payload, default=str) + json.dumps(
            ec.attributes, default=str
        )
        m = _re.search(r"[0-9a-fA-F]{64}", blob)
        if m:
            return m.group(0)[:12]
    except Exception:
        pass
    from meshcore import EventType
    got = {}

    def on_info(event):
        p = event.payload or {}
        pk = p.get("public_key") or ""
        if pk and "prefix" not in got:
            got["prefix"] = pk[:12]

    sub = mc.subscribe(EventType.SELF_INFO, on_info)
    try:
        await asyncio.sleep(2.5)
    finally:
        try:
            mc.unsubscribe(sub)
        except Exception:
            pass
    return got.get("prefix")


async def main():
    from meshcore import EventType
    out = {"mode": MODE, "sent": [], "received": [], "own_pubkey_prefix": None}

    if MODE == "listen":
        seconds = float(sys.argv[3])
        scratch_path = sys.argv[4]
        ready_path = sys.argv[5]
        mc = await _connect()
        try:
            out["own_pubkey_prefix"] = await _own_pubkey_prefix(mc)
            # The firmware replays buffered group messages to a newly
            # connected app; drain that queue BEFORE arming collection so
            # a previous run's traffic cannot satisfy this window.  Bounded
            # and non-fatal: a misbehaving queue must not hang the listener.
            for _ in range(10):
                try:
                    ev = await asyncio.wait_for(
                        mc.commands.get_msg(), timeout=4
                    )
                except Exception:
                    break
                if ev.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
                    break
            got = []
            raw = []

            def on_any(event):
                raw.append(f"{event.type.value}:{str(event.payload)[:60]}")
                if event.type == EventType.DISCONNECTED:
                    with open(scratch_path, "a") as fh:
                        fh.write(json.dumps({"observation_gap": True, "observed_monotonic": time.monotonic()}) + "\n")

            raw_sub = mc.subscribe(None, on_any)

            def on_msg(event):
                p = event.payload or {}
                got.append({
                    "text": p.get("text") or p.get("message", ""),
                    "sender": (p.get("pubkey_prefix") or "")[:12],
                    "channel": p.get("channel_idx"),
                    "snr": p.get("SNR", p.get("snr")),
                    "sender_timestamp": p.get("sender_timestamp"),
                    "observed_monotonic": time.monotonic(),
                })
                with open(scratch_path, "a") as fh:
                    fh.write(json.dumps(got[-1]) + "\n")

            sub = mc.subscribe(EventType.CHANNEL_MSG_RECV, on_msg)
            await mc.start_auto_message_fetching()
            # Ready handshake: collection is armed (drained, subscribed).
            with open(ready_path, "w") as fh:
                fh.write("1")
            await asyncio.sleep(seconds)
            mc.unsubscribe(sub)
            try:
                mc.unsubscribe(raw_sub)
            except Exception:
                pass
            out["received"] = got
            out["raw_events"] = raw
        finally:
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=8)
            except Exception:
                pass
    elif MODE in ("sendn", "sendts"):
        mc = await _connect()
        try:
            out["own_pubkey_prefix"] = await _own_pubkey_prefix(mc)
            if MODE == "sendn":
                texts = json.loads(sys.argv[3])
                for text in texts:
                    res = await asyncio.wait_for(
                        mc.commands.send_chan_msg(int(os.environ.get("MESHCORE_PEER_CHANNEL", "1")), text), timeout=15)
                    out["sent"].append({
                        "text": text,
                        "type": res.type.value,
                        "error": res.is_error(),
                    })
                    await asyncio.sleep(3.0)
            else:
                # Controlled sender-set wire timestamps (supported SDK arg):
                # same text, explicit one-second-resolution timestamps.
                text = sys.argv[3]
                ts_a, ts_b = int(sys.argv[4]), int(sys.argv[5])
                for ts in (ts_a, ts_b):
                    res = await asyncio.wait_for(
                        mc.commands.send_chan_msg(int(os.environ.get("MESHCORE_PEER_CHANNEL", "1")), text, timestamp=ts), timeout=15)
                    out["sent"].append({
                        "text": text, "timestamp": ts,
                        "type": res.type.value, "error": res.is_error(),
                    })
                    await asyncio.sleep(3.0)
        finally:
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=8)
            except Exception:
                pass
    elif MODE == "probe":
        # Preflight: prove the board is connectable and time-synced.
        mc = await _connect()
        try:
            out["own_pubkey_prefix"] = await _own_pubkey_prefix(mc)
            t = await asyncio.wait_for(mc.commands.get_time(), timeout=10)
            dev = (t.payload or {}).get("unix_time") or (t.payload or {}).get("time")
            out["drift_s"] = (dev - int(time.time())) if dev else None
        finally:
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=8)
            except Exception:
                pass

    print(json.dumps(out))


asyncio.run(main())
'''


def run_meshcore_peer(args: list[str], timeout: float) -> dict:
    """Run the native peer script once and return its JSON payload."""
    arity = {"probe": 2, "sendn": 3, "sendts": 5, "listen": 5}
    if not args or args[0] not in arity or len(args) != arity[args[0]]:
        raise ValueError(
            "MeshCore peer requires mode, BLE address and mode-specific arguments"
        )
    proc = subprocess.run(
        [sys.executable, "-c", _PEER_SCRIPT, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"native peer failed ({proc.returncode}): {proc.stderr[-800:]}"
        )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise AssertionError(f"native peer produced no JSON: {proc.stderr[-400:]}")
    return json.loads(lines[-1])


class MeshCorePeerListener:
    """Background native-peer BLE listener with incremental collection.

    The peer appends each received packet to a JSONL scratch file as it
    arrives, so positive cases finish as soon as the expected evidence
    lands (``packets_until``) while absence cases still drain a full
    window (``packets``).  Readiness is a file handshake: arming happens
    after the firmware buffer drain + subscribe, not after a fixed sleep.
    Process/readiness/teardown ownership lives in
    :class:`~tests.helpers.live_peer_common.PeerProcess`.

    Host-link hygiene lives at this owning boundary too: the child's async
    SDK teardown can leave the BlueZ ACL link lingering after the child
    retires, and a still-connected board does not advertise, so a re-arm
    child would die honestly while discovering nothing. Arming therefore
    first recovers a stale link (documented disconnect), and disarming
    verifies the drop with a bounded wait, healing a lingering link the
    same way (:mod:`medre_lab.ble_link`).
    """

    _JSON_PATH = Path(os.environ["MEDRE_LAB_RUN_DIR"]) / "meshcore_pair_peer.json"
    _READY_PATH = Path(os.environ["MEDRE_LAB_RUN_DIR"]) / "meshcore_pair_peer.ready"
    _READY_TIMEOUT = _PEER_READY_TIMEOUT

    def __init__(self, seconds: float, link: BlueZLink | None = None) -> None:
        self._seconds = seconds
        self._owner = PeerProcess(self._READY_PATH, self._READY_TIMEOUT)
        if link is None:
            link = BlueZLink(
                _PEER_BLE,
                os.environ.get("MESHCORE_PEER_BLE_ADAPTER") or None,
                Path(os.environ.get("MEDRE_LAB_RUN_DIR", ".")),
            )
        self._link = link

    def __enter__(self) -> "MeshCorePeerListener":
        self._JSON_PATH.unlink(missing_ok=True)
        self._READY_PATH.unlink(missing_ok=True)
        # Owned hygiene: a stale host link holds the board silent; recover
        # it with the documented disconnect before the child scans, or
        # refuse the arm honestly.
        self._link.ensure_available("arm")
        try:
            # Ready handshake: drain + subscribe + auto-fetch armed before
            # any MEDRE-side TX (bounded; no fixed startup sleep).
            self._owner.start(
                [
                    sys.executable,
                    "-c",
                    _PEER_SCRIPT,
                    "listen",
                    _PEER_BLE,
                    str(self._seconds),
                    str(self._JSON_PATH),
                    str(self._READY_PATH),
                ]
            )
        except BaseException:
            # A failed arm still owns the link: the child may have reached
            # the board before dying (attempt-level ConnectionError).
            self._link.verify_dropped("arm-failed")
            raise
        return self

    def _read_packets(self) -> list[dict]:
        packets = read_jsonl(self._JSON_PATH)
        if any(packet.get("observation_gap") for packet in packets):
            raise AssertionError("MeshCore witness disconnected during collection")
        return packets

    def packets_until(
        self,
        predicate: Callable[[list[dict]], bool],
        timeout: float,
    ) -> list[dict]:
        """Poll collected packets until ``predicate`` holds or timeout."""
        return poll_packets_until(self._read_packets, predicate, timeout)

    def packets(self, timeout: float | None = None) -> list[dict]:
        """Drain the listener's full window (absence/negative evidence)."""
        out, err = self._owner.communicate(timeout=timeout or self._seconds + 40)
        assert self._owner.proc is not None
        if self._owner.proc.returncode != 0:
            raise AssertionError(f"native peer listener failed: {err[-800:]}")
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        return json.loads(lines[-1])["received"] if lines else []

    def __exit__(self, *exc: object) -> None:
        self._owner.terminate()
        # Owned teardown verification: SIGINT (or the kill ladder) retires
        # the child, but its async SDK disconnect can leave the BlueZ ACL
        # link up indefinitely. Await the drop, heal a lingering link with
        # the documented disconnect, and fail honestly if it cannot drop.
        self._link.verify_dropped("disarm")
