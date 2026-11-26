"""Offline checks for the opt-in openHop VIRTUAL RADIO profile.

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
RADIO_CONFIG = ROOT / "lab" / "config" / "openhop-radio-repeater.yaml"
RELAY_SOURCE = ROOT / "lab" / "openhop-radio" / "openhop_radio_relay.py"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text())


@pytest.fixture(scope="module")
def radio_config() -> dict:
    return yaml.safe_load(RADIO_CONFIG.read_text())


def test_radio_profile_exists(compose):
    """The radio profile is opt-in and separate from the Phase 2 profile."""
    services = compose["services"]
    assert "openhop-radio-relay" in services
    assert "openhop-repeater-a" in services
    assert "openhop-repeater-b" in services
    # The Phase 3B bridge profile reuses the SAME two repeaters and the
    # same relay (one radio overlay, two nodes); the profiles are exclusive
    # at run time.
    for name in ("openhop-radio-relay", "openhop-repeater-a", "openhop-repeater-b"):
        assert set(services[name]["profiles"]) == {"openhop-radio", "openhop-bridge"}


def test_phase2_openhop_profile_is_untouched(compose):
    """The Phase 2 profile must keep its own services and shapes."""
    services = compose["services"]
    assert services["openhop-repeater"]["profiles"] == ["openhop"]
    assert services["medre-openhop"]["profiles"] == ["openhop"]
    # The Phase 2 repeater is still the null-radio rig.
    assert "openhop-repeater.yaml" not in str(services["openhop-radio-relay"])
    # The radio profile must not have crept into the Phase 2 services.
    assert "openhop-radio" not in services["openhop-repeater"]["profiles"]
    assert "openhop-radio" not in services["medre-openhop"]["profiles"]


def test_radio_profile_publishes_no_ports(compose):
    """Nothing of the radio rig is reachable from the host."""
    services = compose["services"]
    for name in ("openhop-radio-relay", "openhop-repeater-a", "openhop-repeater-b"):
        assert "ports" not in services[name], f"{name} must not publish ports"
    # The companion ports live inside the containers only.
    assert "5000" not in str(services["openhop-repeater-a"].get("ports", ""))


def test_radio_profile_has_no_hardware_capabilities(compose):
    services = compose["services"]
    for name in ("openhop-radio-relay", "openhop-repeater-a", "openhop-repeater-b"):
        service = services[name]
        assert "devices" not in service
        assert "cap_add" not in service
        assert "privileged" not in service
        for volume in service.get("volumes", []):
            if isinstance(volume, dict):
                assert "devices" not in volume
        for mount in service.get("volumes", []):
            if isinstance(mount, dict) and mount.get("target", "").startswith("/dev"):
                raise AssertionError(f"{name} mounts a device: {mount}")


def test_radio_healthchecks_are_process_liveness_only(compose):
    """No healthcheck may TCP-probe: the relay has exactly two client slots.

    A TCP probe would occupy one of the relay's two client slots (evicting a
    repeater's radio) or evict the companion's single client.
    """
    services = compose["services"]
    for name in ("openhop-radio-relay", "openhop-repeater-a", "openhop-repeater-b"):
        test = services[name]["healthcheck"]["test"]
        rendered = " ".join(test)
        assert "/proc/" in rendered, f"{name} healthcheck is not a /proc scan"
        assert "nc " not in rendered
        assert "curl" not in rendered
        assert "5055" not in rendered, f"{name} healthcheck probes the relay port"
        assert "5000" not in rendered, f"{name} healthcheck probes the companion port"
        assert "grep -qx" in rendered


def test_radio_network_is_private(compose):
    """The relay and both repeaters share one private network."""
    networks = compose["networks"]
    assert "openhop-radio" in networks
    services = compose["services"]
    for name in ("openhop-radio-relay", "openhop-repeater-a", "openhop-repeater-b"):
        assert services[name]["networks"] == ["openhop-radio"]
    # The Phase 2 network is a different one.
    assert services["openhop-repeater"]["networks"] == ["openhop"]


def test_radio_repeaters_have_separate_state(compose):
    """Each repeater owns its own identity/volume: they are two distinct nodes."""
    services = compose["services"]
    volumes_a = {
        v["source"]
        for v in services["openhop-repeater-a"]["volumes"]
        if isinstance(v, dict)
    }
    volumes_b = {
        v["source"]
        for v in services["openhop-repeater-b"]["volumes"]
        if isinstance(v, dict)
    }
    assert volumes_a != volumes_b
    # The companion identity key is generated per instance, never committed.
    assert (
        "__RADIO_COMPANION_IDENTITY_KEY__" in RADIO_CONFIG.read_text()
    ), "the committed template must carry the placeholder, not a key"


def test_radio_config_uses_the_modem_transport(radio_config):
    """The repeater's radio IS the lab relay, with LBT disabled."""
    assert radio_config["radio_type"] == "modem_tcp"
    modem = radio_config["modem_tcp"]
    assert modem["host"] == "openhop-radio-relay"
    assert modem["port"] == 5055
    assert modem["token"] == "", "the relay must need no secret"
    assert modem["lbt_enabled"] is False, "LBT must be disabled on a byte pipe"


def test_radio_config_is_monitor_mode_with_flooding_allowed(radio_config):
    """A repeat would echo the packet back; flooding must be allowed."""
    assert radio_config["repeater"]["mode"] == "monitor"
    assert radio_config["mesh"]["unscoped_flood_allow"] is True
    assert radio_config["mesh"]["global_flood_allow"] is True


def test_phase2_config_still_denies_transmission_and_flooding():
    """The Phase 2 profile keeps its null radio and its denied flooding."""
    phase2 = yaml.safe_load(
        (ROOT / "lab" / "config" / "openhop-repeater.yaml").read_text()
    )
    assert phase2["radio_type"] is None
    assert phase2["repeater"]["mode"] == "no_tx"
    assert phase2["mesh"]["unscoped_flood_allow"] is False


def test_relay_source_is_lab_owned_and_never_echoes():
    """The relay is lab code: no openHop radio backend is imported or patched."""
    source = RELAY_SOURCE.read_text()
    assert (
        "import openhop_core" not in source
    ), "the relay must not import openHop radio backends"
    assert "from openhop_core" not in source
    assert "SX1262" not in source
    assert "PROTO_SYNC = 0xAA" in source
    # The peer (never the sender) receives a transmission.
    assert "_peer_of" in source
    assert "never echoed back to the sender" in source
    assert "RECEIVED BY THE OTHER" in source


def test_relay_frames_match_the_upstream_wire_format():
    """SYNC | CMD | LEN (u16 LE) | PAYLOAD | CRC-16/CCITT-FALSE (u16 LE)."""
    source = RELAY_SOURCE.read_text()
    assert "PROTO_SYNC = 0xAA" in source
    assert 'struct.pack("<BH", cmd, length)' in source
    assert "crc16_ccitt" in source
    # The CRC covers CMD+LEN+PAYLOAD (the SYNC byte is excluded).
    assert "crc16_ccitt(header + payload)" in source


def test_relay_handles_the_commands_the_driver_sends():
    """Every command the openHop TCP radio driver issues must be answered."""
    source = RELAY_SOURCE.read_text()
    for name in (
        "CMD_PING",
        "CMD_AUTH",
        "CMD_SET_CONFIG",
        "CMD_GET_CONFIG",
        "CMD_RX_START",
        "CMD_TX_REQUEST",
        "CMD_STATUS_REQ",
        "CMD_NOISE_REQ",
        "CMD_CAD_REQUEST",
        "CMD_SET_CAD_PARAMS",
        "CMD_GET_VERSION",
    ):
        assert f"== {name}" in source, f"the relay does not handle {name}"
    for name in (
        "CMD_PONG",
        "CMD_CONFIG_RESP",
        "CMD_RX_STARTED",
        "CMD_TX_DONE",
        "CMD_RX_PACKET",
        "CMD_STATUS_RESP",
        "CMD_NOISE_RESP",
        "CMD_CAD_RESP",
        "CMD_CAD_PARAMS_RESP",
        "CMD_VERSION_RESP",
        "CMD_AUTH_OK",
        "CMD_ERROR",
    ):
        assert name in source, f"the relay never sends {name}"


def test_probe_and_proof_scripts_exist():
    assert (ROOT / "lab" / "tools" / "openhop_radio_probe.py").is_file()
    assert (ROOT / "lab" / "openhop-radio" / "packet_proof.sh").is_file()
    proof = (ROOT / "lab" / "openhop-radio" / "packet_proof.sh").read_text()
    assert "evidence_boundary" in proof
    assert "NOT physical RF" in proof


def test_packet_proof_refuses_when_a_medre_holds_companion_a():
    """The proof's subscriber must be companion A's only client.

    The openHop companion accepts ONE client at a time and evicts the
    previous one, so a proof run while a MEDRE runtime holds companion A
    would evict MEDRE, whose reconnect evicts the subscriber right back
    and drives the measured deliveries to zero. The proof must refuse
    before connecting, naming the container that holds the slot.
    """
    proof = (ROOT / "lab" / "openhop-radio" / "packet_proof.sh").read_text()
    for holder in (
        "medre-lab-medre-openhop-bridge",
        "medre-lab-medre-openhop-fault",
    ):
        assert holder in proof, f"the proof never checks for {holder}"
    assert "ONE client" in proof
    # The refusal must happen in the precondition phase, before any
    # probe container runs.
    assert proof.index("Phase 0: preconditions") < proof.index("Phase 1: packet proof")


def test_bring_up_exposes_the_radio_profile():
    """up.sh and the CLI must both offer the opt-in radio profile."""
    up = (ROOT / "lab" / "up.sh").read_text()
    assert "--openhop-radio" in up
    assert "openhop-radio" in up
    cli = (ROOT / "medre_lab" / "cli.py").read_text()
    assert '"openhop-radio"' in cli
    assert '"openhop-radio": "--openhop-radio"' in cli


def test_radio_profile_is_mutually_exclusive_with_the_other_rigs():
    """The relay holds two client slots: the radio rig cannot share a rig."""
    up = (ROOT / "lab" / "up.sh").read_text()
    assert re.search(
        r"WITH_OPENHOP_RADIO.*WITH_OPENHOP.*WITH_MEDRE.*WITH_NORMAL", up, re.S
    )
