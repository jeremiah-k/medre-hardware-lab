"""Offline checks for the opt-in openHop BRIDGE profile (Phase 3B).

Topology and wiring assertions only: they read the committed compose,
config and script files and never contact Docker, a radio or a network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "compose.yaml"
BRIDGE_CONFIG = ROOT / "lab" / "config" / "medre-openhop-bridge.yaml"
PHASE2_CONFIG = ROOT / "lab" / "config" / "medre-openhop.yaml"
RADIO_CONFIG = ROOT / "lab" / "config" / "openhop-radio-repeater.yaml"
PROBE = ROOT / "lab" / "tools" / "openhop_bridge_probe.py"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text())


@pytest.fixture(scope="module")
def bridge_config() -> dict:
    return yaml.safe_load(BRIDGE_CONFIG.read_text())


def test_bridge_profile_exists(compose):
    """The bridge profile is opt-in and separate from Phase 2 / Phase 3A."""
    services = compose["services"]
    assert "medre-openhop-bridge" in services
    assert "openhop-bridge-probe" in services
    assert services["medre-openhop-bridge"]["profiles"] == ["openhop-bridge"]
    assert services["openhop-bridge-probe"]["profiles"] == ["openhop-bridge"]


def test_bridge_profile_is_exclusive(compose):
    """The bridge profile owns its own rig; no other profile's MEDRE."""
    services = compose["services"]
    # Phase 2's openhop profile keeps its own repeater and MEDRE.
    assert services["openhop-repeater"]["profiles"] == ["openhop"]
    assert services["medre-openhop"]["profiles"] == ["openhop"]
    # The virtual-radio repeaters are shared with the bridge profile (they
    # are the same two nodes), but the bridge profile's MEDRE is its own.
    assert services["medre-openhop-bridge"] is not services["medre-openhop"]
    assert services["medre-openhop-bridge"]["container_name"] == (
        "medre-lab-medre-openhop-bridge"
    )


def test_bridge_publishes_no_ports(compose):
    """Nothing of the bridge rig is reachable from the host."""
    services = compose["services"]
    for name in ("medre-openhop-bridge", "openhop-bridge-probe"):
        assert "ports" not in services[name], f"{name} must not publish ports"
    # The repeaters and the relay publish no host ports either.
    for name in (
        "openhop-radio-relay",
        "openhop-repeater-a",
        "openhop-repeater-b",
    ):
        assert "ports" not in services[name], f"{name} must not publish ports"


def test_bridge_healthchecks_are_liveness_only(compose):
    """No healthcheck may connect to a companion port (one client at a time)."""
    services = compose["services"]
    for name in (
        "medre-openhop-bridge",
        "openhop-repeater-a",
        "openhop-repeater-b",
        "openhop-radio-relay",
    ):
        test = str(services[name]["healthcheck"]["test"])
        assert "/proc" in test, f"{name} healthcheck must be process liveness"
        assert "5000" not in test
        assert "5055" not in test


def test_bridge_route_is_bidirectional_and_enabled(bridge_config):
    """The bridge route exists ONLY in the bridge config, both ways."""
    routes = bridge_config["routes"]
    assert list(routes) == ["openhop-bridge"]
    route = routes["openhop-bridge"]
    assert route["source_adapters"] == ["openhop-a"]
    assert route["dest_adapters"] == ["radio-c"]
    assert route["directionality"] == "bidirectional"
    assert route["enabled"] is True
    assert route["source_channel"] == "0"
    assert route["dest_channel"] == "0"


def test_phase2_route_stays_disabled():
    """Phase 2 keeps its route disabled: no bridge there."""
    phase2 = yaml.safe_load(PHASE2_CONFIG.read_text())
    for route in phase2["routes"].values():
        assert route["enabled"] is False


def test_bridge_adapters_point_at_the_right_nodes(bridge_config):
    """The meshcore adapter owns companion A; meshtastic owns the MeshC node."""
    adapters = bridge_config["adapters"]
    meshcore = adapters["meshcore"]["openhop-a"]
    assert meshcore["host"] == "openhop-repeater-a"
    assert meshcore["port"] == 5000
    assert meshcore["default_channel"] == 0
    meshtastic = adapters["meshtastic"]["radio-c"]
    assert meshtastic["host"] == "meshtasticd-c-medre"
    assert meshtastic["port"] == 4403
    assert "0" in meshtastic["channel_mapping"]


def test_bridge_relay_prefixes_are_empty(bridge_config):
    """Empty relay prefixes keep the relayed bytes byte-exact."""
    adapters = bridge_config["adapters"]
    assert adapters["meshcore"]["openhop-a"]["relay_prefix"] == ""
    assert adapters["meshtastic"]["radio-c"]["relay_prefix"] == ""


def test_probe_uses_the_pinned_sdk_and_both_roles():
    """The probe is the virtual MC peer (companion B) and the MeshC observer."""
    source = PROBE.read_text()
    assert "import meshcore" in source
    assert "meshtastic.tcp_interface" in source
    # The expected direction-2 prefix comes from the environment (the
    # generated repeater config), never guessed in code.
    assert "OPENHOP_BRIDGE_EXPECT_PREFIX" in source


def test_proof_script_asserts_exactly_one_arrival():
    """The proof asserts the exact bytes and the single delivery."""
    proof = (ROOT / "lab" / "openhop-bridge" / "bridge_proof.sh").read_text()
    assert "arrival_count" in proof
    assert "distinct_arrival_count" in proof
    assert "duplicate_client_events" in proof
    assert "byte-exact" in proof
    assert "protocol-simulated" in proof
    assert "echo_at_b" in proof
    # The radio-level single crossing is asserted too.
    assert "relay transmissions" in proof
    assert "RX GRP_TXT" in proof


def test_proof_script_fails_on_a_duplicate_client_event():
    """D5 is pinned, so a duplicate client delivery must FAIL the proof.

    The strict assertions live in lab/tools/openhop_bridge_check.py, which
    the proof script calls; the test below pins that the script really
    uses the strict checker and does not merely REPORT the duplicate.
    """
    proof = (ROOT / "lab" / "openhop-bridge" / "bridge_proof.sh").read_text()
    assert "openhop_bridge_check" in proof
    assert "check_proof" in proof
    # A failure must abort the proof, not be logged and ignored.
    assert "FAIL:" in proof
    assert "raise SystemExit" in proof
    # The checker itself must treat a duplicate as a failure.
    check = (ROOT / "lab" / "tools" / "openhop_bridge_check.py").read_text()
    assert "duplicate_client_events" in check
    assert "duplicate client event" in check
    assert "arrival_count != distinct" in check


def test_proof_writes_the_evidence_before_it_fails():
    """A failed run must not leave the previous run's verdict behind.

    The proof clears the artifacts it rewrites, writes the evidence and the
    verdict BEFORE it fails, and reports every failure it found together.
    """
    proof = (ROOT / "lab" / "openhop-bridge" / "bridge_proof.sh").read_text()
    # The stale artifacts are cleared up front.
    assert "rm -f" in proof
    assert "verdict.json" in proof
    # The evidence is written BEFORE the failure list is handed to the shell.
    write_at = proof.index('print("evidence written")')
    fail_at = proof.index('Path(evidence_dir / "failures.txt")')
    assert write_at < fail_at
    # The failures are collected and reported together at the end.
    assert "ALL_FAILURES" in proof
    assert "the Phase 3B bridge proof failed" in proof


def test_repeater_config_is_unchanged_by_the_bridge():
    """The bridge reuses the Phase 3A repeater contract (monitor mode)."""
    radio = yaml.safe_load(RADIO_CONFIG.read_text())
    assert radio["repeater"]["mode"] == "monitor"
    assert radio["mesh"]["unscoped_flood_allow"] is True
    assert radio["mesh"]["global_flood_allow"] is True
    assert radio["radio_type"] == "modem_tcp"


def test_seed_script_is_present_and_pins_the_candidates():
    """The seed script materialises the pinned candidate SHAs."""
    seed = (ROOT / "lab" / "openhop-bridge" / "seed_candidate_sources.sh").read_text()
    assert "186ad7d1d5dcdbecec3337a06e3a213a33ee9956" in seed
    assert "8ba54facbd8fc9e3a7d5b75f8b43b279cabe344f" in seed
    assert "c404c8fe120bc8ec9671f969ca7ca14158cdd6ea" in seed
    assert "05684df6ada1697d9df4fca912700c4aaf01751b" in seed
    assert "--check" in seed
    assert re.search(r"rev-parse HEAD", seed)


def test_seed_script_fetches_each_candidate_by_its_branch_name():
    """The seeder takes each revision by branch name, not by a source HEAD.

    The revisions under qualification live on named branches in local
    clones. Naming the branch keeps the seed independent of whatever the
    source repository happens to have checked out, and naming it in plain
    words keeps the lab free of private ref namespaces.
    """
    seed = (ROOT / "lab" / "openhop-bridge" / "seed_candidate_sources.sh").read_text()
    assert "fix/meshcore-receive-pump" in seed
    assert "fix/companion-frame-admission" in seed
    assert "fix/companion-queue-ownership" in seed
    assert "feat/openhop-live-status" in seed
    # Every git ref the seeder names lives in a public namespace - the
    # heads it fetches from the source clones, or the remote-tracking
    # refs it creates inside its own throwaway seed checkout.  No
    # private ref namespace appears anywhere in the tracked seeder.
    named_refs = re.findall(r"refs/[\w./-]+", seed)
    assert named_refs, "the seeder must name the branches it fetches"
    assert all(
        ref.startswith("refs/heads/") or ref.startswith("refs/remotes/")
        for ref in named_refs
    )
    # The seeder must not fetch "whatever the branch points at": every
    # candidate is also pinned by its exact SHA and checked out detached.
    assert "${branch}" not in seed
