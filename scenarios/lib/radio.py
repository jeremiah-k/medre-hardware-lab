"""Meshtastic radio injection, sniffing, and node inspection for scenarios.

Runs INSIDE the lab-tools container (mtjk/meshtastic Python package is
installed there; node APIs are reachable over the compose networks).

Subcommands:
    info HOST PORT
        Print node identity JSON: {my_node_num, node_id (!hex form)}.
    nodes HOST PORT
        Print the connected node's heard-node table JSON {nodes: {"!hex":
        {long_name, short_name}}} (PEER nodes only).
    send-text HOST PORT TEXT [--channel N] [--reply-id P] [--dest !hex]
        Send a TEXT_MESSAGE_APP packet (broadcast by default; a structured
        reply with --reply-id; a DIRECT MESSAGE with --dest) and print
        {"packet_id": <int>} from the transmitted MeshPacket.
        Omitting --channel uses the library default (primary channel).
    send-reaction HOST PORT KEY REPLY_ID [--channel N]
        Send a radio tapback: Data{TEXT_MESSAGE_APP, payload=KEY,
        reply_id=REPLY_ID, emoji=1} and print the transmitted packet id.
        This mirrors mmrelay's own send_text_reply packet construction, with
        the emoji flag set the way device tapbacks set it.
    listen HOST PORT --timeout S [--expect-text X] [--expect-reply-id P]
        Sniff the node's fromRadio stream (PEER nodes only: never attach a
        second client to a relay-connected node). Print the first matching
        text packet as JSON {packet_id, from_id, text, reply_id, channel};
        exit 1 on timeout with the packets seen so far.

The emoji reaction path builds the protobuf directly because the public
sendText() helper does not expose the Data.emoji flag.

Poisoned windows: the meshtastic client logs "Dropped N queued packet(s)
before reconnect config" when its API connection was evicted/reconnected
mid-window and the transport lost queued packets. listen() captures those
warnings (same TransportDropCapture contract as medre_radio.py) and
reports them as "transport_drops" in its result JSON, so an absence
window over a poisoned transport is never counted as clean evidence.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

BROADCAST_ADDR = "^all"


class TransportDropCapture(logging.Handler):
    """Capture the transport's dropped-queued-packet warnings (see module
    docstring); re-exported as medre_radio.TransportDropCapture's base."""

    DROP_SIGNATURE = "queued packet"

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        if "Dropped" in text and self.DROP_SIGNATURE in text:
            self.messages.append(text)


def connect(host: str, port: int):
    from meshtastic.tcp_interface import TCPInterface

    return TCPInterface(hostname=host, portNumber=port, timeout=30)


def node_info(interface) -> dict:
    my_num = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
    node_id = None
    if my_num is not None:
        node_id = f"!{my_num:08x}"
    return {"my_node_num": my_num, "node_id": node_id}


def send_text(interface, text: str, channel: int | None, reply_id: int | None, dest: str | None = None) -> dict:
    kwargs: dict = {"destinationId": dest if dest is not None else BROADCAST_ADDR}
    if channel is not None:
        kwargs["channelIndex"] = channel
    if reply_id is not None:
        kwargs["replyId"] = reply_id
    packet = interface.sendText(text, **kwargs)
    packet_id = getattr(packet, "id", None)
    return {"packet_id": packet_id, "kwargs": {k: str(v) for k, v in kwargs.items()}}


def node_list(interface) -> dict:
    """Heard-node table of the connected node (keyed by "!hex" node id).

    Used to resolve a medre-OWNED node's id WITHOUT attaching a second API
    client to it: on a two-node mesh (peer + medre-owned primary), the peer's
    node db holds the primary's entry once the primary has transmitted.
    """
    nodes = getattr(interface, "nodes", None) or {}
    out = {}
    for node_id, entry in nodes.items():
        user = (entry or {}).get("user") or {}
        out[str(node_id)] = {
            "long_name": user.get("longName") or (entry or {}).get("longName"),
            "short_name": user.get("shortName") or (entry or {}).get("shortName"),
        }
    return {"nodes": out}


def send_reaction(interface, key: str, reply_id: int, channel: int | None) -> dict:
    """Send a tapback packet with Data.emoji set, mirroring mmrelay's builder."""
    from meshtastic import mesh_pb2, portnums_pb2

    data_msg = mesh_pb2.Data()
    data_msg.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    data_msg.payload = key.encode("utf-8")
    data_msg.reply_id = int(reply_id)
    data_msg.emoji = 1

    mesh_packet = mesh_pb2.MeshPacket()
    mesh_packet.channel = int(channel) if channel is not None else 0
    mesh_packet.decoded.CopyFrom(data_msg)
    mesh_packet.id = interface._generatePacketId()

    sent = interface._sendPacket(mesh_packet, destinationId=BROADCAST_ADDR)
    return {"packet_id": getattr(sent, "id", mesh_packet.id)}


def _decoded(packet: dict) -> dict:
    decoded = packet.get("decoded")
    return decoded if isinstance(decoded, dict) else {}


def _packet_view(packet: dict) -> dict | None:
    """Return a comparable view of one fromRadio packet, or None if not text."""
    decoded = _decoded(packet)
    text = decoded.get("text")
    if not isinstance(text, str):
        return None
    return {
        "packet_id": packet.get("id"),
        "from_id": packet.get("fromId"),
        "text": text,
        "reply_id": decoded.get("replyId"),
        "emoji": decoded.get("emoji"),
        "channel": packet.get("channel"),
    }


def listen(host: str, port: int, timeout: float, expect_text: str | None, expect_reply_id: int | None, tag: str | None) -> int:
    """Sniff fromRadio until a matching text packet arrives or timeout."""
    from pubsub import pub

    drops = TransportDropCapture()
    logging.getLogger().addHandler(drops)

    seen: list[dict] = []

    def _on_packet(packet, interface) -> None:  # noqa: ANN001 - pubsub signature
        if isinstance(packet, dict):
            view = _packet_view(packet)
            if view is not None:
                seen.append(view)

    interface = connect(host, port)
    pub.subscribe(_on_packet, "meshtastic.receive")
    # Readiness marker: scenarios gate their act on this line so a packet
    # sent right after arming cannot slip through the subscribe window.
    print(json.dumps({"listening": True, "host": host, "port": port, "tag": tag}), flush=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            for view in seen:
                if expect_text is not None and expect_text not in view["text"]:
                    continue
                if expect_reply_id is not None and view["reply_id"] != expect_reply_id:
                    continue
                print(
                    json.dumps(
                        {
                            "match": view,
                            "packets_seen": len(seen),
                            "transport_drops": drops.messages,
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            if time.monotonic() >= deadline:
                print(
                    json.dumps(
                        {
                            "error": "no matching packet on the sniffed stream",
                            "expect_text": expect_text,
                            "expect_reply_id": expect_reply_id,
                            "packets_seen": seen[-10:],
                            "transport_drops": drops.messages,
                        },
                        ensure_ascii=False,
                    )
                )
                return 1
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info"); p.add_argument("host"); p.add_argument("port", type=int)
    p = sub.add_parser("nodes"); p.add_argument("host"); p.add_argument("port", type=int)
    p = sub.add_parser("send-text"); p.add_argument("host"); p.add_argument("port", type=int)
    p.add_argument("text"); p.add_argument("--channel", type=int, default=None)
    p.add_argument("--reply-id", type=int, default=None)
    p.add_argument("--dest", default=None, help="destination node id (!hex) for a direct message")
    p = sub.add_parser("send-reaction"); p.add_argument("host"); p.add_argument("port", type=int)
    p.add_argument("key"); p.add_argument("reply_id", type=int)
    p.add_argument("--channel", type=int, default=None)
    p = sub.add_parser("listen"); p.add_argument("host"); p.add_argument("port", type=int)
    p.add_argument("--timeout", type=float, default=45.0)
    p.add_argument("--expect-text", default=None)
    p.add_argument("--expect-reply-id", type=int, default=None)
    p.add_argument("--tag", default=None)

    args = parser.parse_args()

    if args.command == "listen":
        try:
            return listen(args.host, args.port, args.timeout, args.expect_text, args.expect_reply_id, args.tag)
        except Exception as exc:  # noqa: BLE001 - report as JSON
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
            return 1

    try:
        interface = connect(args.host, args.port)
    except Exception as exc:  # noqa: BLE001 - report connection failures as JSON
        print(json.dumps({"error": f"cannot connect to {args.host}:{args.port}: {exc}"}))
        return 1

    try:
        if args.command == "info":
            print(json.dumps(node_info(interface)))
            return 0
        if args.command == "nodes":
            print(json.dumps(node_list(interface)))
            return 0
        if args.command == "send-text":
            print(json.dumps(send_text(interface, args.text, args.channel, args.reply_id, args.dest)))
            return 0
        if args.command == "send-reaction":
            print(json.dumps(send_reaction(interface, args.key, args.reply_id, args.channel)))
            return 0
        return 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    finally:
        try:
            interface.close()
        except Exception:  # noqa: BLE001 - best-effort close
            pass


if __name__ == "__main__":
    sys.exit(main())
