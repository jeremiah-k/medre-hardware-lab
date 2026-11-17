"""Exercise the same override installation script used by the lab container."""

import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERRIDES = ROOT / "lab/config/synapse-overrides.yaml"
SCRIPT = ROOT / "lab/tools/apply-synapse-overrides.sh"


@pytest.mark.parametrize("tail", ["", "legacy", "duplicate", "changed", "current"])
def test_synapse_overrides_are_idempotent(tmp_path, tail):
    prefix = "server_name: lab.test\n"
    block = OVERRIDES.read_text()
    legacy = block.replace(
        "Hardware-lab Synapse overrides", "interop-lab Synapse overrides"
    )
    tails = {
        "": "",
        "legacy": legacy,
        "duplicate": legacy + block + block,
        "changed": block.replace("burst_count: 100", "burst_count: 99"),
        "current": block,
    }
    config = tmp_path / "homeserver.yaml"
    config.write_text(prefix + tails[tail])
    config.chmod(0o640)
    command = ["sh", str(SCRIPT), str(config), str(OVERRIDES)]
    result = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=10
    )
    assert config.read_text() == prefix + block
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert yaml.safe_load(config.read_text())["rc_message"]["burst_count"] == 100
    assert ("overrides reapplied" in result.stdout) is (tail != "current")
    before = config.stat()
    second = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=10
    )
    assert second.stdout == ""
    assert config.stat().st_ino == before.st_ino
    assert config.read_text() == prefix + block
    assert list(tmp_path.iterdir()) == [config]


@pytest.mark.parametrize("source", ["", "unmarked", "legacy", "duplicate", "header-only"])
def test_invalid_override_source_preserves_config(tmp_path, source):
    block = OVERRIDES.read_text()
    invalid = {
        "": "",
        "unmarked": "rc_message: {}\n",
        "legacy": block.replace("Hardware-lab", "interop-lab"),
        "duplicate": block + block,
        "header-only": block.splitlines()[0] + "\n",
    }[source]
    config = tmp_path / "homeserver.yaml"
    old = "server_name: lab.test\n" + block
    config.write_text(old)
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(invalid)
    inode = config.stat().st_ino
    result = subprocess.run(
        ["sh", str(SCRIPT), str(config), str(overrides)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "expected one current" in result.stderr
    assert config.read_text() == old
    assert config.stat().st_ino == inode
    assert set(tmp_path.iterdir()) == {config, overrides}


def test_override_refuses_symlink(tmp_path):
    target = tmp_path / "actual.yaml"
    target.write_text("server_name: lab.test\n")
    config = tmp_path / "homeserver.yaml"
    config.symlink_to(target)
    result = subprocess.run(
        ["sh", str(SCRIPT), str(config), str(OVERRIDES)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert config.is_symlink()
    assert target.read_text() == "server_name: lab.test\n"
