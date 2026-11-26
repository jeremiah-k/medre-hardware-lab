#!/usr/bin/env python3
"""Companion matrix peer probe: the virtual MeshCore peer on companion B.

The probe plays ONE role: it is the **only** client of companion B
(``openhop-repeater-b:5000``). It never connects to companion A - MEDRE
is the only client there - so the single-client rule of the openHop
companion server is never violated.

Modes
-----
``send``
    Connect to companion B, submit N unique channel-0 nonces, and report
    what the companion answered.  Every send result is captured, so an
    explicit failure (``ERROR``) is reported as a failure and never
    mistaken for a delivered message.

``listen``
    Connect to companion B and drain the companion queue the way a real
    client does (the SDK's own auto-fetch path), counting the frames that
    carry a needle.  Used to count far-side arrivals and echoes.

Both modes report the GET_MSG commands the client issued and how many of
them were in flight at once, so the evidence separates "the companion
delivered the message twice" from "the client asked twice".

Evidence: one JSON object on stdout.  Exit 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timezone

CONNECT_TIMEOUT_SECONDS = 20.0
COMMAND_TIMEOUT_SECONDS = 20.0
DEFAULT_SETTLE_SECONDS = 8.0


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class MCDrain:
    """Drains the MC peer's companion message queue the way a REAL client does.

    The SDK's own auto-fetch path (``start_auto_message_fetching``) issues
    the GET_MSG commands when the companion announces that messages are
    waiting, and it calls ``get_msg()`` with **no timeout**.  A
    ``get_msg(timeout=...)`` that times out leaves its GET_MSG command
    PENDING at the companion, so a polling loop can stack two commands at
    the server and the server can serve the same message to both.

    The counters below make that distinction visible: ``get_msg_calls`` is
    how many GET_MSG commands the client issued and
    ``max_in_flight_get_msg`` is how many were in flight at once.
    """

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.get_msg_calls = 0
        self.max_in_flight_get_msg = 0
        self._in_flight = 0
        self._subscriptions: list = []

    async def start(self, client) -> None:
        import meshcore

        original_get_msg = client.commands.get_msg

        async def counted_get_msg(*args, **kwargs):
            self.get_msg_calls += 1
            self._in_flight += 1
            self.max_in_flight_get_msg = max(
                self.max_in_flight_get_msg, self._in_flight
            )
            try:
                return await original_get_msg(*args, **kwargs)
            finally:
                self._in_flight -= 1

        client.commands.get_msg = counted_get_msg

        def on_event(event):
            event_type = str(getattr(event, "type", ""))
            if event_type not in (
                str(meshcore.EventType.CHANNEL_MSG_RECV),
                str(meshcore.EventType.CONTACT_MSG_RECV),
            ):
                return
            payload = getattr(event, "payload", None) or {}
            self.messages.append(
                {
                    "received_at": utcnow(),
                    "event_type": event_type,
                    "channel_idx": payload.get("channel_idx"),
                    "text": payload.get("text"),
                }
            )

        for event_type in (
            meshcore.EventType.CHANNEL_MSG_RECV,
            meshcore.EventType.CONTACT_MSG_RECV,
        ):
            self._subscriptions.append(client.subscribe(event_type, on_event))

        await client.start_auto_message_fetching()

    def stop(self, client) -> None:
        for subscription in self._subscriptions:
            try:
                subscription.unsubscribe()
            except Exception:  # pragma: no cover - best effort
                pass
        self._subscriptions = []

    def count(self, needle: str) -> int:
        return sum(
            1 for entry in self.messages if needle in str(entry.get("text") or "")
        )


async def connect(host: str, port: int):
    import meshcore

    return await asyncio.wait_for(
        meshcore.MeshCore.create_tcp(host, port, auto_reconnect=False),
        timeout=CONNECT_TIMEOUT_SECONDS,
    )


async def run_send(args) -> dict:
    result: dict = {
        "case": args.case,
        "mode": "send",
        "label": "protocol-simulated",
        "started_at": utcnow(),
        "host": args.host_b,
        "port": args.port_b,
        "channel_index": args.channel,
        "nonces": [],
        "sends": [],
        "failures": [],
    }

    try:
        client = await connect(args.host_b, args.port_b)
    except Exception as exc:  # pragma: no cover - defensive
        result["failures"].append(
            f"connect to {args.host_b}:{args.port_b} failed: {exc!r}"
        )
        result["finished_at"] = utcnow()
        result["pass"] = False
        return result

    if client is None:
        result["failures"].append("companion B did not answer the APP_START handshake")
        result["finished_at"] = utcnow()
        result["pass"] = False
        return result

    drain = MCDrain()
    try:
        await drain.start(client)
    except Exception as exc:  # pragma: no cover - defensive
        result["failures"].append(f"could not start the client drain: {exc!r}")

    try:
        for index in range(args.count):
            nonce = f"{args.prefix}-{index}-{uuid.uuid4().hex[:8]}"

            result["nonces"].append(nonce)
            entry: dict = {"nonce": nonce, "started_at": utcnow()}
            try:
                event = await asyncio.wait_for(
                    client.commands.send_chan_msg(args.channel, nonce),
                    timeout=COMMAND_TIMEOUT_SECONDS,
                )
                entry["send_event"] = str(getattr(event, "type", ""))
                entry["finished_at"] = utcnow()
                if entry["send_event"] != "EventType.OK":
                    entry["accepted"] = False
                    result["failures"].append(
                        f"companion B refused the send of {nonce}: {entry['send_event']}"
                    )
                else:
                    entry["accepted"] = True
            except Exception as exc:
                entry["send_event"] = f"exception: {exc!r}"
                entry["accepted"] = False
                entry["finished_at"] = utcnow()
                result["failures"].append(f"send of {nonce} failed: {exc!r}")
            result["sends"].append(entry)

        # Give the radio path time to carry the frames while the client is
        # still attached, so an echo at companion B would be counted.
        await asyncio.sleep(args.settle)
    finally:
        drain.stop(client)
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5.0)
        except Exception:  # pragma: no cover - best effort
            pass

    result["echo_at_b"] = drain.count(args.prefix)
    result["get_msg_commands"] = drain.get_msg_calls
    result["max_in_flight_get_msg"] = drain.max_in_flight_get_msg
    result["frames_received"] = len(drain.messages)
    result["accepted_count"] = sum(
        1 for entry in result["sends"] if entry.get("accepted")
    )
    result["finished_at"] = utcnow()
    result["pass"] = not result["failures"]
    return result


async def run_listen(args) -> dict:
    result: dict = {
        "case": args.case,
        "mode": "listen",
        "label": "protocol-simulated",
        "started_at": utcnow(),
        "host": args.host_b,
        "port": args.port_b,
        "needle": args.needle,
        "window_seconds": args.window,
    }

    try:
        client = await connect(args.host_b, args.port_b)
    except Exception as exc:  # pragma: no cover - defensive
        result["failures"] = [f"connect to {args.host_b}:{args.port_b} failed: {exc!r}"]
        result["finished_at"] = utcnow()
        result["pass"] = False
        return result

    drain = MCDrain()
    try:
        await drain.start(client)
        deadline = time.monotonic() + args.window
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
    finally:
        drain.stop(client)
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5.0)
        except Exception:  # pragma: no cover - best effort
            pass

    hits = [
        entry for entry in drain.messages if args.needle in str(entry.get("text") or "")
    ]
    result["arrivals"] = hits
    result["arrival_count"] = len(hits)
    result["distinct_arrival_count"] = len({entry.get("text") for entry in hits})
    result["duplicate_client_events"] = max(
        0, len(hits) - result["distinct_arrival_count"]
    )
    result["get_msg_commands"] = drain.get_msg_calls
    result["max_in_flight_get_msg"] = drain.max_in_flight_get_msg
    result["frames_received"] = len(drain.messages)
    result["finished_at"] = utcnow()
    result["pass"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("send", "listen"), default="send")
    parser.add_argument("--case", default="openhop-peer-probe")
    parser.add_argument("--host-b", default="openhop-repeater-b")
    parser.add_argument("--port-b", type=int, default=5000)
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--prefix", default="openhop-peer-probe")
    parser.add_argument("--needle", default="")
    parser.add_argument("--window", type=float, default=30.0)
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE_SECONDS)
    args = parser.parse_args(argv)

    if args.mode == "listen" and not args.needle:
        parser.error("--mode listen requires --needle")

    if args.mode == "send":
        result = asyncio.run(run_send(args))
    else:
        result = asyncio.run(run_listen(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    sys.exit(main())
