"""Collecting radio sniffer for the medre interop scenarios.

Runs INSIDE the lab-tools container (mtjk/meshtastic installed). A
companion to radio.py's `listen` (same connection discipline: PEER nodes
only, one client per node, tagged listeners) that COLLECTS every text
packet in a window instead of exiting at the first match. Loop-inertness
and single-transmission assertions need the full population of packets,
not just the first one.

Subcommands:
    collect HOST PORT --timeout S [--contains X] --tag T
        Print {"listening": true, ...} on readiness, record every
        TEXT_MESSAGE_APP packet seen until the deadline, then print
        {"packets": [...], "count": N, "transport_drops": [...]} and exit 0
        (empty captures are a valid result; the scenario interprets them).
    info HOST PORT
        Node identity JSON (re-exported from radio.py).

Packet views match radio.py listen: {packet_id, from_id, text, reply_id,
emoji, channel}.

Poisoned windows: the meshtastic client logs "Dropped N queued packet(s)
before reconnect config" when its API connection was evicted/reconnected
mid-window and the transport lost queued packets (c7d3d5a class). Those
warnings are captured from the meshtastic logger into
"transport_drops": an ABSENCE collector reporting count=0 over a window
with transport_drops must not count the zero as clean evidence - the
scenario layer FAILs on that combination.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

import radio  # same scenarios/lib directory: connection + packet-view helpers


def collect(
    host: str, port: int, timeout: float, contains: str | None, tag: str | None
) -> int:
    from pubsub import pub

    # radio.TransportDropCapture (reachable as medre_radio.TransportDropCapture
    # through the module import): the poisoned-window signature capture.
    drops = radio.TransportDropCapture()
    logging.getLogger().addHandler(drops)

    seen: list[dict] = []

    def _on_packet(packet, interface) -> None:  # noqa: ANN001 - pubsub signature
        if isinstance(packet, dict):
            view = radio._packet_view(packet)
            if view is not None:
                seen.append(view)

    interface = radio.connect(host, port)
    pub.subscribe(_on_packet, "meshtastic.receive")
    print(
        json.dumps({"listening": True, "host": host, "port": port, "tag": tag}),
        flush=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            time.sleep(0.4)
    finally:
        try:
            pub.unsubscribe(_on_packet, "meshtastic.receive")
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        try:
            interface.close()
        except Exception:  # noqa: BLE001
            pass
    matches = (
        [view for view in seen if contains in (view.get("text") or "")]
        if contains is not None
        else list(seen)
    )
    print(
        json.dumps(
            {
                "packets": matches,
                "count": len(matches),
                "seen_total": len(seen),
                "contains": contains,
                "transport_drops": drops.messages,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect")
    p.add_argument("host")
    p.add_argument("port", type=int)
    p.add_argument("--timeout", type=float, default=45.0)
    p.add_argument("--contains", default=None)
    p.add_argument("--tag", default=None)

    p = sub.add_parser("info")
    p.add_argument("host")
    p.add_argument("port", type=int)

    args = parser.parse_args()
    if args.command == "info":
        try:
            interface = radio.connect(args.host, args.port)
        except Exception as exc:  # noqa: BLE001 - report as JSON
            print(json.dumps({"error": f"cannot connect: {exc}"}))
            return 1
        try:
            print(json.dumps(radio.node_info(interface)))
            return 0
        finally:
            try:
                interface.close()
            except Exception:  # noqa: BLE001
                pass
    try:
        return collect(args.host, args.port, args.timeout, args.contains, args.tag)
    except Exception as exc:  # noqa: BLE001 - report as JSON
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
