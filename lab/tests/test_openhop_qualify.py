"""Check sequential qualification orchestration without a Docker rig."""

import fcntl
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import openhop_qualify as qualify  # noqa: E402


def hold_lock(path):
    """Take the lock the way a second process would: a fresh open() is a
    new file description, so it is a real contender for the flock."""
    handle = open(path, "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_qualification_runs_every_stage_and_keeps_fault_last(tmp_path, monkeypatch):
    monkeypatch.setattr(qualify, "assert_rig_down", Mock())
    stage = Mock(return_value=0)
    monkeypatch.setattr(qualify, "run_stage", stage)
    assert qualify.qualify(tmp_path / "run", skip_build=True) == 0
    assert [call.args[1] for call in stage.call_args_list] == [
        "lifecycle-up",
        "lifecycle",
        "lifecycle-down",
        "radio-up",
        "packet-path",
        "radio-down",
        "bridge-up",
        "bridge-proof",
        "backlog",
        "ordering",
        "fault",
        "verify-backlog",
        "verify-ordering",
        "verify-fault",
        "bridge-down",
    ]
    assert all(
        "--skip-build" in command
        for _, name, command in (call.args for call in stage.call_args_list)
        if name.endswith("-up")
    )


def test_campaign_matrices_run_fail_fast(tmp_path):
    arguments = dict(qualify.stages(tmp_path, skip_build=True))
    for matrix in ("backlog", "ordering", "fault"):
        assert "--fail-fast" in arguments[matrix]


@pytest.mark.parametrize("failure", ["lifecycle-up", "packet-path", "verify-ordering"])
def test_qualification_preserves_first_failure_and_attempts_cleanup(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(qualify, "assert_rig_down", Mock())
    stage = Mock(side_effect=lambda _, name, command: 7 if name == failure else 0)
    monkeypatch.setattr(qualify, "run_stage", stage)
    assert qualify.qualify(tmp_path / "run", skip_build=False) == 7
    assert [call.args[1] for call in stage.call_args_list][-2:] == [failure, "cleanup"]


def test_refused_rig_does_not_run_teardown(tmp_path, monkeypatch):
    monkeypatch.setattr(qualify, "assert_rig_down", Mock(side_effect=ValueError))
    stage = Mock()
    monkeypatch.setattr(qualify, "run_stage", stage)
    with pytest.raises(ValueError):
        qualify.qualify(tmp_path / "run", skip_build=False)
    stage.assert_not_called()


def test_stage_records_exit_and_logs(tmp_path):
    assert (
        qualify.run_stage(tmp_path, "example", ["bash", "-c", "echo evidence; exit 7"])
        == 7
    )
    record = json.loads((tmp_path / "timeline.jsonl").read_text())
    assert record["stage"] == "example"
    assert record["exit_code"] == 7
    assert record["elapsed_seconds"] >= 0
    assert (tmp_path / record["log"]).read_text() == "evidence\n"


def fresh_qualification(tmp_path, monkeypatch):
    """Point the lock at the test's tree and forget any lock this test
    process already holds, so each test negotiates the lock for real."""
    monkeypatch.setattr(qualify, "_qualification_lock", None)
    monkeypatch.setattr(qualify, "assert_rig_down", Mock())
    stage = Mock(return_value=0)
    monkeypatch.setattr(qualify, "run_stage", stage)
    return stage


def test_a_second_holder_is_refused_before_any_stage(tmp_path, monkeypatch):
    lock_path = tmp_path / "openhop-qualification.lock"
    holder = hold_lock(lock_path)
    try:
        stage = fresh_qualification(tmp_path, monkeypatch)
        with pytest.raises(ValueError) as exc:
            qualify.qualify(tmp_path / "run", skip_build=True, lock_path=lock_path)
    finally:
        holder.close()
    assert str(lock_path) in str(exc.value)
    assert "another qualification" in str(exc.value)
    stage.assert_not_called()
    qualify.assert_rig_down.assert_not_called()


def test_the_lock_is_taken_again_after_the_holder_releases(tmp_path, monkeypatch):
    lock_path = tmp_path / "openhop-qualification.lock"
    holder = hold_lock(lock_path)
    holder.close()
    stage = fresh_qualification(tmp_path, monkeypatch)
    assert qualify.qualify(tmp_path / "run", skip_build=True, lock_path=lock_path) == 0
    assert [call.args[1] for call in stage.call_args_list][-1] == "bridge-down"


def test_a_stale_lock_file_does_not_block(tmp_path, monkeypatch):
    """The lock is advisory state, not the file's existence: a file left
    by a dead holder carries no lock, so qualification proceeds."""
    lock_path = tmp_path / "openhop-qualification.lock"
    lock_path.write_text("31337\n", encoding="utf-8")
    fresh_qualification(tmp_path, monkeypatch)
    assert qualify.qualify(tmp_path / "run", skip_build=True, lock_path=lock_path) == 0
