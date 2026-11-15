"""NORMAL-profile interop smoke: star-topology fan-out + loop freedom.

Exercises the `normal` compose profile (medre-normal on its OWN meshnet
MeshC/GammaLab + loopback LXMF leg; mmrelay-a on AlphaLab; mmrelay-b on
BetaLab; the shared surface is #lab-bridge ch0 / #lab-bridge-ch2 ch2).

Part A (fan-out + loop-freedom proof, smoke (a) of the --normal contract):
  @alice sends a unique text into #lab-bridge. The text must air EXACTLY
  ONCE on each of the three meshnets (witnessed by live listeners on
  meshtasticd-a-peer / -b-peer / -c-peer). Then, over a settle window
  (default 120s, >= 2 minutes per the contract):
    - each peer still carries EXACTLY ONE copy (no re-air cascade),
    - no body containing the marker carries a nested relay prefix chain
      (the cross-bridge loop signature triaged 2026-11-11),
    - both mmrelay message_map tables, the medre-normal mapping store
      (canonical_events / native_message_refs / delivery_outbox), and the
      mmrelay send-queue ("Queued message" log lines) are FLAT across the
      window — growth would mean a bridge kept re-bridging copies.

Part B (radio -> room, smoke (b)):
  (b1) inject on meshtasticd-c-peer -> medre-normal emits exactly ONE
       canonical Matrix event into #lab-bridge, schema-valid (envelope
       schema_version 1, numeric source_channel, non-empty canonical id)
       and with radio attribution.
  (b2) inject on meshtasticd-a-peer -> @relay-a's copy present;
       medre-normal re-airs it on MeshC exactly once (c-peer observes);
       mmrelay-b re-bridges it onto BetaLab exactly once (b-peer observes);
       a short settle keeps everything flat (no cascade).

The LXMF counterpart leg is driven separately by lab/up.sh +
lab/tools/lxmf_counterpart.py (it must run inside the medre-normal
container); this smoke records the medre-side lxmf0 outbound refs it can
observe in the mapping store.

Relay-connected primaries are NEVER touched (single API client per node):
injection and witnessing go exclusively through the *-peer nodes.

Usage (inside lab-tools): python /opt/lab-tools/normal_smoke.py
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any

from lab_lib import (
    LAB_LOGS_DIR,
    load_runtime,
    matrix_request,
)
from smoke import PeerNode

SMOKE_TIMEOUT_MATRIX = float(os.environ.get("SMOKE_TIMEOUT_MATRIX", "120"))
SMOKE_TIMEOUT_RADIO = float(os.environ.get("SMOKE_TIMEOUT_RADIO", "60"))
SMOKE_SETTLE_SECONDS = float(os.environ.get("NORMAL_SMOKE_SETTLE_SECONDS", "120"))
SMOKE_B_SETTLE_SECONDS = float(os.environ.get("NORMAL_SMOKE_B_SETTLE_SECONDS", "20"))

# Nested-prefix loop signature (triage 2026-11-11): relay prefixes applied
# more than once to one body, or meshnet tags ([!...] labels) repeated.
_NESTED_PREFIX_RE = re.compile(r"(relay-[ab]: .*){2,}")
_MESHNET_TAG_RE = re.compile(r"\[![0-9a-f]{8}")


def utcnow() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def read_sqlite_ro(db_path: str) -> sqlite3.Connection:
    """Open a WAL-mode SQLite db read-only via a scratch copy.

    The state volumes are read-only mounts and the databases are WAL: a
    mode=ro open cannot create the -shm, so copy db+wal to a scratch dir
    first (same reader pattern as the scenario suite's medre_maps helper).
    """
    scratch = tempfile.mkdtemp(prefix="smoke-db-")
    target = os.path.join(scratch, os.path.basename(db_path))
    shutil.copy(db_path, target)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            shutil.copy(db_path + suffix, target + suffix)
    conn = sqlite3.connect(target, timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def table_count(db_path: str, query: str, params: tuple = ()) -> int:
    conn = read_sqlite_ro(db_path)
    try:
        return int(conn.execute(query, params).fetchone()[0])
    finally:
        conn.close()


def mapping_counts() -> dict[str, int]:
    """Global state counters (reported; concurrent scenario traffic moves
    these, so loop detection is marker-scoped — see marker_counts)."""
    return {
        "mmrelay_a_message_map": table_count(
            "/state/mmrelay-a/database/meshtastic.sqlite",
            "SELECT COUNT(*) FROM message_map",
        ),
        "mmrelay_b_message_map": table_count(
            "/state/mmrelay-b/database/meshtastic.sqlite",
            "SELECT COUNT(*) FROM message_map",
        ),
        "mmrelay_a_queued_log_lines": queued_log_lines("a"),
        "mmrelay_b_queued_log_lines": queued_log_lines("b"),
        "medre_canonical_events": table_count(
            "/state/medre-normal/state/medre.sqlite",
            "SELECT COUNT(*) FROM canonical_events",
        ),
        "medre_native_message_refs": table_count(
            "/state/medre-normal/state/medre.sqlite",
            "SELECT COUNT(*) FROM native_message_refs",
        ),
        "medre_delivery_outbox": table_count(
            "/state/medre-normal/state/medre.sqlite",
            "SELECT COUNT(*) FROM delivery_outbox",
        ),
    }


def marker_counts(marker: str) -> dict[str, int]:
    """Marker-scoped state counters: the loop-freedom invariant.

    A cross-bridge loop re-bridges the SAME text, producing NEW map rows /
    canonical events / delivery refs that carry the marker. External
    concurrent traffic (other markers) must not move these counters. All
    counts must stay FLAT across the settle window.
    """
    escaped = marker.replace("%", "")
    like = f"%{escaped}%"
    counts: dict[str, int] = {}
    for relay in ("a", "b"):
        counts[f"mmrelay_{relay}_map_rows_with_marker"] = table_count(
            f"/state/mmrelay-{relay}/database/meshtastic.sqlite",
            "SELECT COUNT(*) FROM message_map WHERE meshtastic_text LIKE ?",
            (like,),
        )
    medre_db = "/state/medre-normal/state/medre.sqlite"
    counts["medre_canonical_events_with_marker"] = table_count(
        medre_db,
        "SELECT COUNT(*) FROM canonical_events WHERE payload LIKE ?",
        (like,),
    )
    counts["medre_refs_for_marker_events"] = table_count(
        medre_db,
        "SELECT COUNT(*) FROM native_message_refs WHERE event_id IN "
        "(SELECT event_id FROM canonical_events WHERE payload LIKE ?)",
        (like,),
    )
    return counts


def queued_log_lines(relay: str) -> int:
    """Count mmrelay send-queue 'Queued message (N/M)' log lines (both files)."""
    total = 0
    base = f"/state/mmrelay-{relay}/logs/mmrelay.log"
    for path in (base, base + ".1"):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                total += sum(1 for line in handle if "Queued message" in line)
        except OSError:
            continue
    return total


def room_messages_since(
    token: str, room_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    """Fetch recent room timeline events via the messages endpoint."""
    quoted = room_id.replace("!", "%21").replace(":", "%3A")
    response = matrix_request(
        "GET",
        f"/_matrix/client/v3/rooms/{quoted}/messages",
        token,
        params={"dir": "b", "limit": str(limit)},
    )
    if response.status_code >= 400:
        return []
    chunk = response.json().get("chunk", [])
    return list(reversed(chunk))


def marker_bodies(events: list[dict[str, Any]], marker: str) -> list[str]:
    bodies: list[str] = []
    for event in events:
        if event.get("type") != "m.room.message":
            continue
        content = event.get("content", {})
        body = content.get("body", "") if isinstance(content, dict) else ""
        if isinstance(body, str) and marker in body:
            bodies.append(body)
    return bodies


def nested_prefix_problems(bodies: list[str], label: str) -> list[str]:
    problems: list[str] = []
    for body in bodies:
        if _NESTED_PREFIX_RE.search(body):
            problems.append(f"{label}: nested relay prefixes: {body!r}")
        elif len(_MESHNET_TAG_RE.findall(body)) > 1:
            problems.append(f"{label}: repeated meshnet tags: {body!r}")
    return problems


def wait_for_peer_texts(
    listener: PeerNode, marker: str, expected: int, timeout: float
) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        seen = [t for t in listener.received_texts() if marker in t]
        if len(seen) >= expected:
            return seen
        time.sleep(1)
    return [t for t in listener.received_texts() if marker in t]


def wait_for_peer_packet_ids(
    listener: PeerNode, marker: str, expected: int, timeout: float
) -> list[dict[str, Any]]:
    """Wait until `expected` DISTINCT radio packet ids carry the marker.

    The firmware retransmits reliable packets until ACKed; hearing the same
    packet id twice is reliability, not a second bridge emission. A loop (a
    bridge re-bridging a copy) produces a NEW packet id — that is what this
    counts.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        distinct = distinct_marker_packets(listener, marker)
        if len(distinct) >= expected:
            return distinct
        time.sleep(1)
    return distinct_marker_packets(listener, marker)


def distinct_marker_packets(listener: PeerNode, marker: str) -> list[dict[str, Any]]:
    seen_ids: set[Any] = set()
    distinct: list[dict[str, Any]] = []
    for record in listener.received_packets():
        if marker not in str(record.get("text", "")):
            continue
        packet_id = record.get("packet_id")
        if packet_id in seen_ids:
            continue
        seen_ids.add(packet_id)
        distinct.append(record)
    return distinct


def wait_for_room_event_count(
    token: str,
    room_id: str,
    marker: str,
    expected: int,
    timeout: float,
    sender: str | None = None,
) -> list[dict[str, Any]]:
    """Poll the room until `expected` messages mention the marker (and, when
    given, originate from `sender`)."""
    deadline = time.monotonic() + timeout
    matched: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        matched = [
            event
            for event in room_messages_since(token, room_id)
            if event.get("type") == "m.room.message"
            and marker in str(event.get("content", {}).get("body", ""))
            and (sender is None or event.get("sender") == sender)
        ]
        if len(matched) >= expected:
            return matched
        time.sleep(1)
    return matched


def envelope_of(event: dict[str, Any]) -> dict[str, Any] | None:
    content = event.get("content", {})
    medre = content.get("medre", {}) if isinstance(content, dict) else {}
    envelope = medre.get("envelope") if isinstance(medre, dict) else None
    return envelope if isinstance(envelope, dict) else None


def envelope_problems(envelope: dict[str, Any] | None, label: str) -> list[str]:
    """Shared canonical-envelope structural contract (see medre_smoke.py)."""
    if envelope is None:
        return [f"{label}: canonical envelope missing (content['medre']['envelope'])"]
    problems: list[str] = []
    schema_version = envelope.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        problems.append(
            f"{label}: schema_version must be int 1, got {schema_version!r}"
        )
    canonical_event_id = envelope.get("canonical_event_id")
    if not (isinstance(canonical_event_id, str) and canonical_event_id.strip()):
        problems.append(f"{label}: canonical_event_id missing/malformed")
    source_adapter = envelope.get("source_adapter")
    if not (isinstance(source_adapter, str) and source_adapter.strip()):
        problems.append(f"{label}: source_adapter must be a non-empty string")
    source_channel = envelope.get("source_channel")
    if not (isinstance(source_channel, str) and source_channel.isdigit()):
        problems.append(
            f"{label}: source_channel must be numeric, got {source_channel!r}"
        )
    return problems


def write_result(result: dict[str, Any]) -> None:
    os.makedirs(LAB_LOGS_DIR, exist_ok=True)
    path = os.path.join(LAB_LOGS_DIR, "normal-smoke-result.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(f"  result written to {path}")


def fail(result: dict[str, Any], message: str) -> int:
    print(f"NORMAL SMOKE FAIL: {message}")
    result["verdict"] = "FAIL"
    write_result(result)
    return 1


def main() -> int:
    runtime = load_runtime()
    alice_token = runtime["users"]["alice"]["access_token"]
    admin_token = runtime["users"]["admin"]["access_token"]
    relay_a_user = runtime["users"]["relay-a"]["user_id"]
    runtime["users"]["relay-b"]["user_id"]
    medre_user = runtime["users"]["medre"]["user_id"]
    bridge_room = runtime["rooms"]["lab-bridge"]["room_id"]

    marker_a = f"NORMSMOKE_A_{int(time.time())}_{os.urandom(3).hex()}"
    marker_c = f"NORMSMOKE_C_{int(time.time())}_{os.urandom(3).hex()}"
    marker_a2 = f"NORMSMOKE_B_{int(time.time())}_{os.urandom(3).hex()}"
    result: dict[str, Any] = {
        "marker_fanout": marker_a,
        "marker_c_inject": marker_c,
        "marker_a_inject": marker_a2,
        "started_at": utcnow(),
        "topology": (
            "star: mmrelay-a (AlphaLab) + mmrelay-b (BetaLab) + medre-normal "
            "(MeshC/GammaLab + lxmf loopback) sharing #lab-bridge ch0 / "
            "#lab-bridge-ch2 ch2"
        ),
        "settle_seconds": SMOKE_SETTLE_SECONDS,
        "steps": {},
        "verdict": "PENDING",
    }

    injector_c = PeerNode("meshtasticd-c-peer")
    injector_a = PeerNode("meshtasticd-a-peer")
    witness_b = PeerNode("meshtasticd-b-peer")
    try:
        # ============================ Part A ============================
        print(f"[A1] @alice sends fan-out marker in #lab-bridge: {marker_a}")
        send_response = matrix_request(
            "PUT",
            f"/_matrix/client/v3/rooms/{bridge_room}/send/m.room.message/{int(time.time() * 1000)}",
            alice_token,
            {"msgtype": "m.text", "body": marker_a},
        )
        if send_response.status_code >= 400:
            result["steps"]["alice_send"] = {
                "status": "ERROR",
                "code": send_response.status_code,
            }
            return fail(result, f"alice send failed: {send_response.text}")
        result["steps"]["alice_send"] = {"status": "OK"}

        print("[A2] Waiting for the marker to air on all THREE meshnets...")
        packets_alpha = wait_for_peer_packet_ids(
            injector_a, marker_a, expected=1, timeout=SMOKE_TIMEOUT_RADIO
        )
        packets_beta = wait_for_peer_packet_ids(
            witness_b, marker_a, expected=1, timeout=SMOKE_TIMEOUT_RADIO
        )
        packets_gamma = wait_for_peer_packet_ids(
            injector_c, marker_a, expected=1, timeout=SMOKE_TIMEOUT_RADIO
        )
        print(
            f"  AlphaLab={len(packets_alpha)} BetaLab={len(packets_beta)} "
            f"MeshC={len(packets_gamma)} (distinct packet ids)"
        )
        result["steps"]["fanout_initial"] = {
            "alpha": packets_alpha,
            "beta": packets_beta,
            "gamma": packets_gamma,
        }
        if not (packets_alpha and packets_beta and packets_gamma):
            return fail(
                result,
                "fan-out incomplete: each mesh must carry the text "
                f"(alpha={len(packets_alpha)} beta={len(packets_beta)} "
                f"gamma={len(packets_gamma)})",
            )

        print("[A3] Mapping tables + queue logs snapshot (t0)...")
        counts_t0 = mapping_counts()
        marker_t0 = marker_counts(marker_a)
        result["steps"]["mapping_counts_t0"] = counts_t0
        result["steps"]["marker_counts_t0"] = marker_t0
        print(f"  global: {counts_t0}")
        print(f"  marker-scoped: {marker_t0}")

        print(
            f"[A4] Settle window ({SMOKE_SETTLE_SECONDS:.0f}s): loop-freedom "
            "proof (copies must stay exactly one per mesh, state flat)..."
        )
        time.sleep(SMOKE_SETTLE_SECONDS)

        packets_alpha_final = distinct_marker_packets(injector_a, marker_a)
        packets_beta_final = distinct_marker_packets(witness_b, marker_a)
        packets_gamma_final = distinct_marker_packets(injector_c, marker_a)
        raw_heard = {
            "alpha_raw_heard": len(
                [t for t in injector_a.received_texts() if marker_a in t]
            ),
            "beta_raw_heard": len(
                [t for t in witness_b.received_texts() if marker_a in t]
            ),
            "gamma_raw_heard": len(
                [t for t in injector_c.received_texts() if marker_a in t]
            ),
        }
        counts_t1 = mapping_counts()
        marker_t1 = marker_counts(marker_a)
        result["steps"]["fanout_final"] = {
            "alpha": packets_alpha_final,
            "beta": packets_beta_final,
            "gamma": packets_gamma_final,
            **raw_heard,
        }
        result["steps"]["mapping_counts_t1"] = counts_t1
        result["steps"]["marker_counts_t1"] = marker_t1

        for mesh, packets in (
            ("alpha", packets_alpha_final),
            ("beta", packets_beta_final),
            ("gamma", packets_gamma_final),
        ):
            if len(packets) != 1:
                return fail(
                    result,
                    f"{mesh} mesh carried {len(packets)} DISTINCT radio "
                    f"packets of the fan-out marker after settle (expected "
                    f"exactly 1): {packets!r}",
                )
        # Loop-freedom proof: marker-scoped counters must be FLAT. (The global
        # counters move with concurrent scenario traffic and are reported
        # only; a loop re-bridges the SAME text and would grow the
        # marker-scoped rows.)
        flat_deltas: dict[str, int] = {
            key: marker_t1[key] - marker_t0[key] for key in marker_t0
        }
        result["steps"]["marker_deltas_over_settle"] = flat_deltas
        result["steps"]["global_deltas_over_settle"] = {
            key: counts_t1[key] - counts_t0[key] for key in counts_t0
        }
        print(f"  marker-scoped deltas over settle: {flat_deltas}")
        print(
            f"  global deltas (informational): {result['steps']['global_deltas_over_settle']}"
        )
        if any(delta != 0 for delta in flat_deltas.values()):
            return fail(
                result,
                f"marker-scoped mapping rows grew over the settle window "
                f"(loop signature): {flat_deltas}",
            )

        print("[A5] Nested-prefix scan (room bodies + all sniffed copies)...")
        room_events = room_messages_since(admin_token, bridge_room)
        bodies = marker_bodies(room_events, marker_a)
        sniffed = [
            record["text"]
            for record in packets_alpha_final + packets_beta_final + packets_gamma_final
        ]
        problems = nested_prefix_problems(bodies, "room") + nested_prefix_problems(
            sniffed, "radio"
        )
        result["steps"]["nested_prefix_scan"] = {
            "room_bodies": bodies,
            "radio_bodies": sniffed,
            "problems": problems,
        }
        if problems:
            return fail(result, f"nested-prefix loop signature found: {problems}")
        print("  no nested-prefix bodies (room + all three meshes)")

        # Informational: medre's lxmf0 outbound refs for the fan-out leg.
        try:
            lxmf_refs = table_count(
                "/state/medre-normal/state/medre.sqlite",
                "SELECT COUNT(*) FROM native_message_refs WHERE adapter='lxmf0'",
            )
            result["steps"]["medre_lxmf0_outbound_refs"] = lxmf_refs
            print(f"  medre lxmf0 native refs: {lxmf_refs}")
        except Exception as exc:  # noqa: BLE001 - informational only
            print(f"  lxmf0 ref query unavailable: {exc}")

        # ============================ Part B ============================
        # (b1) c-peer injection -> ONE canonical medre event in #lab-bridge.
        print(f"[B1] Injecting on meshtasticd-c-peer: {marker_c}")
        injector_c.send_text(marker_c)
        medre_events = wait_for_room_event_count(
            admin_token,
            bridge_room,
            marker_c,
            expected=1,
            timeout=SMOKE_TIMEOUT_MATRIX,
            sender=medre_user,
        )
        if not medre_events:
            return fail(result, "no @medre canonical event for the MeshC injection")
        event = medre_events[0]
        envelope = envelope_of(event)
        problems = envelope_problems(envelope, "medre-normal(b1)")
        native_meta = (
            event.get("content", {})
            .get("medre", {})
            .get("envelope", {})
            .get("metadata", {})
        )
        print(
            f"  medre event_id={event.get('event_id')} body={event.get('content', {}).get('body', '')!r}"
        )
        print(f"  envelope={json.dumps(envelope, sort_keys=True)}")
        result["steps"]["b1_medre_event"] = {
            "event_id": event.get("event_id"),
            "envelope": envelope,
            "native_metadata_keys": (
                sorted(native_meta.keys()) if isinstance(native_meta, dict) else []
            ),
            "problems": problems,
        }
        if problems:
            return fail(result, f"b1 envelope invalid: {problems}")

        # (b2) a-peer injection -> relay-a copy -> medre re-air on MeshC +
        #      relay-b re-bridge on BetaLab, each exactly once.
        print(f"[B2] Injecting on meshtasticd-a-peer: {marker_a2}")
        injector_a.send_text(marker_a2)
        relay_a_events = wait_for_room_event_count(
            admin_token,
            bridge_room,
            marker_a2,
            expected=1,
            timeout=SMOKE_TIMEOUT_MATRIX,
            sender=relay_a_user,
        )
        if not relay_a_events:
            return fail(result, "no @relay-a copy for the Alpha injection")
        relay_a_event = relay_a_events[0]
        print(
            f"  relay-a event_id={relay_a_event.get('event_id')} "
            f"body={relay_a_event.get('content', {}).get('body', '')!r}"
        )
        result["steps"]["b2_relay_a_event"] = {
            "event_id": relay_a_event.get("event_id"),
            "body": relay_a_event.get("content", {}).get("body", ""),
        }

        gamma_b2 = wait_for_peer_packet_ids(
            injector_c, marker_a2, expected=1, timeout=SMOKE_TIMEOUT_RADIO
        )
        beta_b2 = wait_for_peer_packet_ids(
            witness_b, marker_a2, expected=1, timeout=SMOKE_TIMEOUT_RADIO
        )
        print(f"  MeshC distinct={len(gamma_b2)} BetaLab distinct={len(beta_b2)}")
        result["steps"]["b2_radio_copies_initial"] = {
            "gamma": gamma_b2,
            "beta": beta_b2,
        }
        if not gamma_b2:
            return fail(
                result, "medre-normal did not re-air the Alpha injection on MeshC"
            )
        if not beta_b2:
            return fail(
                result, "mmrelay-b did not re-bridge the Alpha injection to BetaLab"
            )

        print(
            f"[B3] Short settle ({SMOKE_B_SETTLE_SECONDS:.0f}s): re-air must "
            "be exactly once per mesh..."
        )
        time.sleep(SMOKE_B_SETTLE_SECONDS)
        gamma_b2_final = distinct_marker_packets(injector_c, marker_a2)
        beta_b2_final = distinct_marker_packets(witness_b, marker_a2)
        alpha_b2_final = distinct_marker_packets(injector_a, marker_a2)
        result["steps"]["b2_radio_copies_final"] = {
            "gamma": gamma_b2_final,
            "beta": beta_b2_final,
            "alpha(self-heard)": alpha_b2_final,
        }
        if len(gamma_b2_final) != 1:
            return fail(
                result,
                f"MeshC carried {len(gamma_b2_final)} DISTINCT packets of the "
                f"Alpha injection after settle (expected exactly 1): "
                f"{gamma_b2_final!r}",
            )
        if len(beta_b2_final) != 1:
            return fail(
                result,
                f"BetaLab carried {len(beta_b2_final)} copies after settle "
                f"(expected exactly 1): {beta_b2_final!r}",
            )

        bodies_b2 = [
            str(e.get("content", {}).get("body", ""))
            for e in room_messages_since(admin_token, bridge_room)
            if marker_a2 in str(e.get("content", {}).get("body", ""))
        ]
        problems_b2 = nested_prefix_problems(
            bodies_b2, "room-b2"
        ) + nested_prefix_problems(
            [r["text"] for r in gamma_b2_final + beta_b2_final], "radio-b2"
        )
        result["steps"]["b2_nested_prefix_scan"] = {
            "room_bodies": bodies_b2,
            "problems": problems_b2,
        }
        if problems_b2:
            return fail(result, f"b2 nested-prefix signature: {problems_b2}")
    finally:
        injector_c.close()
        injector_a.close()
        witness_b.close()

    result["finished_at"] = utcnow()
    result["verdict"] = "PASS"
    write_result(result)
    print(
        "NORMAL SMOKE PASS: fan-out aired exactly once per mesh and stayed "
        "flat over the settle window; radio->room produced one canonical "
        "envelope per direction with no cascade and no nested prefixes."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
