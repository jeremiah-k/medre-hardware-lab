"""Owned host-BLE-link hygiene for peer boards the lab drives over BlueZ.

The lab owns the peer board's host-link lifecycle boundary: after a peer
listener child retires, the BlueZ ACL link to the board must actually be
gone — a board that is still ``Connected: yes`` does not advertise, and a
re-arm child then dies honestly while trying to discover it (the
deterministic round-2 campaign blocker: the child's SDK teardown can leave
the ACL link lingering indefinitely while every child-side exception is
swallowed). The owning boundary therefore *verifies* the drop with a
bounded wait, and when the link lingers performs the documented clean
``busctl Disconnect`` heal that provably restores advertising. Before an
arm, a stale link is recovered the same way, or the arm is refused.

This is machinery hygiene only: it never touches firmware, pairing, bonds,
keys, or clocks, and it never reaches into released core or provider SDKs.
Every stage appends one JSONL evidence record to the run directory.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

#: Boards resume advertising ~1.1 s after a clean disconnect, so this bound
#: rides out scheduler delay on a natural drop while staying far below the
#: re-arm readiness budget.
DROP_WAIT_SECONDS = 12.0
POLL_INTERVAL_SECONDS = 0.5
#: Bounded heal ladder after a lingering link: the documented disconnect,
#: then re-verify. A second attempt covers a transient D-Bus failure.
HEAL_ATTEMPTS = 2
HEAL_WAIT_SECONDS = 6.0

Runner = Callable[..., subprocess.CompletedProcess]


class LinkHygieneError(AssertionError):
    """The peer board's host link could not be verified dropped."""


def device_object_path(controller: str, address: str) -> str:
    """BlueZ object path for a device, matching the owned disconnect form."""
    return f"/org/bluez/{controller}/dev_{address.upper().replace(':', '_')}"


class BlueZLink:
    """Verify-and-heal ownership for one board's host BLE link."""

    def __init__(
        self,
        address: str,
        controller: str | None,
        run_dir: Path | None = None,
        *,
        runner: Runner = subprocess.run,
        log_name: str = "meshcore-peer-link-hygiene.jsonl",
        drop_wait: float = DROP_WAIT_SECONDS,
        heal_wait: float = HEAL_WAIT_SECONDS,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        if not address:
            raise ValueError("peer BLE address is required for link hygiene")
        self._address = address
        self._controller = controller
        self._run_dir = run_dir
        self._runner = runner
        self._log_path = run_dir / log_name if run_dir is not None else None
        self._drop_wait = drop_wait
        self._heal_wait = heal_wait
        self._poll_interval = poll_interval

    # ------------------------------------------------------------------
    # BlueZ primitives (busctl with a controller, bluetoothctl otherwise)
    # ------------------------------------------------------------------

    def _bluetoothctl(self, *args: str) -> str:
        result = self._runner(
            ["bluetoothctl", *args], capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"bluetoothctl {' '.join(args)} failed ({result.returncode}): "
                f"{result.stderr.strip()[-200:]}"
            )
        return result.stdout

    def _busctl(self, *args: str) -> str:
        result = self._runner(
            ["busctl", *args], capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"busctl {' '.join(args[:2])} failed ({result.returncode}): "
                f"{result.stderr.strip()[-200:]}"
            )
        return result.stdout

    def is_connected(self) -> bool | None:
        """Read the BlueZ Connected property; ``None`` when undecidable."""
        try:
            if self._controller:
                out = self._busctl(
                    "get-property",
                    "org.bluez",
                    device_object_path(self._controller, self._address),
                    "org.bluez.Device1",
                    "Connected",
                )
                return out.strip().endswith("true")
            out = self._bluetoothctl("info", self._address)
            match = re.search(r"\bConnected:\s*(yes|no)\b", out)
            return {"yes": True, "no": False}[match.group(1)] if match else None
        except Exception:
            return None

    def _documented_disconnect(self) -> None:
        """The campaign's owned heal: one clean host-initiated disconnect."""
        if self._controller:
            self._busctl(
                "call",
                "org.bluez",
                device_object_path(self._controller, self._address),
                "org.bluez.Device1",
                "Disconnect",
            )
        else:
            self._bluetoothctl("disconnect", self._address)

    def wait_dropped(self, timeout: float) -> bool | None:
        """Poll until the link is proven gone or the bound expires.

        Returns ``True`` (proven ``Connected: no``), ``False`` (still
        ``Connected: yes`` at the bound), or ``None`` (never readable).
        """
        deadline = time.monotonic() + timeout
        while True:
            connected = self.is_connected()
            if connected is False:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None if connected is None else False
            time.sleep(min(self._poll_interval, remaining))

    # ------------------------------------------------------------------
    # Owned stages
    # ------------------------------------------------------------------

    def ensure_available(self, stage: str) -> dict:
        """Before an arm: recover a stale host link, or refuse honestly.

        A connected board does not advertise, so arming a listener against
        one is a deterministic failure. The documented disconnect heals a
        stale link; a board that stays connected through the bounded ladder
        refuses the arm.
        """
        record: dict = {"stage": stage, "at": time.monotonic(), "heals": 0}
        connected = self.is_connected()
        record["connected"] = connected
        for _ in range(HEAL_ATTEMPTS):
            if connected is not True:
                break
            try:
                self._documented_disconnect()
            except RuntimeError as exc:
                record.setdefault("errors", []).append(str(exc)[-200:])
                connected = self.is_connected()
                record["connected"] = connected
                continue
            record["heals"] += 1
            dropped = self.wait_dropped(self._heal_wait)
            connected = None if dropped is None else not dropped
            record["connected"] = connected
        # ``known_clear`` is False when the state is undecidable (unreadable):
        # pre-arm that is not a refusal — the child's bounded connect loop
        # reports the board honestly — it just is not proven clear here.
        record["known_clear"] = connected is False
        self._append(record)
        if connected is True:
            raise LinkHygieneError(
                f"stale peer host link refused the documented disconnect: "
                f"{self._address} stays Connected: yes ({stage})"
            )
        return record

    def verify_dropped(self, stage: str) -> dict:
        """After a disarm: await the drop; heal a lingering link; verify.

        The child's async SDK teardown may leave the ACL link up even after
        the child retires. The owning boundary gives the natural drop a
        bounded wait, then performs the documented disconnect, then requires
        a proven ``Connected: no`` — an unverifiable or lingering link is an
        honest failure, never a padded pass.
        """
        record: dict = {"stage": stage, "at": time.monotonic(), "heals": 0}
        dropped = self.wait_dropped(self._drop_wait)
        for _ in range(HEAL_ATTEMPTS):
            if dropped is True:
                break
            try:
                self._documented_disconnect()
            except RuntimeError as exc:
                record.setdefault("errors", []).append(str(exc)[-200:])
                continue
            record["heals"] += 1
            dropped = self.wait_dropped(self._heal_wait)
        if dropped is not True:
            record["dropped"] = False
            record["verified"] = False
            self._append(record)
            raise LinkHygieneError(
                "peer host link did not drop after listener teardown: "
                f"{self._address} last read Connected: "
                f"{'unreadable' if dropped is None else 'yes'} ({stage})"
            )
        record["dropped"] = True
        record["verified"] = True
        record["connected"] = False
        self._append(record)
        return record

    # ------------------------------------------------------------------

    def _append(self, record: dict) -> None:
        """Append one hygiene evidence record; never mask the outcome."""
        if self._log_path is None:
            return
        try:
            record["address_role"] = "peer"
            with self._log_path.open("a") as stream:
                stream.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass
