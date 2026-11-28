"""Tests for the preset CLI actions: save, list, show and load.

save records the LIVE configuration -- the lock's pins, the topology the
last bring-up actually ran (from lab/state/last-bringup.json), and the
suite that topology runs by default -- and refuses anything that would
record a pairing that never ran. load switches the lock to a preset's
pins and verifies every checkout HEAD against them. The fixtures are
temporary lab roots: a real lock, a stub compose.yaml, a stub runner, and
the real qualification driver copied in (its stage list is derived, never
hardcoded). The load tests use fabricated local source repositories whose
HEADs ARE the pins, with a fetch stub that mirrors rig.fetch's clone
behavior minus the network.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from medre_lab import docker as rig
from medre_lab import presets
from medre_lab.cli import main

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "last_bringup", ROOT / "lab/last_bringup.py"
)
assert _spec and _spec.loader
last_bringup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(last_bringup)

GIT_IDENTITY = ("-c", "user.name=Lab Test", "-c", "user.email=lab@example.invalid")


def build_lab_root(tmp_path: Path) -> Path:
    root = tmp_path / "labroot"
    (root / "lab/state").mkdir(parents=True)
    (root / "lab/presets").mkdir(parents=True)
    (root / "lab/tools").mkdir()
    (root / "lab/config").mkdir()
    (root / "sources.lock.json").write_bytes((ROOT / "sources.lock.json").read_bytes())
    (root / "compose.yaml").write_text("")
    (root / "lab/run-scenarios.sh").write_text("")
    for name in ("openhop_qualify.py", "openhop_config_access.py"):
        shutil.copyfile(ROOT / "lab/tools" / name, root / "lab/tools" / name)
    shutil.copyfile(
        ROOT / "lab/check_source_mounts.py", root / "lab/check_source_mounts.py"
    )
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


# --- load ----------------------------------------------------------------------


def run_git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def make_source_repo(path: Path) -> str:
    """A tiny checkout whose module files satisfy lab/check_source_mounts."""
    path.mkdir(parents=True)
    run_git(path, "init", "-q", "-b", "main")
    for key, info in rig.SOURCES.items():
        file = path / info[1]
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f"# {key}\n", encoding="utf-8")
    run_git(path, "add", "-A")
    run_git(path, "commit", "-q", "-m", "module files")
    return run_git(path, "rev-parse", "HEAD").strip()


def make_fake_pins(repos: Path) -> dict:
    """Ten pins whose SHAs are real commits of local repositories."""
    pins = {}
    for key in rig.SOURCES:
        head = make_source_repo(repos / key)
        pins[key] = {"url": f"https://github.com/fake/{key}.git", "ref": head}
    return pins


def test_prepare_binds_manifests_and_environment_to_one_lock(tmp_path):
    root = build_lab_root(tmp_path)
    expected_pins = make_fake_pins(root / "sources")
    raw = json.dumps(expected_pins).encode()
    (root / "sources.lock.json").write_bytes(raw)
    env = rig.prepare(root)

    assert env["LAB_SOURCE_LOCK_SHA256"] == hashlib.sha256(raw).hexdigest()
    manifest = json.loads(Path(env["LAB_SOURCE_MANIFEST_OPENHOP"]).read_text())
    for entries in manifest.values():
        for entry in entries:
            key = Path(entry["source"]).name
            assert entry["ref"] == expected_pins[key]["ref"]


def write_preset(root: Path, name: str, topology: str, pins: dict, suite: list) -> None:
    data = {
        "name": name,
        "description": f"the {name} test preset",
        "topology": topology,
        "sources": pins,
        "suite": suite,
    }
    (root / "lab/presets" / f"{name}.json").write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


def stub_fetch(root: Path, repos: Path):
    """rig.fetch minus the network: clone the pinned commits locally."""

    def fetch(target_root, pins):
        for name, entry in pins.items():
            checkout = Path(target_root) / "sources" / name
            if checkout.exists():
                continue  # existing checkouts are never reset
            subprocess.check_call(
                ["git", "clone", "-q", str(repos / name), str(checkout)]
            )
            subprocess.check_call(
                ["git", "-C", str(checkout), "checkout", "-q", "--detach", entry["ref"]]
            )

    return fetch


def test_verify_checkouts_accepts_pins_and_names_every_mismatch(tmp_path: Path):
    repos = tmp_path / "repos"
    pins = make_fake_pins(repos)
    checkout = tmp_path / "sources" / "medre"
    subprocess.check_call(["git", "clone", "-q", str(repos / "medre"), str(checkout)])
    good = dict(pins)
    good["medre"] = {
        "url": pins["medre"]["url"],
        "ref": run_git(checkout, "rev-parse", "HEAD"),
    }
    presets.verify_checkouts(tmp_path, {"medre": good["medre"]})  # no raise
    bad = dict(pins)
    bad["medre"] = {"url": pins["medre"]["url"], "ref": "a" * 40}
    bad["mmrelay"] = {"url": pins["mmrelay"]["url"], "ref": "b" * 40}
    with pytest.raises(ValueError, match="differs from pin") as excinfo:
        presets.verify_checkouts(tmp_path, bad)
    assert "medre" in str(excinfo.value) and "mmrelay" in str(excinfo.value)
    with pytest.raises(ValueError, match="missing"):
        presets.verify_checkouts(tmp_path, {k: pins[k] for k in ("openhop-core",)})


def test_load_refuses_unrecorded_pins_and_proceeds_through_membership(
    tmp_path, monkeypatch, capsys
):
    repos = tmp_path / "repos"
    fake_pins = make_fake_pins(repos)
    root = build_lab_root(tmp_path)
    write_preset(root, "fake", "base", fake_pins, ["scenarios"])
    # The live lock (the real pins) matches no saved preset -> refused.
    with pytest.raises(ValueError, match="matches no saved preset"):
        presets.load_preset(root, "fake")
    assert json.loads((root / "sources.lock.json").read_text())["medre"]["ref"] == (
        json.loads((ROOT / "sources.lock.json").read_text())["medre"]["ref"]
    )
    # Recording the live configuration makes the switch legitimate.
    record_bringup(root, "base", [])
    assert presets.save_preset(root, "live-state", None, False) == 0
    monkeypatch.setattr(presets.rig, "fetch_checkouts", stub_fetch(root, repos))
    assert presets.load_preset(root, "fake") == 0
    assert json.loads((root / "sources.lock.json").read_text()) == fake_pins
    for key in rig.SOURCES:
        head = subprocess.check_output(
            ["git", "-C", str(root / "sources" / key), "rev-parse", "HEAD"], text=True
        ).strip()
        assert head == fake_pins[key]["ref"]
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["lock_changed"] is True and summary["verified_pins"] == 10


def test_load_is_idempotent_when_pins_already_match(tmp_path, monkeypatch, capsys):
    repos = tmp_path / "repos"
    fake_pins = make_fake_pins(repos)
    root = build_lab_root(tmp_path)
    write_preset(root, "fake", "base", fake_pins, ["scenarios"])
    (root / "sources.lock.json").write_text(json.dumps(fake_pins, indent=2) + "\n")
    record_bringup(root, "base", [])
    presets.save_preset(root, "same-pins", None, False)
    monkeypatch.setattr(presets.rig, "fetch_checkouts", stub_fetch(root, repos))
    assert presets.load_preset(root, "fake") == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["lock_changed"] is False
    assert (root / "sources.lock.json").read_text() == json.dumps(
        fake_pins, indent=2
    ) + "\n"


def test_load_force_discards_unrecorded_pins(tmp_path, monkeypatch):
    repos = tmp_path / "repos"
    fake_pins = make_fake_pins(repos)
    root = build_lab_root(tmp_path)
    write_preset(root, "fake", "base", fake_pins, ["scenarios"])
    monkeypatch.setattr(presets.rig, "fetch_checkouts", stub_fetch(root, repos))
    assert presets.load_preset(root, "fake", force=True) == 0
    assert json.loads((root / "sources.lock.json").read_text()) == fake_pins


def test_bring_up_runs_the_existing_docker_up_path(tmp_path, monkeypatch):
    root = build_lab_root(tmp_path)
    commands = []
    monkeypatch.setattr(presets.rig, "assert_no_other_rig", lambda target: None)
    monkeypatch.setattr(
        presets.rig, "prepare", lambda target: {"LAB_ROOT": str(target)}
    )
    monkeypatch.setattr(
        presets.subprocess, "call", lambda argv, **kw: commands.append(argv) or 0
    )
    assert presets.bring_up(root, "base") == 0
    assert presets.bring_up(root, "openhop") == 0
    assert presets.bring_up(root, "openhop-radio") == 0
    assert commands == [
        ["bash", "lab/up.sh"],
        ["bash", "lab/up.sh", "--openhop"],
        ["bash", "lab/up.sh", "--openhop-radio"],
    ]


def test_cli_docker_up_uses_the_same_topology_map(tmp_path, monkeypatch):
    root = build_lab_root(tmp_path)
    commands = []
    monkeypatch.setattr(rig, "assert_no_other_rig", lambda target: None)
    monkeypatch.setattr(rig, "prepare", lambda target: {"LAB_ROOT": str(target)})
    monkeypatch.setattr(
        "medre_lab.cli.subprocess.call", lambda argv, **kw: commands.append(argv) or 0
    )
    assert main(["--root", str(root), "docker", "up", "--profile", "base"]) == 0
    assert main(["--root", str(root), "docker", "up", "--profile", "stress"]) == 0
    assert commands == [["bash", "lab/up.sh"], ["bash", "lab/up.sh", "--with-medre"]]


def test_cli_load_wiring_refuses_and_loads(tmp_path, monkeypatch, capsys):
    repos = tmp_path / "repos"
    fake_pins = make_fake_pins(repos)
    root = build_lab_root(tmp_path)
    write_preset(root, "fake", "base", fake_pins, ["scenarios"])
    assert main(["--root", str(root), "preset", "load", "fake"]) == 2
    assert "matches no saved preset" in capsys.readouterr().err
    record_bringup(root, "base", [])
    assert presets.save_preset(root, "live-state", None, False) == 0
    monkeypatch.setattr(presets.rig, "fetch_checkouts", stub_fetch(root, repos))
    assert main(["--root", str(root), "preset", "load", "fake"]) == 0
    capsys.readouterr()
    assert json.loads((root / "sources.lock.json").read_text()) == fake_pins


def test_rejected_preset_switch_preserves_previous_lock(tmp_path, monkeypatch):
    repos = tmp_path / "repos"
    fake_pins = make_fake_pins(repos)
    root = build_lab_root(tmp_path)
    original = (root / "sources.lock.json").read_bytes()
    write_preset(root, "fake", "base", fake_pins, ["scenarios"])
    monkeypatch.setattr(presets.rig, "fetch_checkouts", lambda *_: None)
    monkeypatch.setattr(presets.rig, "prepare", lambda *_: pytest.fail("prepare ran"))
    with pytest.raises(ValueError, match="checkout verification failed"):
        presets.load_preset(root, "fake", force=True)
    assert (root / "sources.lock.json").read_bytes() == original


def test_failed_prepare_restores_selection_files(tmp_path, monkeypatch):
    repos = tmp_path / "repos"
    fake_pins = make_fake_pins(repos)
    root = build_lab_root(tmp_path)
    original = (root / "sources.lock.json").read_bytes()
    write_preset(root, "fake", "base", fake_pins, ["scenarios"])
    env = root / ".env"
    env.write_text("original\n")
    monkeypatch.setattr(presets.rig, "fetch_checkouts", stub_fetch(root, repos))

    def broken_prepare(target):
        (target / ".env").write_text("changed\n")
        raise OSError("prepare failed")

    monkeypatch.setattr(presets.rig, "prepare", broken_prepare)
    with pytest.raises(OSError, match="prepare failed"):
        presets.load_preset(root, "fake", force=True)
    assert (root / "sources.lock.json").read_bytes() == original
    assert env.read_text() == "original\n"
