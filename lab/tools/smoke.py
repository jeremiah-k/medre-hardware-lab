"""Interop smoke: peer radio on Alpha mesh -> Matrix room -> Beta mesh.

Path exercised (owner topology: per-relay node PAIRS, injection via peers):
  meshtasticd-a-peer (TCP API send) --over-air--> meshtasticd-a
  -> mmrelay-a -> #lab-alpha-ch0 (Matrix) -> mmrelay-b
  --over-air--> meshtasticd-b, witnessed by a live listener on
  meshtasticd-b-peer.

Assertions (state/API based, no log greps):
  1. Matrix event from @relay-a:lab.test in #lab-alpha-ch0 whose body
     contains the unique smoke text (observed via @untrusted sync).
  2. Row in relay B's message_map SQLite table mapping that Matrix event to a
     non-null meshtastic_id (proof relay B dispatched into the Beta mesh).
  3. The Beta-side peer node (meshtasticd-b-peer) actually RECEIVES the text
     over the simulated air (live listener; the witness never touches the
     relay-connected node's API, so no relay connection churn).

The primary node of a meshnet is NEVER used for injection or listening:
meshtasticd force-closes the previous TCP API client on every new connection.

Usage (inside lab-tools): python /opt/lab-tools/smoke.py
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from typing import Any

from lab_lib import (
    LAB_LOGS_DIR,
    connect_sqlite_ro,
    load_runtime,
    wait_for_matrix_event,
)

SMOKE_TIMEOUT_MATRIX = float(os.environ.get("SMOKE_TIMEOUT_MATRIX", "90"))
SMOKE_TIMEOUT_DB = float(os.environ.get("SMOKE_TIMEOUT_DB", "60"))
SMOKE_TIMEOUT_RADIO = float(os.environ.get("SMOKE_TIMEOUT_RADIO", "45"))


class PeerNode:
    """A meshtasticd node used for radio injection / witnessing.

    Connects with a settle delay so the node's want_config exchange is fully
    complete before any send (an immediate send after connect is dropped by
    the node). Use ONLY on peer nodes, never on the relay-connected primary.

    Received text packets are collected via the mtjk pubsub topic, filtered
    to THIS interface (multiple interfaces in one process share the topic).
    """

    def __init__(self, host: str) -> None:
        from meshtastic.tcp_interface import TCPInterface
        from pubsub import pub

        self.host = host
        self._texts: list[str] = []
        self._packets: list[dict] = []
        self.iface = TCPInterface(hostname=host, portNumber=4403)

        node_ref = self

        def on_packet(packet, iface=None, interface=None, **kwargs):
            # mtjk (2.7.11.post14) publishes the receiving interface as the
            # `interface` kwarg; older releases used `iface`. With multiple
            # TCPInterfaces in one process the filter MUST engage or every
            # listener records every interface's packets.
            active = interface if interface is not None else iface
            if active is not None and active is not node_ref.iface:
                return
            decoded = packet.get("decoded", {}) if isinstance(packet, dict) else {}
            if decoded.get("portnum") == "TEXT_MESSAGE_APP":
                node_ref._texts.append(decoded.get("text", ""))
                node_ref._packets.append(
                    {
                        "packet_id": packet.get("id"),
                        "from_id": packet.get("fromId"),
                        "text": decoded.get("text", ""),
                    }
                )

        self._callback = on_packet
        pub.subscribe(on_packet, "meshtastic.receive")
        time.sleep(4)

    def send_text(self, text: str) -> None:
        self.iface.sendText(text)

    def received_texts(self) -> list[str]:
        return list(self._texts)

    def received_packets(self) -> list[dict]:
        """Received text packets with wire ids (dedup/loop analysis)."""
        return list(self._packets)

    def close(self) -> None:
        try:
            from pubsub import pub

            pub.unsubscribe(self._callback, "meshtastic.receive")
        except Exception:  # noqa: BLE001 - best-effort unsubscribe
            pass
        try:
            self.iface.close()
        except Exception:  # noqa: BLE001 - best-effort close
            pass


def wait_message_map_row(
    db_path: str, matrix_event_id: str, timeout: float
) -> dict | None:
    """Poll relay SQLite for a message_map row with a non-null meshtastic_id."""
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            conn = connect_sqlite_ro(db_path)
            try:
                row = conn.execute(
                    "SELECT meshtastic_id, matrix_room_id, meshtastic_text, meshtastic_meshnet "
                    "FROM message_map WHERE matrix_event_id=?",
                    (matrix_event_id,),
                ).fetchone()
            finally:
                conn.close()
            if row and row[0] not in (None, ""):
                return {
                    "meshtastic_id": row[0],
                    "matrix_room_id": row[1],
                    "meshtastic_text": row[2],
                    "meshtastic_meshnet": row[3],
                }
        except Exception as exc:  # noqa: BLE001 - retry until deadline
            last_error = str(exc)
        time.sleep(1)
    if last_error:
        print(f"  last SQLite error: {last_error}", file=sys.stderr)
    return None


def main() -> int:
    runtime = load_runtime()
    untrusted_token = runtime["users"]["untrusted"]["access_token"]
    relay_a_user = runtime["users"]["relay-a"]["user_id"]
    alpha_ch0_room = runtime["rooms"]["lab-alpha-ch0"]["room_id"]

    smoke_text = f"INTEROP_SMOKE_{int(time.time())}_{os.urandom(3).hex()}"
    result: dict[str, Any] = {
        "smoke_text": smoke_text,
        "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "path": (
            "meshtasticd-a-peer --air--> meshtasticd-a -> mmrelay-a -> "
            "#lab-alpha-ch0 -> mmrelay-b --air--> meshtasticd-b "
            "(witnessed by meshtasticd-b-peer listener)"
        ),
        "steps": {},
    }
    print(f"Smoke text: {smoke_text}")

    witness = PeerNode("meshtasticd-b-peer")
    injector = PeerNode("meshtasticd-a-peer")
    try:
        print("[1/4] Injecting text into the Alpha mesh via meshtasticd-a-peer...")
        injector.send_text(smoke_text)
        mesh_send_at = time.time()
        result["steps"]["radio_inject"] = {
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "host": "meshtasticd-a-peer",
        }

        print("[2/4] Waiting for Matrix event from relay A in #lab-alpha-ch0...")
        matched = wait_for_matrix_event(
            token=untrusted_token,
            room_id=alpha_ch0_room,
            timeout_seconds=SMOKE_TIMEOUT_MATRIX,
            sender=relay_a_user,
            event_type="m.room.message",
            body_contains=smoke_text,
        )
        if matched is None:
            print("SMOKE FAIL: no Matrix event from relay A containing smoke text")
            result["steps"]["matrix_event"] = {"status": "TIMEOUT"}
            write_result(result)
            return 1
        event, _since = matched
        matrix_at = event.get("origin_server_ts")
        print(
            f"  event_id={event.get('event_id')} sender={event.get('sender')} "
            f"origin_server_ts={matrix_at} body={event.get('content', {}).get('body', '')!r}"
        )
        result["steps"]["matrix_event"] = {
            "status": "OK",
            "event_id": event.get("event_id"),
            "sender": event.get("sender"),
            "origin_server_ts": matrix_at,
            "mesh_send_epoch_s": mesh_send_at,
            "latency_ms_from_mesh_send": (
                matrix_at - int(mesh_send_at * 1000)
                if isinstance(matrix_at, int)
                else None
            ),
        }

        print(
            "[3/4] Waiting for relay B message_map row (room -> Beta mesh dispatch)..."
        )
        row = wait_message_map_row(
            "/state/mmrelay-b/database/meshtastic.sqlite",
            event["event_id"],
            SMOKE_TIMEOUT_DB,
        )
        if row is None:
            print("SMOKE FAIL: relay B did not map the event into the Beta mesh")
            result["steps"]["relay_b_dispatch"] = {"status": "TIMEOUT"}
            write_result(result)
            return 1
        print(
            f"  meshtastic_id={row['meshtastic_id']} meshnet={row['meshtastic_meshnet']}"
        )
        result["steps"]["relay_b_dispatch"] = {"status": "OK", **row}

        print("[4/4] Waiting for Beta-side over-air witness on meshtasticd-b-peer...")
        deadline = time.monotonic() + SMOKE_TIMEOUT_RADIO
        seen: list[str] = []
        while time.monotonic() < deadline:
            seen = [t for t in witness.received_texts() if smoke_text in t]
            if seen:
                break
            time.sleep(1)
        if not seen:
            print(
                "SMOKE FAIL: Beta-side peer node never received the text over the air"
            )
            result["steps"]["beta_witness"] = {"status": "TIMEOUT"}
            write_result(result)
            return 1
        print(f"  meshtasticd-b-peer received: {seen}")
        result["steps"]["beta_witness"] = {"status": "OK", "texts": seen}
    finally:
        injector.close()
        witness.close()

    result["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    write_result(result)
    print(
        "SMOKE PASS: Alpha radio text reached Matrix via relay A "
        f"({result['steps']['matrix_event']['event_id']}), was dispatched into "
        f"the Beta mesh by relay B (meshtastic_id={row['meshtastic_id']}), and "
        "arrived over the Beta air (peer witness)"
    )
    return 0


def write_result(result: dict[str, Any]) -> None:
    os.makedirs(LAB_LOGS_DIR, exist_ok=True)
    path = os.path.join(LAB_LOGS_DIR, "smoke-result.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(f"  result written to {path}")


if __name__ == "__main__":
    sys.exit(main())
