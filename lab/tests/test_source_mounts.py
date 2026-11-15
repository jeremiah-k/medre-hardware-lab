import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

module_path = Path(__file__).resolve().parents[1] / "check_source_mounts.py"
spec = importlib.util.spec_from_file_location("source_guard", module_path)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.fixture
def entry(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    file = root / "src" / "sample.py"
    file.parent.mkdir()
    file.write_text("value = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "src/sample.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    return {
        "source": str(root),
        "ref": revision,
        "file": "src/sample.py",
        "module": "sample",
        "target": "/src/sample",
    }


def test_clean_exact_source(entry):
    assert guard.check_source(entry) == Path(entry["source"])


def test_wrong_revision(entry):
    entry["ref"] = "0" * 40
    with pytest.raises(ValueError, match="HEAD differs"):
        guard.check_source(entry)


def test_dirty_source(entry):
    Path(entry["source"], "src/sample.py").write_text("value = 2\n")
    with pytest.raises(ValueError, match="tracked files"):
        guard.check_source(entry)


def test_deleted_checkout(tmp_path, entry):
    entry["source"] = str(tmp_path / "deleted")
    with pytest.raises(FileNotFoundError):
        guard.check_source(entry)


def test_empty_recreated_source(entry):
    Path(entry["source"], "src/sample.py").unlink()
    with pytest.raises(ValueError, match="tracked files"):
        guard.check_source(entry)


def test_parent_checkout_not_accepted(entry):
    entry["source"] = str(Path(entry["source"], "src"))
    with pytest.raises(ValueError, match="checkout root"):
        guard.check_source(entry)


def test_wrong_container_mount_fails_before_import(entry, monkeypatch):
    data = [{"State": {"Running": True}, "Mounts": []}]
    monkeypatch.setattr(
        guard.subprocess, "check_output", lambda *a, **kw: json.dumps(data)
    )
    monkeypatch.setattr(guard, "check_source", lambda e: Path(e["source"]))
    with pytest.raises(ValueError, match="source mount"):
        guard.check_container("rig", [entry])
