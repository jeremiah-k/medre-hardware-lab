"""Source provenance cannot mistake a local seed ref for publication."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openhop_pins as pins


def test_local_seed_ref_is_not_origin_tracking(tmp_path):
    def git(*args):
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
    git("update-ref", "refs/remotes/local-qualification/seed", head)
    assert pins.status_of(tmp_path, head) == "local-candidate"
    git("update-ref", "refs/remotes/origin/main", head)
    assert pins.status_of(tmp_path, head) == "origin-tracking"


def test_conflicting_mount_sources_are_rejected(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "a": [{"module": "medre.core", "source": "/a", "ref": "a"}],
                "b": [{"module": "medre.core", "source": "/b", "ref": "b"}],
            }
        )
    )
    with pytest.raises(ValueError, match="conflicting"):
        pins.manifest_entries(manifest)


def test_failed_git_inspection_never_updates_record(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "a": [
                    {
                        "module": "openhop_core",
                        "source": str(tmp_path / "missing"),
                        "ref": "a",
                    }
                ]
            }
        )
    )
    record = tmp_path / "VERSION"
    record.write_text("original\n")
    assert pins.main(["pins", str(manifest), "--record", str(record)]) == 1
    assert record.read_text() == "original\n"
