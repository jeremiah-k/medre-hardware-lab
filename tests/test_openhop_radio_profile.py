"""Offline checks for the opt-in openHop VIRTUAL RADIO profile.

Topology and wiring assertions read the committed Compose, config
and script files. The packet proof uses a Docker command stub.
"""

from __future__ import annotations

import os
import re
import subprocess
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

    def state_volume(service: str) -> str:
        sources = []
        for mount in services[service]["volumes"]:
            if isinstance(mount, str):
                source, target, *_ = mount.split(":")
            else:
                source, target = mount["source"], mount["target"]
            if target == "/var/lib/openhop_repeater":
                sources.append(source)
        assert len(sources) == 1
        assert sources[0] in compose["volumes"]
        return sources[0]

    assert state_volume("openhop-repeater-a") != state_volume("openhop-repeater-b")
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


@pytest.mark.parametrize(
    "holder",
    ("medre-lab-medre-openhop-bridge", "medre-lab-medre-openhop-fault"),
)
def test_packet_proof_refuses_when_a_medre_holds_companion_a(tmp_path, holder):
    """Refuse to evict either MEDRE client before the proof launches its probe."""
    docker = tmp_path / "docker"
    calls = tmp_path / "calls"
    docker.write_text("""#!/bin/sh
printf '%s\n' "$*" >> "$MEDRE_TEST_DOCKER_CALLS"
[ "$1" = inspect ] || exit 9
case "$4" in
    "$MEDRE_TEST_SLOT_HOLDER"|medre-lab-openhop-*) echo true ;;
    *) echo false ;;
esac
""")
    docker.chmod(0o700)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "lab" / "openhop-radio" / "packet_proof.sh"),
            "--evidence-dir",
            str(tmp_path / "evidence"),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
            "MEDRE_TEST_DOCKER_CALLS": str(calls),
            "MEDRE_TEST_SLOT_HOLDER": holder,
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert f"slot is held by the running container {holder}" in result.stderr
    assert all(call.startswith("inspect ") for call in calls.read_text().splitlines())


def test_bring_up_exposes_the_radio_profile():
    """up.sh and the CLI must both offer the opt-in radio profile."""
    up = (ROOT / "lab" / "up.sh").read_text()
    assert "--openhop-radio" in up
    assert "openhop-radio" in up
    cli = (ROOT / "medre_lab" / "cli.py").read_text()
    assert '"openhop-radio"' in cli
    # The topology-to-flag map lives ONCE, in medre_lab.presets.UP_FLAGS,
    # shared by docker up and preset load.
    assert "UP_FLAGS" in cli
    from medre_lab.presets import UP_FLAGS

    assert UP_FLAGS["openhop-radio"] == ("--openhop-radio",)


def test_radio_profile_is_mutually_exclusive_with_the_other_rigs():
    """The relay holds two client slots: the radio rig cannot share a rig."""
    up = (ROOT / "lab" / "up.sh").read_text()
    assert re.search(
        r"WITH_OPENHOP_RADIO.*WITH_OPENHOP.*WITH_MEDRE.*WITH_NORMAL", up, re.S
    )
