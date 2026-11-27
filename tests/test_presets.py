"""Tests for the preset CLI actions: save, list, show (load has its own).

save records the LIVE configuration -- the lock's pins, the topology the
last bring-up actually ran (from lab/state/last-bringup.json), and the
suite that topology runs by default -- and refuses anything that would
record a pairing that never ran. The fixtures are temporary lab roots:
a real lock, a stub compose.yaml, a stub runner, and the real
qualification driver copied in (its stage list is derived, never
hardcoded).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from medre_lab import presets
from medre_lab.cli import main

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "last_bringup", ROOT / "lab/last_bringup.py"
)
assert _spec and _spec.loader
last_bringup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(last_bringup)


def build_lab_root(tmp_path: Path) -> Path:
    root = tmp_path / "labroot"
    (root / "lab/state").mkdir(parents=True)
    (root / "lab/presets").mkdir(parents=True)
    (root / "lab/tools").mkdir()
    (root / "sources.lock.json").write_bytes((ROOT / "sources.lock.json").read_bytes())
    (root / "compose.yaml").write_text("")
    (root / "lab/run-scenarios.sh").write_text("")
    for name in ("openhop_qualify.py", "openhop_config_access.py"):
        shutil.copyfile(ROOT / "lab/tools" / name, root / "lab/tools" / name)
    return root


def record_bringup(root: Path, topology: str, flags: list[str]) -> None:
    last_bringup.write_record(root, topology, flags)


def test_save_records_the_live_configuration(tmp_path: Path):
    root = build_lab_root(tmp_path)
    record_bringup(root, "base", ["--skip-smoke"])
    assert presets.save_preset(root, "mybase", None, False) == 0
    saved = json.loads((root / "lab/presets/mybase.json").read_text(encoding="utf-8"))
    assert saved["name"] == "mybase"
    assert saved["topology"] == "base"
    assert saved["suite"] == ["scenarios"]
    assert saved["sources"] == json.loads(
        (ROOT / "sources.lock.json").read_text(encoding="utf-8")
    )
    assert saved["description"]


def test_save_detects_the_driver_suite_for_openhop_topologies(tmp_path: Path):
    root = build_lab_root(tmp_path)
    record_bringup(root, "openhop", ["--openhop"])
    assert presets.save_preset(root, "qual-cycle", "a qualification pass", False) == 0
    saved = presets.read_preset(root, "qual-cycle")
    assert saved["suite"] == [
        f"qual:{stage}" for stage in presets.qualify_stage_names(root)
    ]
    assert saved["description"] == "a qualification pass"


def test_save_refuses_to_overwrite_without_force(tmp_path: Path):
    root = build_lab_root(tmp_path)
    record_bringup(root, "base", [])
    assert presets.save_preset(root, "mybase", None, False) == 0
    with pytest.raises(ValueError, match="already exists"):
        presets.save_preset(root, "mybase", None, False)
    assert presets.save_preset(root, "mybase", "revised", True) == 0
    assert presets.read_preset(root, "mybase")["description"] == "revised"


def test_save_refuses_a_bringup_of_different_lock_content(tmp_path: Path):
    root = build_lab_root(tmp_path)
    record_bringup(root, "base", [])
    # The lock moves AFTER the bring-up was recorded.
    data = json.loads((root / "sources.lock.json").read_text(encoding="utf-8"))
    data["medre"]["ref"] = "b" * 40
    (root / "sources.lock.json").write_text(json.dumps(data, indent=2) + "\n")
    with pytest.raises(ValueError, match="different sources.lock.json"):
        presets.save_preset(root, "moved", None, False)
    assert not (root / "lab/presets/moved.json").exists()


def test_save_requires_the_last_bringup_record(tmp_path: Path):
    root = build_lab_root(tmp_path)
    with pytest.raises(ValueError, match="no last-bringup record"):
        presets.save_preset(root, "mybase", None, False)


def test_save_requires_a_safe_name(tmp_path: Path):
    root = build_lab_root(tmp_path)
    record_bringup(root, "base", [])
    for name in ("../escape", "Big", "with_underscore", ""):
        with pytest.raises(ValueError, match="preset name"):
            presets.save_preset(root, name, None, False)


def test_list_reports_each_preset_and_its_pins_match(tmp_path: Path, capsys):
    root = build_lab_root(tmp_path)
    record_bringup(root, "base", [])
    presets.save_preset(root, "mybase", None, False)
    capsys.readouterr()
    assert presets.list_presets(root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry["name"] for entry in payload["presets"]] == ["mybase"]
    entry = payload["presets"][0]
    assert entry["pins_match_lock"] is True
    assert entry["topology"] == "base"
    assert entry["suite"] == ["scenarios"]


def test_list_refuses_an_empty_library(tmp_path: Path):
    root = build_lab_root(tmp_path)
    with pytest.raises(ValueError, match="empty"):
        presets.list_presets(root)


def test_show_prints_the_committed_choices(tmp_path: Path, capsys):
    root = build_lab_root(tmp_path)
    record_bringup(root, "base", [])
    presets.save_preset(root, "mybase", "kept for reuse", False)
    capsys.readouterr()
    assert presets.show_preset(root, "mybase") == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["description"] == "kept for reuse"
    with pytest.raises(ValueError, match="no preset named"):
        presets.show_preset(root, "absent")


def test_cli_wiring_save_and_list(tmp_path: Path):
    root = build_lab_root(tmp_path)
    record_bringup(root, "base", [])
    save = subprocess.run(
        [
            sys.executable,
            "-m",
            "medre_lab.cli",
            "--root",
            str(root),
            "preset",
            "save",
            "clibase",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    payload = json.loads(save.stdout)
    assert payload["topology"] == "base"
    listing = subprocess.run(
        [sys.executable, "-m", "medre_lab.cli", "--root", str(root), "preset", "list"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    entries = json.loads(listing.stdout)["presets"]
    assert [entry["name"] for entry in entries] == ["clibase"]


def test_cli_reports_refusals_as_failures(tmp_path: Path, capsys):
    root = build_lab_root(tmp_path)
    assert main(["--root", str(root), "preset", "save", "mybase"]) == 2
    assert "no last-bringup record" in capsys.readouterr().err
