#!/usr/bin/env python3
"""Bounded packet-path proof for the openHop virtual-radio rig.

This probe proves that a REAL MeshCore group-text packet produced by one
openHop repeater reaches the OTHER repeater over the lab's virtual radio
medium, and that it does NOT echo back.

It uses the PINNED upstream SDK (``meshcore==2.3.15``, the same
distribution MEDRE's medre-adapter-meshcore depends on) and the MeshCore
companion frame protocol, exactly like the Phase 2 protocol probe.

WHAT IT DOES
------------
1. Connects a SUBSCRIBER client to companion A (repeater A's companion
   server) and a SENDER client to companion B (repeater B's companion
   server). Each companion server holds ONE client at a time; A and B are
   different servers, so one client each is legal.
2. The sender transmits a unique channel-0 nonce through companion B. The
   repeater encrypts it with its channel-0 secret and puts it on the air.
   The lab's virtual radio relay carries the packet to the other repeater,
   whose companion delivers it to the subscriber.
3. Asserts, inside a bounded window:
     * the subscriber at A received EXACTLY ONE channel message,
     * that message's text is byte-exact the nonce,
     * the sender at B received NO channel message (no echo).

SINGLE-CLIENT RULE
------------------
openHop's companion server accepts ONE client at a time and evicts the
previous one, so this probe holds exactly one client per companion.

WHAT IT PROVES / DOES NOT PROVE
-------------------------------
It proves that real MeshCore packets cross a real openHop radio driver
and are really encrypted, transmitted, received and decrypted by two
independent repeaters. The RF MEDIUM is simulated: the relay is a byte
pipe, so NOTHING here is evidence of physical RF range, spectrum,
antenna behaviour or regulatory compliance.

Output: one JSON object on stdout. Exit code 0 on success, 1 on failure
(the reason is in the JSON).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import uuid

CONNECT_TIMEOUT_SECONDS = 20.0
COMMAND_TIMEOUT_SECONDS = 15.0
DRAIN_POLL_SECONDS = 2.0


async def _connect(host: str, port: int) -> tuple[object | None, dict]:
    """Open one MeshCore TCP client and return it with a small evidence dict."""
    import meshcore

    evidence: dict = {"host": host, "port": port}
    try:
        client = await asyncio.wait_for(
            meshcore.MeshCore.create_tcp(host, port, auto_reconnect=False),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        evidence["error"] = (
            f"connect to {host}:{port} timed out after {CONNECT_TIMEOUT_SECONDS}s"
        )
        return None, evidence
    except Exception as exc:  # pragma: no cover - defensive
        evidence["error"] = f"connect to {host}:{port} failed: {exc!r}"
        return None, evidence

    if client is None:
        evidence["error"] = (
            f"{host}:{port} did not answer the MeshCore handshake (APP_START unanswered)"
        )
        return None, evidence

    info = dict(getattr(client, "self_info", {}) or {})
    evidence["self_info"] = {
        "name": info.get("name"),
        "public_key": info.get("public_key"),
    }
    if not info.get("public_key"):
        evidence["error"] = "SELF_INFO carries no public_key"
        return None, evidence
    return client, evidence


class _Drain:
    """Bounded drain of one client's message queue."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.messages: list[dict] = []
        self.last_queue_reply = 0.0
        self.failed = False

    async def run(self, client, deadline: float) -> None:
        import meshcore

        while time.monotonic() < deadline:
            remaining = max(0.5, deadline - time.monotonic())
            try:
                event = await client.commands.get_msg(
                    timeout=min(DRAIN_POLL_SECONDS, remaining)
                )
            except Exception:
                self.failed = True
                return
            event_type = str(getattr(event, "type", ""))
            if event_type == str(meshcore.EventType.NO_MORE_MSGS):
                self.last_queue_reply = time.monotonic()
                await asyncio.sleep(0.1)
                continue
            if event_type == str(meshcore.EventType.ERROR):
                self.failed = True
                return
            if event_type != str(meshcore.EventType.CHANNEL_MSG_RECV):
                continue
            self.last_queue_reply = time.monotonic()
            payload = getattr(event, "payload", None) or {}
            self.messages.append(
                {
                    "channel_idx": payload.get("channel_idx"),
                    "text": payload.get("text"),
                }
            )


async def proof(args) -> dict:
    import meshcore

    try:
        from importlib.metadata import version as _dist_version

        sdk_version = _dist_version("meshcore")
    except Exception:  # pragma: no cover - metadata is present in the image
        sdk_version = getattr(meshcore, "__version__", "unknown")

    nonce = f"openhop-radio-proof-{uuid.uuid4().hex}"
    evidence: dict = {
        "case": "openhop-virtual-radio-packet-path",
        "pass": False,
        "sdk_version": sdk_version,
        "nonce": nonce,
        "channel_idx": args.channel,
        "window_seconds": args.window,
    }

    subscriber, sub_evidence = await _connect(args.host_a, args.port_a)
    evidence["subscriber"] = sub_evidence
    if subscriber is None:
        evidence["reason"] = sub_evidence.get("error", "subscriber connect failed")
        return evidence

    sender, send_evidence = await _connect(args.host_b, args.port_b)
    evidence["sender"] = send_evidence
    if sender is None:
        await subscriber.disconnect()
        evidence["reason"] = send_evidence.get("error", "sender connect failed")
        return evidence

    deadline = time.monotonic() + args.window
    sub_drain = _Drain("subscriber")
    send_drain = _Drain("sender")
    drains = asyncio.gather(
        sub_drain.run(subscriber, deadline), send_drain.run(sender, deadline)
    )

    try:
        sender_name = (send_evidence.get("self_info") or {}).get("name") or ""
        # openHop prefixes an outgoing GROUP TEXT with the sending companion's
        # node name ("<node name>: <text>"), and the receiving companion
        # delivers that exact content. The expected text is therefore derived
        # from the sender's own SELF_INFO, not guessed.
        expected_text = f"{sender_name}: {nonce}" if sender_name else nonce
        evidence["expected_text"] = expected_text

        sent = await asyncio.wait_for(
            sender.commands.send_chan_msg(args.channel, nonce),
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        sent_at = time.monotonic()
        evidence["send_event"] = str(getattr(sent, "type", ""))
        if evidence["send_event"] != str(meshcore.EventType.OK):
            evidence["reason"] = (
                f"companion B refused the channel send: {evidence['send_event']}"
            )
            return evidence
    except asyncio.TimeoutError:
        evidence["reason"] = "the channel send timed out"
        return evidence
    except Exception as exc:
        evidence["reason"] = f"the channel send failed: {exc!r}"
        return evidence

    finally:
        try:
            await drains
        finally:
            for client in (subscriber, sender):
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=5.0)
                except Exception:  # pragma: no cover - best effort
                    pass

    delivered = [
        msg
        for msg in sub_drain.messages
        if msg.get("text") == expected_text and msg.get("channel_idx") == args.channel
    ]
    evidence["deliveries_at_a"] = sub_drain.messages
    evidence["deliveries_at_b"] = send_drain.messages
    evidence["delivered_count_at_a"] = len(delivered)
    evidence["echo_count_at_b"] = len(send_drain.messages)
    evidence["duplicate_client_events_at_a"] = max(0, len(delivered) - 1)
    evidence["sender_queue_observed_after_send"] = (
        send_drain.last_queue_reply >= sent_at
    )

    if sub_drain.failed or send_drain.failed:
        evidence["reason"] = "a peer queue observation failed before the window ended"
        return evidence
    if len(delivered) != 1 or len(sub_drain.messages) != 1:
        evidence["reason"] = "A did not observe exactly one matching channel message"
        return evidence
    if not evidence["sender_queue_observed_after_send"]:
        evidence["reason"] = "B did not return a queue observation after the send"
        return evidence
    if send_drain.messages:
        evidence["reason"] = (
            f"the message echoed back to B ({len(send_drain.messages)} message(s))"
        )
        return evidence

    evidence["pass"] = True
    evidence["reason"] = (
        "the channel-0 packet crossed the virtual radio from B to A with the exact "
        "text and nothing echoed back to B"
    )
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-a", default="openhop-repeater-a")
    parser.add_argument("--port-a", type=int, default=5000)
    parser.add_argument("--host-b", default="openhop-repeater-b")
    parser.add_argument("--port-b", type=int, default=5000)
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--window", type=float, default=60.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.window) or args.window <= 0:
        parser.error("--window must be finite and positive")
    result = asyncio.run(proof(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    sys.exit(main())
