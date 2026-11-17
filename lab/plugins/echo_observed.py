"""echo_observed: a transparent observation plugin for hardware-lab scenarios.

REQUIREMENT FOR THE RIG (scenarios/README.md documents this too):
  * bind-mount this file into each mmrelay container at
    ``<mmrelay home>/plugins/custom/echo_observed.py`` (the relays run with
    ``--home /data``, so ``/data/plugins/custom/echo_observed.py``);
  * add ``custom-plugins: {echo_observed: {active: true}}`` to each relay's
    generated config (or the equivalent config section);
  * mount a directory writable by the relays and visible to lab-tools (the
    ``./lab/logs`` bind works) and set ``ECHO_OBSERVED_PATH`` per relay to a
    per-relay JSONL file inside it, e.g.
    ``ECHO_OBSERVED_PATH=/lab-logs/scenarios/echo-observed-a.jsonl``.

Behavior contract: this plugin NEVER claims events (every handler returns
False) and never sends anything; it only appends a JSON line per observed
Matrix room message and Meshtastic packet so the blank-reply-plugin scenario
can prove which events stayed observable through the relay's normal path.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from mmrelay.plugins.base_plugin import BasePlugin

PLUGIN_NAME = "echo_observed"


def _observation_path() -> str:
    return os.environ.get(
        "ECHO_OBSERVED_PATH",
        os.path.join(os.path.expanduser("~"), "echo_observed.jsonl"),
    )


class Plugin(BasePlugin):
    plugin_name = PLUGIN_NAME

    def __init__(self, plugin_name: str | None = None) -> None:
        super().__init__(plugin_name or PLUGIN_NAME)

    def get_matrix_commands(self) -> list[str]:
        # Never claim Matrix commands: claiming would suppress relay delivery
        # and change the behavior under observation.
        return []

    def _record(self, kind: str, **fields: Any) -> None:
        record = {"ts": time.time(), "kind": kind, **fields}
        try:
            path = _observation_path()
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                handle.flush()
            self.logger.debug("echo_observed recorded %s event", kind)
        except Exception:  # noqa: BLE001 - observation must never break the relay
            self.logger.exception("echo_observed failed to record %s event", kind)

    async def handle_room_message(self, room, event, full_message: str) -> bool:
        self._record(
            "matrix",
            room_id=getattr(room, "room_id", None),
            sender=getattr(event, "sender", None),
            event_id=getattr(event, "event_id", None),
            event_type=type(event).__name__,
            body=full_message if isinstance(full_message, str) else str(full_message),
        )
        return False

    async def handle_meshtastic_message(
        self, packet, formatted_message, longname, meshnet_name
    ) -> bool:
        decoded = packet.get("decoded", {}) if isinstance(packet, dict) else {}
        self._record(
            "meshtastic",
            packet_id=packet.get("id") if isinstance(packet, dict) else None,
            from_id=packet.get("fromId") if isinstance(packet, dict) else None,
            text=(decoded.get("text") if isinstance(decoded, dict) else None),
            longname=longname,
            meshnet_name=meshnet_name,
        )
        return False
