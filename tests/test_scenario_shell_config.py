"""Scenario defaults are Bash expressions, not dotenv substitution syntax."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("override", [None, "owned-peer.example"])
def test_scenario_shell_config_preserves_defaults_and_overrides(override):
    env = {"PATH": os.defpath}
    if override is not None:
        env["NODE_A_HOST"] = override
    result = subprocess.run(
        [
            "bash",
            "-euc",
            'LAB_ROOT=$1; source "$2"; printf "%s\\n" "$NODE_A_HOST" "$NODE_A_PORT" "$LAB_COMPOSE_FILE"',
            "scenario-config",
            str(ROOT),
            str(ROOT / "scenarios/config.env"),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        override or "meshtasticd-a",
        "4403",
        str(ROOT / "compose.yaml"),
    ]



def test_deferred_scenario_is_recognized_without_running_it(tmp_path):
    """Formatting must not change the associative-array scenario key."""
    runner = tmp_path / "lab/run-scenarios.sh"
    runner.parent.mkdir()
    runner.write_bytes((ROOT / "lab/run-scenarios.sh").read_bytes())
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    unexpected = scenarios / "scenario-threenode-same-mesh.sh"
    unexpected.write_text("#!/bin/bash\necho unexpected-execution\nexit 1\n")
    unexpected.chmod(0o755)
    result = subprocess.run(
        ["bash", str(runner), "threenode-same-mesh"],
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert "(deferred)" in result.stdout
    assert "SKIP: DEFERRED by owner" in result.stdout
    assert "0 failed / 1 scenario(s) executed" in result.stdout
    assert "unexpected-execution" not in result.stdout
    assert not list((tmp_path / "lab/logs/scenarios").glob("*.log"))


def test_unknown_scenario_remains_a_failure(tmp_path):
    runner = tmp_path / "lab/run-scenarios.sh"
    runner.parent.mkdir()
    runner.write_bytes((ROOT / "lab/run-scenarios.sh").read_bytes())
    result = subprocess.run(
        ["bash", str(runner), "not-a-scenario"],
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert "ERROR: unknown scenario 'not-a-scenario'" in result.stdout
