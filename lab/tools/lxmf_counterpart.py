"""LXMF counterpart driver for the normal-profile medre relay.

Runs INSIDE the medre-normal container (same network namespace, so the
loopback Reticulum UDPInterface pair connects; the container bakes the pinned
RNS/LXMF stack):

    docker compose --profile normal exec -T medre-normal \
        python /opt/lab-tools/lxmf_counterpart.py \
            --identity /data-lxmf/peer.identity \
            --reticulum-config /data-lxmf/reticulum-peer \
            --storage /data-lxmf/peer-router \
            --expect-text <marker> \
            --send-text <marker> \
            --timeout 240

Behavior (the counterpart is the LXMF peer of medre's lxmf0 adapter):
  1. Own RNS instance over the loopback-only config (127.0.0.1 UDP pair,
     share_instance = No — never reaches a physical interface).
  2. Registers its delivery identity on a real LXMRouter and announces once,
     so medre learns the path to the peer's (stable, bootstrapped) delivery
     destination hash — the one lab/up.sh substituted into the medre config.
  3. Listens for medre's periodic announces (aspect filter "lxmf.delivery")
     and records the medre destination hash + display name.
  4. Waits for an inbound message containing --expect-text (the room fan-out
     leg: matrix -> medre lxmf0 -> counterpart) and records it.
  5. Sends --send-text to medre's delivery destination (opportunistic
     delivery), waiting for the delivery receipt (the counterpart -> medre ->
     #lab-bridge leg).
  6. Prints per-message JSON lines and a final RESULT line, exits 0 only if
     BOTH legs completed inside the timeout.

stdout protocol (parsed/teed by lab/up.sh):
  LXMF_COUNTERPART_READY={json}    after init + announce (readiness gate)
  LXMF_COUNTERPART_MESSAGE={json}  per inbound message
  LXMF_COUNTERPART_RESULT={json}   final verdict
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import signal
import sys
import threading
import time

RESULT_PREFIX = "LXMF_COUNTERPART_RESULT="
READY_PREFIX = "LXMF_COUNTERPART_READY="
MESSAGE_PREFIX = "LXMF_COUNTERPART_MESSAGE="
APP_NAME = "lxmf"
ASPECT = "delivery"


def utcnow() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class AnnounceCollector:
    """Collect lxmf.delivery announces (medre's periodic path discovery)."""

    # RNS.Transport announce-handler contract: the filter is the FULL
    # destination name ("app_name.aspect") hashed against the announced
    # identity — LXMF's own LXMFDeliveryAnnounceHandler uses the same value.
    aspect_filter = f"{APP_NAME}.{ASPECT}"

    def __init__(self) -> None:
        self.announces: dict[str, dict[str, str]] = {}
        self.first_seen_at: str | None = None

    def received_announce(self, destination_hash, announced_identity, app_data) -> None:
        del announced_identity
        hash_hex = (
            destination_hash.hex()
            if isinstance(destination_hash, (bytes, bytearray))
            else str(destination_hash)
        )
        name = ""
        if isinstance(app_data, (bytes, bytearray)):
            # LXMF app_data: [display name][\x00?stamp cost] — keep printable text.
            try:
                name = bytes(app_data).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - best-effort decode
                name = ""
        name = name.split("\x00")[0].strip()
        if hash_hex not in self.announces:
            self.announces[hash_hex] = {"display_name": name}
            self.first_seen_at = self.first_seen_at or utcnow()
        else:
            self.announces[hash_hex]["display_name"] = name or (
                self.announces[hash_hex]["display_name"]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", required=True, help="peer RNS identity file")
    parser.add_argument(
        "--reticulum-config", required=True, help="RNS config dir (loopback pair)"
    )
    parser.add_argument("--storage", required=True, help="LXMRouter storage path")
    parser.add_argument("--display-name", default="lab-counterpart")
    parser.add_argument("--expect-text", default="", help="wait for inbound body")
    parser.add_argument("--send-text", default="", help="send this body to medre")
    parser.add_argument(
        "--timeout", type=float, default=240.0, help="overall deadline seconds"
    )
    args = parser.parse_args()

    import LXMF  # noqa: PLC0415 - pinned SDK, container-local
    import RNS  # noqa: PLC0415

    result: dict[str, object] = {
        "started_at": utcnow(),
        "reticulum_config": args.reticulum_config,
        "storage": args.storage,
        "display_name": args.display_name,
        "rns_version": RNS.__version__,
        "lxmf_version": LXMF.__version__,
        "received": [],
        "sent_delivered": False,
        "medre_announce_seen": False,
        "medre_destination_hash": None,
        "errors": [],
    }
    deadline = time.monotonic() + args.timeout

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    if not os.path.isfile(args.identity):
        result["errors"].append(f"identity file missing: {args.identity}")
        print(RESULT_PREFIX + json.dumps(result))
        return 1
    os.makedirs(args.reticulum_config, exist_ok=True)
    os.makedirs(args.storage, exist_ok=True)

    reticulum = RNS.Reticulum(configdir=args.reticulum_config)
    del reticulum  # instance is owned by the SDK singleton

    identity = RNS.Identity.from_file(args.identity)
    if identity is None:
        result["errors"].append(f"failed to load identity: {args.identity}")
        print(RESULT_PREFIX + json.dumps(result))
        return 1

    router = LXMF.LXMRouter(identity=identity, storagepath=args.storage)
    delivery_destination = router.register_delivery_identity(
        identity, display_name=args.display_name
    )
    own_hash = delivery_destination.hash
    result["own_destination_hash"] = own_hash.hex()

    received_lock = threading.Lock()
    received: list[dict[str, object]] = []

    def on_delivery(lxmsg) -> None:  # noqa: ANN001 - SDK type
        source_hash = None
        try:
            source_hash = lxmsg.source_hash.hex()
        except Exception:  # noqa: BLE001 - diagnostics only
            pass
        try:
            content = (
                lxmsg.content.decode("utf-8", errors="replace")
                if isinstance(lxmsg.content, (bytes, bytearray))
                else str(lxmsg.content)
            )
        except Exception:  # noqa: BLE001 - diagnostics only
            content = ""
        record: dict[str, object] = {
            "received_at": utcnow(),
            "source_hash": source_hash,
            "content": content,
            "title": str(getattr(lxmsg, "title", "") or ""),
        }
        with received_lock:
            received.append(record)
        print(MESSAGE_PREFIX + json.dumps(record), flush=True)

    router.register_delivery_callback(on_delivery)

    # Announce ourselves once so medre learns the path to the peer hash.
    try:
        router.announce(own_hash)
        result["self_announced"] = True
    except Exception as exc:  # noqa: BLE001 - report, keep listening
        result["errors"].append(f"self announce failed: {exc}")

    collector = AnnounceCollector()
    RNS.Transport.register_announce_handler(collector)

    print(
        READY_PREFIX
        + json.dumps({
            "own_destination_hash": own_hash.hex(),
            "at": utcnow(),
        }),
        flush=True,
    )

    # ---- Leg 1: wait for medre's announce (path discovery) ----------------
    peer_hash: str | None = None
    while not collector.announces and remaining() > 0:
        time.sleep(0.5)
    if collector.announces:
        result["medre_announce_seen"] = True
        result["medre_announces"] = collector.announces
        # The medre peer is whichever announced destination is not our own.
        peer_hash = next(
            (h for h in collector.announces if h != own_hash.hex()), None
        )
        result["medre_destination_hash"] = peer_hash
    else:
        result["errors"].append("no lxmf.delivery announce seen before deadline")

    # ---- Leg 2: inbound from medre (matrix -> lxmf0 -> counterpart) -------
    expect_deadline = remaining()

    def has_expected() -> bool:
        if not args.expect_text:
            return True
        with received_lock:
            return any(args.expect_text in str(m.get("content", "")) for m in received)

    while not has_expected() and remaining() > 0:
        time.sleep(0.5)
    with received_lock:
        result["received"] = list(received)
    if args.expect_text and not has_expected():
        result["errors"].append(
            f"expected inbound text not seen ({expect_deadline:.0f}s window)"
        )

    # ---- Leg 3: outbound to medre (counterpart -> medre -> #lab-bridge) ---
    if args.send_text and peer_hash is not None:
        try:
            peer_identity = RNS.Identity.recall(bytes.fromhex(peer_hash))
            if peer_identity is None:
                result["errors"].append("medre identity not recalled from announce")
            else:
                dest = RNS.Destination(
                    peer_identity, RNS.Destination.OUT,
                    RNS.Destination.SINGLE, APP_NAME, ASPECT,
                )
                lxmsg = LXMF.LXMessage(
                    dest, delivery_destination, args.send_text,
                    title="interop-lab counterpart",
                    desired_method=LXMF.LXMessage.OPPORTUNISTIC,
                )
                delivered = threading.Event()

                def on_state(lxmsg) -> None:  # noqa: ANN001 - SDK type
                    if lxmsg.state == LXMF.LXMessage.DELIVERED:
                        delivered.set()
                    elif lxmsg.state == LXMF.LXMessage.FAILED:
                        result["errors"].append("outbound delivery FAILED")

                lxmsg.register_delivery_callback(on_state)
                # LXMF 1.2.1 API (handle_message was removed); the medre
                # adapter session uses the same entry point.
                router.handle_outbound(lxmsg)
                while not delivered.is_set() and remaining() > 0:
                    time.sleep(0.5)
                result["sent_delivered"] = delivered.is_set()
                if not delivered.is_set():
                    result["errors"].append("outbound not DELIVERED before deadline")
        except Exception as exc:  # noqa: BLE001 - diagnostics
            result["errors"].append(f"outbound send failed: {exc}")
    elif args.send_text and peer_hash is None:
        result["errors"].append("cannot send: no medre destination known")

    result["finished_at"] = utcnow()
    ok = (
        result["medre_announce_seen"]
        and (not args.expect_text or has_expected())
        and (not args.send_text or bool(result["sent_delivered"]))
    )
    print(RESULT_PREFIX + json.dumps(result), flush=True)
    return 0 if ok else 1


def _sigterm(signum, frame) -> None:  # noqa: ANN001
    del signum, frame
    sys.exit(1)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm)
    sys.exit(main())
