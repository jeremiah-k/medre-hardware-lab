"""Check sequential qualification orchestration without a Docker rig."""

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import openhop_qualify as qualify  # noqa: E402


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
