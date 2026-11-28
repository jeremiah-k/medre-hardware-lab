"""Unit tests for the Phase 3B bridge proof's strict assertions.

Offline only: they exercise the assertion logic over synthetic probe
results, so the single-delivery contract is pinned by the test suite and
not only by a live Docker run.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

CHECK_PATH = Path(__file__).resolve().parents[1] / "tools" / "openhop_bridge_check.py"

_spec = importlib.util.spec_from_file_location("openhop_bridge_check", CHECK_PATH)
assert _spec and _spec.loader
openhop_bridge_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(openhop_bridge_check)

check_proof = openhop_bridge_check.check_proof

NONCE_1 = "openhop-bridge-mc2mesh-1111"
NONCE_2 = "openhop-bridge-mesh2mc-2222"
PREFIX = "MEDRE-RADIO-a: "


def _proof(**overrides):
    """A passing probe result; the tests break exactly one thing at a time."""
    proof = {
        "case": "openhop-bridge-phase3b",
        "pass": True,
        "reason": "both bridge directions delivered the exact nonce once each",
        "direction_mc_to_mesh": {
            "nonce": NONCE_1,
            "observer_arrivals": [
                {"text": NONCE_1, "received_at": "t1", "channel_index": 0}
            ],
            "arrival_count": 1,
            "distinct_arrival_count": 1,
            "duplicate_client_events": 0,
            "echo_at_b": [],
        },
        "direction_mesh_to_mc": {
            "nonce": NONCE_2,
            "expected_text": PREFIX + NONCE_2,
            "mc_peer_arrivals": [
                {"text": PREFIX + NONCE_2, "received_at": "t2", "channel_idx": 0}
            ],
            "arrival_count": 1,
            "distinct_arrival_count": 1,
            "duplicate_client_events": 0,
        },
    }
    proof.update(overrides)
    return proof


def test_a_clean_run_passes():
    assert check_proof(_proof()) == []


def test_a_duplicate_client_event_in_direction_1_fails():
    """D5 is pinned: a client-level duplicate is a regression, not a quirk."""
    proof = _proof()
    d1 = proof["direction_mc_to_mesh"]
    d1["observer_arrivals"].append({"text": NONCE_1, "received_at": "t1b"})
    d1["arrival_count"] = 2
    d1["duplicate_client_events"] = 1

    failures = check_proof(proof)
    assert failures
    assert any("duplicate client event" in failure for failure in failures)
    assert any("2 arrivals" in failure for failure in failures)


def test_a_duplicate_client_event_in_direction_2_fails():
    proof = _proof()
    d2 = proof["direction_mesh_to_mc"]
    d2["mc_peer_arrivals"].append({"text": PREFIX + NONCE_2, "received_at": "t2b"})
    d2["arrival_count"] = 2
    d2["duplicate_client_events"] = 1

    failures = check_proof(proof)
    assert failures
    assert any("duplicate client event" in failure for failure in failures)
    assert any("2 arrivals" in failure for failure in failures)


def test_arrival_count_must_equal_distinct_arrival_count():
    """A repeat arrival that hides behind the distinct counter still fails."""
    proof = _proof()
    d1 = proof["direction_mc_to_mesh"]
    d1["arrival_count"] = 2
    d1["observer_arrivals"].append({"text": NONCE_1, "received_at": "t1b"})
    d1["duplicate_client_events"] = 0

    failures = check_proof(proof)
    assert failures
    assert any("2 arrivals for 1 distinct text" in failure for failure in failures)


def test_a_missing_arrival_fails():
    proof = _proof()
    d1 = proof["direction_mc_to_mesh"]
    d1["observer_arrivals"] = []
    d1["arrival_count"] = 0
    d1["distinct_arrival_count"] = 0
    d1["duplicate_client_events"] = 0

    failures = check_proof(proof)
    assert failures
    assert any("exactly one distinct arrival" in failure for failure in failures)


def test_a_non_byte_exact_arrival_fails():
    proof = _proof()
    d1 = proof["direction_mc_to_mesh"]
    d1["observer_arrivals"] = [{"text": PREFIX + NONCE_1, "received_at": "t1"}]

    failures = check_proof(proof)
    assert failures
    assert any("byte-exact" in failure for failure in failures)


def test_an_echo_fails():
    proof = _proof()
    proof["direction_mc_to_mesh"]["echo_at_b"] = [{"text": NONCE_1}]

    failures = check_proof(proof)
    assert failures
    assert any("echoed back" in failure for failure in failures)


def test_a_reused_nonce_fails():
    proof = _proof()
    proof["direction_mesh_to_mc"]["nonce"] = NONCE_1

    failures = check_proof(proof)
    assert failures
    assert any("reused one nonce" in failure for failure in failures)


def test_a_failed_probe_is_reported():
    proof = _proof()
    proof["pass"] = False
    proof["reason"] = "the nonce was never delivered"

    failures = check_proof(proof)
    assert failures
    assert any("did not pass" in failure for failure in failures)


@pytest.mark.parametrize("direction", ["direction_mc_to_mesh", "direction_mesh_to_mc"])
def test_both_directions_are_checked(direction):
    """A break in either direction alone fails the proof."""
    proof = _proof()
    target = proof[direction]
    target["arrival_count"] = 3
    target["duplicate_client_events"] = 2

    failures = check_proof(proof)
    assert failures


def test_missing_or_boolean_proof_fields_fail_closed():
    proof = _proof()
    del proof["direction_mc_to_mesh"]["nonce"]
    assert check_proof(proof)
    proof = _proof()
    proof["direction_mc_to_mesh"]["arrival_count"] = True
    assert check_proof(proof)
    proof = _proof()
    del proof["direction_mc_to_mesh"]["echo_at_b"]
    assert check_proof(proof)


def test_wrongly_rendered_additional_nonce_is_not_filtered_out():
    proof = _proof()
    proof["direction_mesh_to_mc"]["mc_peer_arrivals"].append(
        {"text": "wrong: " + NONCE_2}
    )
    assert check_proof(proof)
