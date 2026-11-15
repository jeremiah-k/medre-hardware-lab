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
