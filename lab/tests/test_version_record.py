"""Unit tests for the lab version record maintenance.

The record (lab/VERSION) used to be APPENDED to, so it accumulated
duplicate keys: an old section with old pins sat next to the current one
and a reader could not tell which rig was actually running. These tests
pin the replacement semantics: one section per rig, and no stale copy
left behind.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECORD_PATH = ROOT / "lab" / "version_record.py"

_spec = importlib.util.spec_from_file_location("version_record", RECORD_PATH)
assert _spec and _spec.loader
version_record = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(version_record)


@pytest.fixture()
def record(tmp_path: Path) -> Path:
    path = tmp_path / "VERSION"
    path.write_text(
        "# hardware-lab version record\n"
        "recorded_at: 2026-11-24T10:00:00Z\n"
        "openhop_profile:\n"
        "  openhop_core: /src/openhop-core @ e07722ea\n",
        encoding="utf-8",
    )
    return path


def test_set_replaces_the_section(record: Path):
    version_record.set_section(
        record,
        "openhop_profile",
        ["  openhop_core: /src/openhop-core @ a65773d"],
    )
    text = record.read_text(encoding="utf-8")
    assert text.count("openhop_profile:") == 1
    assert "a65773d" in text
    assert "e07722ea" not in text


def test_pin_status_classifies_only_cached_origin_refs(tmp_path: Path):
    """A locally seeded remote-tracking ref is not origin containment."""
    import subprocess

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *args], text=True
        ).strip()

    git("init", "-q")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "base",
    )
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/local-seed/seed", head)
    assert version_record.pin_status(tmp_path, head) == "local-candidate"
    git("update-ref", "refs/remotes/origin/main", head)
    assert version_record.pin_status(tmp_path, head) == "origin-tracking"
    assert version_record.pin_status(tmp_path, "0" * 40) == "unknown"


def test_set_appends_a_new_section(record: Path):
    version_record.set_section(record, "openhop_bridge", ["  radio: VIRTUAL"])
    text = record.read_text(encoding="utf-8")
    assert "openhop_bridge:" in text
    assert "radio: VIRTUAL" in text
    # The existing section is untouched.
    assert "openhop_profile:" in text


def test_the_preamble_is_preserved(record: Path):
    version_record.set_section(record, "openhop_bridge", ["  radio: VIRTUAL"])
    text = record.read_text(encoding="utf-8")
    assert text.startswith("# hardware-lab version record\n")
    assert "recorded_at: 2026-11-24T10:00:00Z" in text


def test_remove_drops_the_section(record: Path):
    version_record.set_section(record, "openhop_bridge", ["  radio: VIRTUAL"])
    version_record.cmd_remove("openhop_bridge", record)
    text = record.read_text(encoding="utf-8")
    assert "openhop_bridge" not in text


def test_compact_keeps_only_the_most_recent_block(record: Path):
    """A record written by the old append-style scripts is cleaned up."""
    record.write_text(
        "# hardware-lab version record\n"
        "openhop_profile:\n"
        "  openhop_core: /src/openhop-core @ e07722ea\n"
        "openhop_profile:\n"
        "  openhop_core: /src/openhop-core @ a65773d\n",
        encoding="utf-8",
    )
    version_record.cmd_compact(record)
    text = record.read_text(encoding="utf-8")
    assert text.count("openhop_profile:") == 1
    assert "a65773d" in text
    assert "e07722ea" not in text


def test_the_body_is_indented(record: Path):
    version_record.set_section(record, "openhop_bridge", ["radio: VIRTUAL"])
    text = record.read_text(encoding="utf-8")
    assert "\n  radio: VIRTUAL\n" in text


def test_the_record_helper_is_importable_by_the_pin_recorder():
    """lab/openhop_pins.py imports set_section from this module."""
    pins = (ROOT / "lab" / "openhop_pins.py").read_text(encoding="utf-8")
    assert "from version_record import set_section" in pins
