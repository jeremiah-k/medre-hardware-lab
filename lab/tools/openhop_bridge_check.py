#!/usr/bin/env python3
"""Assertions for the Phase 3B bridge proof (MEDRE <-> openHop <-> MeshC).

Pure functions over the probe's JSON result: no Docker, no network, no
radio. bridge_proof.sh calls check_proof() and turns the findings into the
proof's exit status; the unit tests call it directly.

WHY THE STRICTNESS
------------------
The openHop companion used to be able to hand a client the SAME queued
message twice (it kept the message in its in-memory queue and in SQLite at
the same time). Phase 3A documented that upstream behaviour, and the first
Phase 3B run therefore REPORTED the client-level duplicate count instead
of failing on it.

That upstream behaviour is pinned shut by the revisions this lab
qualifies (openhop_core fix/companion-frame-admission - a frame is only
admitted into a live client transport; openhop_repeater
fix/companion-queue-ownership - the queue is delivered oldest first
across the memory and persisted queues, and a retired row can only land
in the client that is still attached). A duplicate client event is
therefore a REGRESSION, not a known upstream quirk, and the proof fails
on it:

  * exactly one distinct arrival per direction,
  * arrival_count == distinct_arrival_count (no extra arrivals at all),
  * duplicate_client_events == 0 (no client-level duplicate deliveries),
  * the arrival text is byte-exact (the openHop wire prefix accounted for),
  * no echo back to companion B during direction 1,
  * the two directions never share a nonce.

Both directions are checked independently.

WHAT THESE ASSERTIONS MEASURE
-----------------------------
The counters above describe ONE bounded, successful run on a virtual
medium: how many nonce-bearing messages the client actually saw. The
openHop companion protocol has no application-level acknowledgement of
durable admission, so a crash between the frame enqueue and the row
delete can replay a message and a crash between the delete and the
receipt can lose it. These assertions are regression counters for the
observed path, not a crash-recovery or transactional-delivery proof.
"""

from __future__ import annotations

from typing import Any

__all__ = ["check_proof"]


def _check_direction(
    label: str,
    direction: dict[str, Any],
    *,
    expected_text: str,
    arrival_texts: list[str],
) -> list[str]:
    """Return the failures of one direction (an empty list means it passed)."""
    failures: list[str] = []

    distinct = direction.get("distinct_arrival_count")
    if type(distinct) is not int or distinct != 1:
        failures.append(
            f"{label}: expected exactly one distinct arrival, got {distinct}"
        )

    # Every arrival the client saw must be a DISTINCT delivery of the same
    # text: arrival_count == distinct_arrival_count.
    arrival_count = direction.get("arrival_count")
    if type(arrival_count) is not int or arrival_count != distinct:
        failures.append(
            f"{label}: the client received {arrival_count} arrivals for "
            f"{distinct} distinct text(s); a repeat delivery is a duplicate"
        )

    # The client-level duplicate counter must be zero now that D5 is pinned.
    duplicates = direction.get("duplicate_client_events")
    if type(duplicates) is not int or duplicates != 0:
        failures.append(
            f"{label}: the client saw {duplicates} duplicate client event(s); "
            "the bounded no-duplicate proof failed"
        )

    # The arrival must be byte-exact.
    if arrival_texts != [expected_text]:
        failures.append(
            f"{label}: expected exactly one byte-exact arrival of "
            f"{expected_text!r}, got {arrival_texts!r}"
        )

    return failures


def check_proof(proof: dict[str, Any]) -> list[str]:
    """Return every failure of the probe result (empty list = PASS)."""
    failures: list[str] = []

    if proof.get("pass") is not True:
        failures.append(f"the bridge probe did not pass: {proof.get('reason')}")

    d1 = proof.get("direction_mc_to_mesh") or {}
    d2 = proof.get("direction_mesh_to_mc") or {}

    for name, direction in (("direction 1", d1), ("direction 2", d2)):
        nonce = direction.get("nonce")
        if not isinstance(nonce, str) or not nonce.strip():
            failures.append(f"{name}: missing nonempty nonce")
    if failures and any("missing nonempty nonce" in item for item in failures):
        return failures

    if d1["nonce"] == d2["nonce"]:
        failures.append("the two directions reused one nonce")

    # --- direction 1: MC peer -> MEDRE -> MeshC peer ------------------------
    # The MeshC observer sees the BARE nonce: MEDRE's meshcore codec lifts
    # openHop's wire prefix off the body.
    d1_arrivals = d1.get("observer_arrivals") or []
    failures += _check_direction(
        "direction 1 (mc-peer -> MEDRE -> meshc-peer)",
        d1,
        expected_text=str(d1.get("nonce")),
        arrival_texts=[str(entry.get("text")) for entry in d1_arrivals],
    )
    if not isinstance(d1.get("echo_at_b"), list):
        failures.append("direction 1: missing echo observation")
    elif d1.get("echo_at_b"):
        failures.append(
            f"direction 1 echoed back to companion B "
            f"({len(d1['echo_at_b'])} message(s))"
        )

    # --- direction 2: MeshC peer -> MEDRE -> MC peer ------------------------
    # The virtual MC peer sees the openHop wire prefix repeater A stamped
    # on the outbound group text.
    d2_expected = d2.get("expected_text")
    if not isinstance(d2_expected, str) or d2["nonce"] not in d2_expected:
        failures.append("direction 2: expected text must contain the nonce")
        return failures
    d2_arrivals = [
        str(entry.get("text"))
        for entry in (d2.get("mc_peer_arrivals") or [])
        if d2["nonce"] in str(entry.get("text") or "")
    ]
    failures += _check_direction(
        "direction 2 (meshc-peer -> MEDRE -> mc-peer)",
        d2,
        expected_text=d2_expected,
        arrival_texts=d2_arrivals,
    )

    return failures
