import json
from pathlib import Path

import pytest

from medre_lab.docker import lock


def test_committed_sources_are_complete_and_exact():
    root = Path(__file__).resolve().parents[1]
    assert len(lock(root)) == 5


@pytest.mark.parametrize("change", ["missing", "moving", "credential"])
def test_invalid_source_plan_rejected(tmp_path, change):
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "sources.lock.json").read_text())
    if change == "missing":
        del data["medre"]
    if change == "moving":
        data["medre"]["ref"] = "main"
    if change == "credential":
        data["medre"]["url"] = "https://user:password@github.com/owner/repo"
    (tmp_path / "sources.lock.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        lock(tmp_path)


def test_duplicate_lock_key_rejected(tmp_path):
    (tmp_path / "sources.lock.json").write_text('{"medre":{},"medre":{}}')
    with pytest.raises(ValueError, match="duplicate"):
        lock(tmp_path)
