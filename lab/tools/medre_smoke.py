"""MEDRE bridge smoke: dual-envelope ingress + dual radio egress, no loops.

Exercises the `medre` compose profile bridge (one medre runtime, bot
@medre:lab.test, dedicated nodes meshtasticd-a-medre / meshtasticd-b-medre)
alongside the existing mmrelay relays on the same rooms and sim meshes.

Part A (radio -> Matrix, smoke (a) of the medre bring-up contract):
  inject a unique text via meshtasticd-a-peer -> over the Alpha air ->
  BOTH @relay-a:lab.test and @medre:lab.test must emit a canonical event
  into #lab-alpha-ch0, and BOTH envelopes must be present and schema-valid
  (content["medre"]["envelope"], schema_version 1, numeric source_channel
  per the r14 channel-identity invariant).

Part B (Matrix -> radio, smoke (b)): a human (@alice) text in
  #lab-alpha-ch0 must be dispatched to the Alpha radio by BOTH bridges
  (relay-a proven by its message_map row; the combined radio copies are
  witnessed live on meshtasticd-a-peer), with NO duplication loop: within
  the settle window the Alpha air carries EXACTLY TWO copies of the marker
  (one per bridge). A third copy would mean one bridge re-bridged the other
  bridge's Matrix envelope or radio copy back to the radio. Beta-side peer
  observations are recorded informationally (mmrelay-b maps #lab-alpha-ch0
  onto its own radio; the medre runtime maps Alpha rooms to radio-a only).

The relay-connected primaries (meshtasticd-a, meshtasticd-a-medre) are never
touched: injection and witnessing go through the peer node.

Usage (inside lab-tools): python /opt/lab-tools/medre_smoke.py
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
    load_runtime,
    matrix_request,
    wait_for_matrix_event,
)
from smoke import PeerNode, wait_message_map_row

MEDRE_SMOKE_TIMEOUT_MATRIX = float(os.environ.get("MEDRE_SMOKE_TIMEOUT_MATRIX", "90"))
MEDRE_SMOKE_TIMEOUT_DB = float(os.environ.get("MEDRE_SMOKE_TIMEOUT_DB", "60"))
MEDRE_SMOKE_TIMEOUT_RADIO = float(os.environ.get("MEDRE_SMOKE_TIMEOUT_RADIO", "45"))
MEDRE_SMOKE_SETTLE_SECONDS = float(os.environ.get("MEDRE_SMOKE_SETTLE_SECONDS", "20"))


def envelope_of(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the canonical metadata envelope of a Matrix event, if any."""
    content = event.get("content", {})
    if not isinstance(content, dict):
        return None
    medre = content.get("medre")
    if not isinstance(medre, dict):
        return None
    envelope = medre.get("envelope")
    return envelope if isinstance(envelope, dict) else None


def envelope_problems(envelope: dict[str, Any] | None, label: str) -> list[str]:
    """Check one canonical envelope against the shared structural contract.

    schema_version must be the int 1 ( MATRIX_METADATA_ENVELOPE_SCHEMA_VERSION),
    canonical_event_id a non-empty $-prefixed event ID, source_adapter a
    non-empty string, and source_channel a numeric string (r14
    channel-identity invariant: numeric channel ids only; a meshnet label
    here is the exact regression triaged 2026-11-10).
    """
    if envelope is None:
        return [f"{label}: canonical envelope missing (content['medre']['envelope'])"]
    problems: list[str] = []
    schema_version = envelope.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        problems.append(f"{label}: schema_version must be int 1, got {schema_version!r}")
    canonical_event_id = envelope.get("canonical_event_id")
    if not (
        isinstance(canonical_event_id, str)
        and canonical_event_id.strip()
        and len(canonical_event_id) <= 64
    ):
        problems.append(
            f"{label}: canonical_event_id must be a non-empty opaque id, "
            f"got {canonical_event_id!r}"
        )
    source_adapter = envelope.get("source_adapter")
    if not (isinstance(source_adapter, str) and source_adapter.strip()):
        problems.append(f"{label}: source_adapter must be a non-empty string")
    source_channel = envelope.get("source_channel")
    if not (isinstance(source_channel, str) and source_channel.isdigit()):
        problems.append(
            f"{label}: source_channel must be a numeric string "
            f"(r14 channel-identity invariant), got {source_channel!r}"
        )
    return problems


def wait_for_peer_texts(
    listener: PeerNode, marker: str, expected: int, timeout: float
) -> list[str]:
    """Poll the peer listener until `expected` copies of marker are seen."""
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        seen = [t for t in listener.received_texts() if marker in t]
        if len(seen) >= expected:
            return seen
        time.sleep(1)
    return seen


def main() -> int:
    runtime = load_runtime()
    untrusted_token = runtime["users"]["untrusted"]["access_token"]
    alice_token = runtime["users"]["alice"]["access_token"]
    relay_a_user = runtime["users"]["relay-a"]["user_id"]
    medre_user = runtime["users"]["medre"]["user_id"]
    alpha_ch0_room = runtime["rooms"]["lab-alpha-ch0"]["room_id"]

    marker_a = f"MEDRE_SMOKE_A_{int(time.time())}_{os.urandom(3).hex()}"
    marker_b = f"MEDRE_SMOKE_B_{int(time.time())}_{os.urandom(3).hex()}"
    result: dict[str, Any] = {
        "marker_radio_to_matrix": marker_a,
        "marker_matrix_to_radio": marker_b,
        "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "path": (
            "A: meshtasticd-a-peer --air-> {meshtasticd-a, meshtasticd-a-medre} "
            "-> mmrelay-a + medre-relay -> #lab-alpha-ch0 (dual envelopes); "
            "B: @alice -> #lab-alpha-ch0 -> {mmrelay-a, medre-relay} --air-> "
            "Alpha (witnessed by meshtasticd-a-peer, no-loop settle window)"
        ),
        "steps": {},
    }

    injector = PeerNode("meshtasticd-a-peer")
    try:
        # ---------------- Part A: radio -> dual canonical envelopes --------
        print(f"[A1] Injecting Alpha radio text via meshtasticd-a-peer: {marker_a}")
        injector.send_text(marker_a)
        result["steps"]["radio_inject"] = {
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "host": "meshtasticd-a-peer",
        }

        print("[A2] Waiting for @relay-a canonical event in #lab-alpha-ch0...")
        relay_event = wait_for_matrix_event(
            token=untrusted_token,
            room_id=alpha_ch0_room,
            timeout_seconds=MEDRE_SMOKE_TIMEOUT_MATRIX,
            sender=relay_a_user,
            event_type="m.room.message",
            body_contains=marker_a,
        )
        print("[A3] Waiting for @medre canonical event in #lab-alpha-ch0...")
        medre_event = wait_for_matrix_event(
            token=untrusted_token,
            room_id=alpha_ch0_room,
            timeout_seconds=MEDRE_SMOKE_TIMEOUT_MATRIX,
            sender=medre_user,
            event_type="m.room.message",
            body_contains=marker_a,
        )
        relay_event = relay_event[0] if relay_event else None
        medre_event = medre_event[0] if medre_event else None

        for label, event, user in (
            ("relay-a", relay_event, relay_a_user),
            ("medre", medre_event, medre_user),
        ):
            if event is None:
                print(f"MEDRE SMOKE FAIL: no Matrix event from {user} containing {marker_a}")
                result["steps"][f"ingress_{label}"] = {"status": "TIMEOUT"}
                write_result(result)
                return 1
            envelope = envelope_of(event)
            problems = envelope_problems(envelope, label)
            print(
                f"  {label}: event_id={event.get('event_id')} "
                f"body={event.get('content', {}).get('body', '')!r}"
            )
            print(f"  {label}: envelope={json.dumps(envelope, sort_keys=True)}")
            result["steps"][f"ingress_{label}"] = {
                "status": "OK" if not problems else "SCHEMA_INVALID",
                "event_id": event.get("event_id"),
                "envelope": envelope,
                "problems": problems,
            }
            if problems:
                for problem in problems:
                    print(f"  SCHEMA PROBLEM: {problem}")
                write_result(result)
                return 1

        # ---------------- Part B: human -> dual radio, no loop -------------
        # The injector PeerNode is already armed as the Alpha-side witness
        # (4s settle on construction, pubsub receive filter on its iface).
        print(f"[B1] Alpha witness armed on meshtasticd-a-peer; @alice sends {marker_b}")
        send_response = matrix_request(
            "PUT",
            f"/_matrix/client/v3/rooms/{alpha_ch0_room}/send/m.room.message/{int(time.time() * 1000)}",
            alice_token,
            {"msgtype": "m.text", "body": marker_b},
        )
        if send_response.status_code >= 400:
            print(f"MEDRE SMOKE FAIL: alice send failed: {send_response.status_code} {send_response.text}")
            result["steps"]["alice_send"] = {"status": "ERROR", "code": send_response.status_code}
            write_result(result)
            return 1
        result["steps"]["alice_send"] = {"status": "OK"}

        print("[B3] Waiting for relay-a message_map row (Alpha radio dispatch)...")
        relay_row = wait_message_map_row(
            "/state/mmrelay-a/database/meshtastic.sqlite",
            send_response.json().get("event_id", ""),
            MEDRE_SMOKE_TIMEOUT_DB,
        )
        if relay_row is None:
            print("MEDRE SMOKE FAIL: relay-a did not map the human text into the Alpha mesh")
            result["steps"]["relay_a_dispatch"] = {"status": "TIMEOUT"}
            write_result(result)
            return 1
        print(f"  relay-a meshtastic_id={relay_row['meshtastic_id']}")
        result["steps"]["relay_a_dispatch"] = {"status": "OK", **relay_row}

        print("[B4] Waiting for BOTH bridges' radio copies on meshtasticd-a-peer...")
        copies = wait_for_peer_texts(
            injector, marker_b, expected=2, timeout=MEDRE_SMOKE_TIMEOUT_RADIO
        )
        result["steps"]["radio_copies_initial"] = {
            "count": len(copies),
            "texts": copies,
            "witness": "meshtasticd-a-peer",
        }
        for text in copies:
            print(f"  radio copy: {text!r}")
        if len(copies) < 2:
            print(
                "MEDRE SMOKE FAIL: expected BOTH bridges on the Alpha radio "
                f"(2 copies), saw {len(copies)}"
            )
            write_result(result)
            return 1

        print(
            f"[B5] Settle window ({MEDRE_SMOKE_SETTLE_SECONDS:.0f}s): watching for "
            "duplication loops (peer copies must be inert on the radio side)..."
        )
        time.sleep(MEDRE_SMOKE_SETTLE_SECONDS)
        final_copies = [t for t in injector.received_texts() if marker_b in t]
        result["steps"]["radio_copies_final"] = {
            "count": len(final_copies),
            "texts": final_copies,
            "settle_seconds": MEDRE_SMOKE_SETTLE_SECONDS,
        }
        if len(final_copies) != 2:
            print(
                "MEDRE SMOKE FAIL: duplication loop — a bridge re-bridged the "
                f"other bridge's copy: {len(final_copies)} radio copies "
                f"(expected exactly 2): {final_copies!r}"
            )
            write_result(result)
            return 1
        print("  no loop: exactly 2 radio copies after settle window")
    finally:
        injector.close()

    result["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    write_result(result)
    print(
        "MEDRE SMOKE PASS: radio text produced BOTH canonical envelopes "
        f"(relay-a={result['steps']['ingress_relay-a']['event_id']}, "
        f"medre={result['steps']['ingress_medre']['event_id']}), the human text "
        "was dispatched to the Alpha radio by BOTH bridges, and no bridge "
        "re-bridged the other's copy (exactly 2 radio copies after settle)."
    )
    return 0


def write_result(result: dict[str, Any]) -> None:
    os.makedirs(LAB_LOGS_DIR, exist_ok=True)
    path = os.path.join(LAB_LOGS_DIR, "medre-smoke-result.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(f"  result written to {path}")


if __name__ == "__main__":
    sys.exit(main())
