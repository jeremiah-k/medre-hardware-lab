#!/usr/bin/env python3
"""Phase 3B bridge probe: virtual MeshCore peer + independent MeshC observer.

The probe holds TWO independent client roles in one container:

* **MC peer** — the pinned upstream MeshCore SDK (``meshcore==2.3.15``)
  connected to companion **B** (``openhop-repeater-b:5000``). It is the ONLY
  client of companion B. It sends the direction-1 nonce and receives the
  direction-2 arrival.
* **MeshC observer** — a Meshtastic client (the same pinned ``mtjk``
  distribution the meshtastic adapter uses) connected to
  ``meshtasticd-c-peer:4403``. It is the ONLY API client of the peer node.
  It observes the direction-1 arrival and injects the direction-2 nonce.

MEDRE itself is NOT in this container: it owns companion A and
``meshtasticd-c-medre``. Both observers therefore observe traffic they did
not originate.

Direction 1 (MC peer -> MEDRE -> MeshC peer)
    The peer submits a unique channel-0 nonce through companion B. Repeater B
    encrypts it with the channel secret and transmits it; the lab's virtual
    radio carries it to repeater A, whose companion delivers it to MEDRE (its
    only client). MEDRE's meshcore adapter decodes it into a canonical
    event, the bidirectional route ``openhop-a <-> radio-c`` relays it to the
    meshtastic adapter, and the MeshC observer sees it ONCE.

Direction 2 (MeshC peer -> MEDRE -> MC peer)
    The observer injects a unique channel-0 nonce at the peer node. It
    reaches MEDRE over MeshC, MEDRE relays it through the meshcore adapter
    to companion A, repeater A transmits it, the virtual radio carries it to
    repeater B, and the MC peer receives it ONCE.

Expected bytes
    openHop prefixes an outgoing GROUP TEXT with the sending companion's
    node name (``<node name>: <text>``).

    * Direction 1: MEDRE's meshcore codec lifts that prefix off the body
      (it keeps the raw wire text in the native metadata), so the MeshC
      observer sees the bare nonce.
    * Direction 2: the peer node is the ORIGINATOR of the MeshCore text, so
      repeater A stamps ITS companion's node name on the wire and the MC
      peer receives ``<a-node-name>: <nonce>``. The expected prefix is
      passed in by the caller (read from the generated repeater config).

Evidence: one JSON object on stdout. Exit 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

CONNECT_TIMEOUT_SECONDS = 20.0
COMMAND_TIMEOUT_SECONDS = 20.0
DRAIN_POLL_SECONDS = 2.0
MESH_SYNC_SECONDS = 4.0


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class MeshCObserver:
    """Independent observer/injector on the MeshC peer node.

    Collects every TEXT packet this interface receives and can inject a
    TEXT packet on a given channel index. Thread-safe: the Meshtastic
    library delivers packets on its own reader thread.
    """

    def __init__(self, host: str, port: int) -> None:
        from meshtastic.tcp_interface import TCPInterface
        from pubsub import pub

        self._lock = threading.Lock()
        self._texts: list[dict] = []
        self._iface = TCPInterface(hostname=host, portNumber=port)

        def on_packet(packet, iface=None, interface=None, **kwargs):
            active = interface if interface is not None else iface
            if active is not None and active is not self._iface:
                return
            decoded = packet.get("decoded", {}) if isinstance(packet, dict) else {}
            if decoded.get("portnum") != "TEXT_MESSAGE_APP":
                return
            with self._lock:
                self._texts.append(
                    {
                        "received_at": utcnow(),
                        "text": decoded.get("text", ""),
                        "channel_index": packet.get("channel"),
                        "from_id": packet.get("fromId"),
                        "packet_id": packet.get("id"),
                        "to_id": packet.get("toId"),
                    }
                )

        self._callback = on_packet
        pub.subscribe(on_packet, "meshtastic.receive")
        # The node needs its want_config exchange to settle before an
        # immediate send is accepted; the lab's own tooling waits the same
        # way.
        time.sleep(MESH_SYNC_SECONDS)

    def texts(self) -> list[dict]:
        with self._lock:
            return list(self._texts)

    def count(self, needle: str) -> int:
        return sum(1 for entry in self.texts() if needle in entry["text"])

    def wait_for_text(self, needle: str, timeout: float) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hits = [entry for entry in self.texts() if needle in entry["text"]]
            if hits:
                return hits
            time.sleep(0.25)
        return []

    def wait_for_text_settled(
        self, needle: str, timeout: float, settle: float
    ) -> list[dict]:
        """Wait for the needle, then keep listening for `settle` seconds.

        A duplicate client delivery arrives within milliseconds of the
        first one, so counting immediately after the FIRST arrival would
        miss it. This waits for the settle window and then returns every
        arrival seen, which is what makes the single-delivery assertion
        meaningful.
        """
        first = self.wait_for_text(needle, timeout)
        if not first:
            return first
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            time.sleep(0.1)
        return [entry for entry in self.texts() if needle in entry["text"]]

    def inject(self, text: str, channel_index: int) -> dict:
        started = utcnow()
        packet = self._iface.sendText(text, channelIndex=channel_index)
        return {
            "started_at": started,
            "finished_at": utcnow(),
            "text": text,
            "channel_index": channel_index,
            "packet_id": getattr(packet, "id", None),
            "from_id": getattr(packet, "fromId", None),
        }

    def close(self) -> None:
        try:
            from pubsub import pub

            pub.unsubscribe(self._callback, "meshtastic.receive")
        except Exception:  # noqa: BLE001 - best effort
            pass
        try:
            self._iface.close()
        except Exception:  # noqa: BLE001 - best effort
            pass


class MCDrain:
    """Drains the MC peer's companion message queue the way a REAL client does.

    The SDK's own auto-fetch path (``start_auto_message_fetching``) issues the
    GET_MSG commands when the companion announces that messages are waiting,
    and it calls ``get_msg()`` with **no timeout**. This matters: a
    ``get_msg(timeout=...)`` that times out leaves its GET_MSG command
    PENDING at the companion. A drain loop that then issues another GET_MSG
    stacks two commands at the server, and the server can serve the same
    message to both - once from the memory queue, once from the persisted
    row. That is a client-side artefact of the polling loop, not the
    companion's behaviour.

    So the drain here uses the SDK's auto-fetch path and collects the
    messages the DISPATCHER delivers - one entry per frame the companion
    actually sent. ``get_msg_calls`` counts the GET_MSG commands that were
    issued, so the evidence distinguishes "the companion sent the frame
    twice" from "the client asked twice".
    """

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.get_msg_calls = 0
        self.max_in_flight_get_msg = 0
        self._in_flight = 0
        self._lock = asyncio.Lock()
        # The serialization lock is OPTIONAL: the sweep compares the SDK's
        # real auto-fetch path as it is (no lock) against the same path with
        # a single-consumer lock. Set OPENHOP_BRIDGE_GET_MSG_LOCK=1 to
        # serialize the GET_MSG commands.
        self._serialize = os.environ.get("OPENHOP_BRIDGE_GET_MSG_LOCK") == "1"
        self._subscriptions: list = []

    async def start(self, client) -> None:
        import meshcore

        # Count the GET_MSG commands the SDK issues and record how many were
        # in flight at once.
        #
        # The SDK's auto-fetch path registers its "messages waiting"
        # subscription BEFORE its initial get_msg(), so a waiting
        # notification can start the fetch loop while the initial command
        # is still in flight - two concurrent GET_MSG commands. The
        # companion can then serve the same message to both (once from the
        # memory queue, once from the persisted row) and the client sees it
        # twice. A single-consumer client never has two commands in flight,
        # which is what the optional lock below reproduces.
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

        async def serialized_get_msg(*args, **kwargs):
            async with self._lock:
                return await counted_get_msg(*args, **kwargs)

        client.commands.get_msg = (
            serialized_get_msg if self._serialize else counted_get_msg
        )

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

        # The real-client path: fetch on the companion's "messages waiting"
        # announcement, with no timed-out command left pending.
        await client.start_auto_message_fetching()

    def stop(self, client) -> None:
        for subscription in self._subscriptions:
            try:
                subscription.unsubscribe()
            except Exception:  # pragma: no cover - best effort
                pass
        self._subscriptions = []
        try:
            asyncio.get_event_loop().create_task(client.stop_auto_message_fetching())
        except Exception:  # pragma: no cover - best effort
            pass


async def run_bridge(args) -> dict:
    import meshcore

    evidence: dict = {
        "case": "openhop-bridge-phase3b",
        "label": "protocol-simulated",
        "pass": False,
        "sdk_version": _sdk_version(),
        "channel_index": args.channel,
        "started_at": utcnow(),
    }

    evidence["observer"] = {"host": args.host_peer, "port": args.port_peer}
    try:
        observer = MeshCObserver(args.host_peer, args.port_peer)
    except Exception as exc:
        evidence["reason"] = f"MeshC observer connection failed: {type(exc).__name__}"
        return evidence

    try:
        client = await asyncio.wait_for(
            meshcore.MeshCore.create_tcp(
                args.host_b, args.port_b, auto_reconnect=False
            ),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # pragma: no cover - defensive
        evidence["reason"] = f"connect to {args.host_b}:{args.port_b} failed: {exc!r}"
        observer.close()
        return evidence

    if client is None:
        evidence["reason"] = "companion B did not answer the APP_START handshake"
        observer.close()
        return evidence

    info = dict(getattr(client, "self_info", {}) or {})
    evidence["mc_peer"] = {
        "host": args.host_b,
        "port": args.port_b,
        "self_info": {
            "name": info.get("name"),
            "public_key": info.get("public_key"),
        },
    }

    drain = MCDrain()
    await drain.start(client)
    loop = asyncio.get_running_loop()

    try:
        # ---------------- direction 1: MC peer -> MEDRE -> MeshC peer -------
        nonce_mc_to_mesh = f"openhop-bridge-mc2mesh-{uuid.uuid4().hex}"
        d1: dict = {
            "direction": "mc-peer -> MEDRE -> meshc-peer",
            "nonce": nonce_mc_to_mesh,
            "started_at": utcnow(),
        }
        sent = await asyncio.wait_for(
            client.commands.send_chan_msg(args.channel, nonce_mc_to_mesh),
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        d1["send_event"] = str(getattr(sent, "type", ""))
        d1["send_finished_at"] = utcnow()
        if d1["send_event"] != str(meshcore.EventType.OK):
            d1["pass"] = False
            d1["reason"] = f"companion B refused the channel send: {d1['send_event']}"
            evidence["direction_mc_to_mesh"] = d1
            drain.stop(client)
            observer.close()
            return evidence

        hits = await loop.run_in_executor(
            None,
            observer.wait_for_text_settled,
            nonce_mc_to_mesh,
            args.window,
            args.settle,
        )
        d1["observer_arrivals"] = hits
        d1["arrival_count"] = len(hits)
        # The openHop companion's duplicate-delivery race (D5) is PINNED in
        # the candidate sources this profile runs on, so the client must see
        # the message once. The counters are therefore reported AND
        # asserted: the bridge proof fails on any duplicate.
        distinct = {entry.get("text") for entry in hits}
        d1["distinct_arrival_count"] = len(distinct)
        d1["duplicate_client_events"] = max(0, len(hits) - len(distinct))
        d1["finished_at"] = utcnow()
        # Exactly ONE arrival of the nonce, with no duplicate delivery.
        d1["pass"] = (
            len(distinct) == 1 and nonce_mc_to_mesh in distinct and len(hits) == 1
        )
        if not d1["pass"]:
            d1["reason"] = (
                "expected exactly one MeshC arrival of the nonce, got "
                f"{len(hits)} arrival(s) of {sorted(distinct)!r}"
            )
        # Nothing may echo back to companion B during direction 1.
        echo = [
            m for m in drain.messages if nonce_mc_to_mesh in str(m.get("text") or "")
        ]
        d1["echo_at_b"] = echo
        if echo:
            d1["pass"] = False
            d1["reason"] = (
                f"the nonce echoed back to companion B ({len(echo)} message(s))"
            )
        evidence["direction_mc_to_mesh"] = d1

        # ---------------- direction 2: MeshC peer -> MEDRE -> MC peer -------
        nonce_mesh_to_mc = f"openhop-bridge-mesh2mc-{uuid.uuid4().hex}"
        d2: dict = {
            "direction": "meshc-peer -> MEDRE -> mc-peer",
            "nonce": nonce_mesh_to_mc,
            "started_at": utcnow(),
        }
        d2["injection"] = observer.inject(nonce_mesh_to_mc, args.channel)
        expected_text = f"{args.expect_prefix}{nonce_mesh_to_mc}"
        d2["expected_text"] = expected_text
        deadline2 = time.monotonic() + args.window
        while time.monotonic() < deadline2:
            hits2 = [m for m in drain.messages if m.get("text") == expected_text]
            if hits2:
                break
            await asyncio.sleep(0.25)
        # Settle: a duplicate client delivery arrives within milliseconds of
        # the first one, so keep draining for the settle window before
        # counting. Without it the single-delivery assertion could not see a
        # duplicate that arrives late.
        settle_deadline = time.monotonic() + args.settle
        while time.monotonic() < settle_deadline:
            await asyncio.sleep(0.1)
        d2["mc_peer_arrivals"] = list(drain.messages)
        # The GET_MSG commands the client issued, and the frames the
        # companion sent back. These two numbers separate "the companion
        # delivered the message twice" from "the client asked twice".
        d2["get_msg_commands"] = drain.get_msg_calls
        d2["max_in_flight_get_msg"] = drain.max_in_flight_get_msg
        d2["frames_received"] = len(drain.messages)
        hits2 = [m for m in drain.messages if m.get("text") == expected_text]
        d2["arrival_count"] = len(hits2)
        distinct2 = {m.get("text") for m in hits2}
        d2["distinct_arrival_count"] = len(distinct2)
        d2["duplicate_client_events"] = max(0, len(hits2) - len(distinct2))
        d2["finished_at"] = utcnow()
        # Exactly ONE arrival of the bridged text, with no duplicate.
        d2["pass"] = (
            len(distinct2) == 1 and expected_text in distinct2 and len(hits2) == 1
        )
        if not d2["pass"]:
            d2["reason"] = (
                "expected exactly one MC peer arrival of the bridged text, got "
                f"{len(hits2)} arrival(s) of {sorted(distinct2)!r}"
            )
        evidence["direction_mesh_to_mc"] = d2
    finally:
        drain.stop(client)
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5.0)
        except Exception:  # pragma: no cover - best effort
            pass
        observer.close()

    d1 = evidence.get("direction_mc_to_mesh") or {}
    d2 = evidence.get("direction_mesh_to_mc") or {}
    evidence["pass"] = bool(d1.get("pass")) and bool(d2.get("pass"))
    evidence["reason"] = (
        "both bridge directions delivered the exact nonce once each, with no "
        "duplicate or echo"
        if evidence["pass"]
        else "one or both bridge directions failed"
    )
    evidence["finished_at"] = utcnow()
    return evidence


def _sdk_version() -> str:
    try:
        from importlib.metadata import version

        return version("meshcore")
    except Exception:  # pragma: no cover - metadata is present in the image
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-b", default="openhop-repeater-b")
    parser.add_argument("--port-b", type=int, default=5000)
    parser.add_argument("--host-peer", default="meshtasticd-c-peer")
    parser.add_argument("--port-peer", type=int, default=4403)
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--window", type=float, default=120.0)
    parser.add_argument(
        "--settle",
        type=float,
        default=5.0,
        help=(
            "Seconds to keep listening after the first arrival, so a "
            "duplicate client delivery that arrives late is still counted."
        ),
    )
    parser.add_argument(
        "--expect-prefix",
        default=os.environ.get("OPENHOP_BRIDGE_EXPECT_PREFIX", ""),
        help=(
            "Expected '<node name>: ' prefix on the direction-2 arrival "
            "(repeater A's companion node name, e.g. 'MEDRE-RADIO-a: ')."
        ),
    )
    args = parser.parse_args(argv)
    result = asyncio.run(run_bridge(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    sys.exit(main())
