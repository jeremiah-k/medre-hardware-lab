"""Tests for the last-bringup record and its up.sh wiring.

The record (lab/state/last-bringup.json) names the CHOICES of the last
completed bring-up -- topology, flags, and the sources.lock.json content
hash it was provisioned from -- and nothing generated. ``medre-lab preset
save`` consumes it, so its topology vocabulary must stay in step with the
preset schema's, and every up.sh exit path that leaves a rig UP must
record.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from medre_lab import presets

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "last_bringup", ROOT / "lab/last_bringup.py"
)
assert _spec and _spec.loader
last_bringup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(last_bringup)

UP_SH = (ROOT / "lab" / "up.sh").read_text(encoding="utf-8")


def write_lock(tmp_path: Path) -> None:
    (tmp_path / "sources.lock.json").write_text("{}\n", encoding="utf-8")


def test_record_captures_choices_not_generated_state(tmp_path: Path):
    write_lock(tmp_path)
    record = last_bringup.write_record(tmp_path, "base", ["--skip-build"])
    path = tmp_path / "lab/state/last-bringup.json"
    assert json.loads(path.read_text(encoding="utf-8")) == record
    assert set(record) == {
        "topology",
        "flags",
        "recorded_at",
        "sources_lock_sha256",
    }
    assert record["topology"] == "base"
    assert record["flags"] == ["--skip-build"]
    assert record["sources_lock_sha256"] == hashlib.sha256(b"{}\n").hexdigest()
    assert record["recorded_at"].endswith("Z")


def test_record_refuses_an_unknown_topology_or_missing_lock(tmp_path: Path):
    write_lock(tmp_path)
    with pytest.raises(ValueError, match="topology"):
        last_bringup.write_record(tmp_path, "threenode", [])
    with pytest.raises(ValueError, match="sources.lock.json"):
        last_bringup.write_record(tmp_path / "nowhere", "base", [])


def test_cli_writes_the_record_into_a_lab_root(tmp_path: Path, capsys):
    write_lock(tmp_path)
    code = last_bringup.main(
        [
            "--topology",
            "normal",
            "--flag=--normal",
            "--flag=--skip-smoke",
            "--lab-root",
            str(tmp_path),
        ]
    )
    assert code == 0
    record = json.loads(
        (tmp_path / "lab/state/last-bringup.json").read_text(encoding="utf-8")
    )
    assert record["topology"] == "normal"
    assert record["flags"] == ["--normal", "--skip-smoke"]
    assert json.loads(capsys.readouterr().out) == record


def test_record_rewrites_in_place(tmp_path: Path):
    write_lock(tmp_path)
    first = last_bringup.write_record(tmp_path, "base", [])
    second = last_bringup.write_record(tmp_path, "openhop", ["--openhop"])
    path = tmp_path / "lab/state/last-bringup.json"
    assert json.loads(path.read_text(encoding="utf-8")) == second
    assert second["topology"] == "openhop" and first["topology"] == "base"


def test_topology_vocabularies_stay_in_step():
    assert last_bringup.TOPOLOGIES == presets.TOPOLOGIES


def test_up_sh_records_on_every_path_that_leaves_a_rig_up():
    """The two openHop profiles exit early, so all three paths must call
    the recorder; the plain path records after the version record, before
    the final banner."""
    calls = UP_SH.count("write_last_bringup_record")
    # one definition + three call sites (plain, openhop, openhop-radio)
    assert calls == 4
    assert "python3 lab/last_bringup.py" in UP_SH
    plain_call = UP_SH.index("write_last_bringup_record", UP_SH.index("lab/VERSION"))
    assert UP_SH.index('log "Interop lab is UP"') > plain_call


def test_up_sh_collects_the_flag_set(tmp_path: Path):
    """Every accepted flag lands in FLAGS, so the record can name them."""
    assert "FLAGS=()" in UP_SH
    assert 'FLAGS+=("${arg}")' in UP_SH
    result = subprocess.run(
        [
            "bash",
            "-c",
            'FLAGS=(); for arg in "$@"; do FLAGS+=("${arg}"); done; '
            'printf "%s\\n" "${FLAGS[@]}"',
            "up",
            "--normal",
            "--skip-build",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == ["--normal", "--skip-build"]
