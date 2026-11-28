"""Offline checks for the opt-in openHop Docker profile.

These are topology and wiring assertions only: they read the committed
compose/up/CLI files and never contact Docker, a radio or a network.
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


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text())


def _services(compose: dict) -> dict:
    return compose["services"]


def test_openhop_profile_exists(compose):
    services = _services(compose)
    assert "openhop-repeater" in services
    assert "medre-openhop" in services
    assert services["openhop-repeater"]["profiles"] == ["openhop"]
    assert services["medre-openhop"]["profiles"] == ["openhop"]


def test_default_profiles_are_untouched(compose):
    """The openHop work must not move any default-profile service."""
    services = _services(compose)
    assert services["medre-relay"]["profiles"] == ["medre"]
    assert services["medre-normal"]["profiles"] == ["normal"]
    assert services["meshtasticd-a-medre"]["profiles"] == ["medre"]
    # meshtasticd-c-medre gains the openhop profile so medre-openhop has a
    # sim endpoint, and the openhop-bridge profile so the Phase 3B bridge has
    # one too; `normal` keeps it as well.
    assert set(services["meshtasticd-c-medre"]["profiles"]) == {
        "normal",
        "openhop",
        "openhop-bridge",
    }


def test_openhop_repeater_publishes_no_ports(compose):
    """The companion TCP port must never be published to the host."""
    services = _services(compose)
    assert "ports" not in services["openhop-repeater"]
    assert "ports" not in services["medre-openhop"]


def test_openhop_services_have_no_hardware_capabilities(compose):
    services = _services(compose)
    for name in ("openhop-repeater", "medre-openhop"):
        service = services[name]
        assert "devices" not in service
        assert "cap_add" not in service
        assert "privileged" not in service
        for volume in service.get("volumes", []):
            if isinstance(volume, dict):
                assert "devices" not in volume


def test_openhop_healthchecks_are_process_liveness_only(compose):
    """No healthcheck may TCP-probe the companion: it evicts the client."""
    services = _services(compose)
    for name in ("openhop-repeater", "medre-openhop"):
        test = services[name]["healthcheck"]["test"]
        rendered = " ".join(test)
        assert "/proc/" in rendered, f"{name} healthcheck is not a /proc scan"
        assert "nc " not in rendered
        assert "curl" not in rendered
        # The exact-line match (`grep -qx`) keeps the healthcheck's own
        # cmdline from matching its own pattern.
        assert "grep -qx" in rendered


def test_companion_tcp_port_is_never_referenced_by_a_probe(compose):
    """No compose healthcheck may touch the companion port.

    A TCP probe would connect as a second client and EVICT the standing
    one (MEDRE's own session), so the port must appear nowhere in a
    healthcheck.
    """
    services = _services(compose)
    for name, service in services.items():
        healthcheck = service.get("healthcheck")
        if not healthcheck:
            continue
        rendered = " ".join(healthcheck["test"])
        assert "5000" not in rendered, f"{name} healthcheck references port 5000"


def test_openhop_network_is_private(compose):
    networks = compose["networks"]
    assert "openhop" in networks
    # The companion lives on this network and nowhere else.
    services = _services(compose)
    assert services["openhop-repeater"]["networks"] == ["openhop"]
    assert set(services["medre-openhop"]["networks"]) == {"openhop", "mesh-c"}


def test_openhop_sources_are_mounted_read_only(compose):
    services = _services(compose)
    expected = {
        "openhop-repeater": {"/src/openhop-core", "/src/openhop-repeater"},
        "medre-openhop": {"/src/core", "/src/meshcore", "/src/meshtastic"},
    }
    for name, targets in expected.items():
        mounts = {
            volume["target"]: volume
            for volume in services[name]["volumes"]
            if isinstance(volume, dict)
        }
        for target in targets:
            assert target in mounts, f"{name} does not mount {target}"
            assert mounts[target]["read_only"] is True


def test_openhop_services_have_persistent_volumes(compose):
    services = _services(compose)
    volumes = compose["volumes"]
    assert "openhop-repeater-data" in volumes
    assert "medre-openhop-state" in volumes
    # The repeater's state (SQLite companion store + identity file) lives on
    # a named volume, so it survives `docker compose down`.
    repeater_volumes = services["openhop-repeater"]["volumes"]
    assert any(
        isinstance(volume, str) and volume.startswith("openhop-repeater-data:")
        for volume in repeater_volumes
    )


def test_openhop_configs_have_no_credentials_in_git():
    """Committed config templates carry placeholders, never secrets."""
    repeater = (ROOT / "lab/config/openhop-repeater.yaml").read_text()
    medre = (ROOT / "lab/config/medre-openhop.yaml").read_text()
    for text in (repeater, medre):
        # Placeholders are expected; literal secrets are not.
        assert "__OPENHOP_" in text or text == medre
    assert "__OPENHOP_COMPANION_IDENTITY_KEY__" in repeater
    assert "__OPENHOP_ADMIN_PASSWORD__" in repeater
    assert "__OPENHOP_GUEST_PASSWORD__" in repeater
    assert "__OPENHOP_JWT_SECRET__" in repeater
    # No long hex literal that could be a real identity key.
    assert not re.search(r"identity_key:\s*\"[0-9a-f]{64}\"", repeater)


def test_openhop_routes_are_disabled_by_default():
    config = yaml.safe_load((ROOT / "lab/config/medre-openhop.yaml").read_text())
    routes = config.get("routes") or {}
    assert routes, "the route shape must stay documented"
    for route in routes.values():
        assert route["enabled"] is False


def test_openhop_repeater_config_disables_radio():
    config = yaml.safe_load((ROOT / "lab/config/openhop-repeater.yaml").read_text())
    assert config["radio_type"] is None
    # Exactly one companion: the companion server accepts one client only.
    companions = config["identities"]["companions"]
    assert len(companions) == 1
    assert companions[0]["settings"]["tcp_port"] == 5000


def test_up_sh_rejects_conflicting_profiles():
    """--openhop is mutually exclusive with the other MEDRE topologies."""
    result = subprocess.run(
        ["bash", str(ROOT / "lab/up.sh"), "--openhop", "--normal"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "mutually exclusive" in result.stderr


def test_up_sh_rejects_openhop_with_stress():
    result = subprocess.run(
        ["bash", str(ROOT / "lab/up.sh"), "--openhop", "--with-medre"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "mutually exclusive" in result.stderr


def test_cli_exposes_the_openhop_profile():
    result = subprocess.run(
        [
            "python3",
            "-m",
            "medre_lab",
            "docker",
            "up",
            "--help",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
        check=True,
    )
    assert "openhop" in result.stdout


def test_scenario_is_not_in_all_scenarios():
    """The openHop scenario is opt-in: it is never in ALL_SCENARIOS."""
    runner = (ROOT / "lab/run-scenarios.sh").read_text()
    all_scenarios = runner.split("ALL_SCENARIOS=(", 1)[1].split(")", 1)[0]
    assert "openhop" not in all_scenarios


def test_scenario_skips_when_the_profile_is_absent(tmp_path):
    """The scenario SKIPs (exit 2) when the openHop profile is not up.

    The check is driven by a compose file that has no openHop services, so
    the assertion does not depend on whether the ambient rig happens to be
    running.
    """
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text(
        "name: medre-lab-empty\n"
        "services:\n"
        "  unrelated:\n"
        "    image: busybox\n"
        "    command: ['true']\n"
    )
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nprintf 'unrelated\\n'\n")
    docker.chmod(0o755)
    scenario = ROOT / "scenarios/scenario-openhop-qualifier.sh"
    assert scenario.exists()
    result = subprocess.run(
        ["bash", str(scenario)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        check=False,
        env={**os.environ, "LAB_COMPOSE_FILE": str(compose_file),
             "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "SKIP" in (result.stdout + result.stderr)
