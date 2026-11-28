"""Tests for the compose suite's evidence fence.

A suite run stamps a per-run evidence directory with the SHARED
source-selection machinery (lab/openhop_selection.py digests, the
lab/tools/openhop_case_summary.py stamp/aggregation/verify pattern) --
this module must not grow any digest logic of its own. The fixtures are
temporary lab repositories (init + commit, no network, no Docker) laid
out exactly like the real one, so the REAL runner script and the REAL
verifier run against them unmodified.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "scenario_evidence", ROOT / "lab/scenario_evidence.py"
)
assert _spec and _spec.loader
scenario_evidence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scenario_evidence)

GIT_IDENTITY = ("-c", "user.name=Lab Test", "-c", "user.email=lab@example.invalid")


def run_git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def commit_all(repo: Path, message: str = "base") -> str:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    return run_git(repo, "rev-parse", "HEAD").strip()


SCENARIO_DEMO = "#!/usr/bin/env bash\necho demo ok\nexit 0\n"
SCENARIO_BROKEN = "#!/usr/bin/env bash\necho broken\nexit 1\n"
SCENARIO_SKIPPING = "#!/usr/bin/env bash\necho not applicable\nexit 2\n"


def init_lab(tmp_path: Path) -> Path:
    lab = tmp_path / "labrepo"
    (lab / "lab/tools").mkdir(parents=True)
    (lab / "lab/tests").mkdir()
    (lab / "scenarios").mkdir()
    shutil.copyfile(ROOT / "lab/openhop_selection.py", lab / "lab/openhop_selection.py")
    shutil.copyfile(
        ROOT / "lab/tools/openhop_case_summary.py",
        lab / "lab/tools/openhop_case_summary.py",
    )
    shutil.copyfile(ROOT / "lab/scenario_evidence.py", lab / "lab/scenario_evidence.py")
    shutil.copyfile(ROOT / "lab/run-scenarios.sh", lab / "lab/run-scenarios.sh")
    for name, body in (
        ("scenario-demo.sh", SCENARIO_DEMO),
        ("scenario-broken.sh", SCENARIO_BROKEN),
        ("scenario-skipping.sh", SCENARIO_SKIPPING),
    ):
        script = lab / "scenarios" / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
    (lab / "sources.lock.json").write_text("{}\n", encoding="utf-8")
    (lab / ".gitignore").write_text(
        "sources/\nevidence/\nlab/logs/\nlab/state/\n__pycache__/\n", encoding="utf-8"
    )
    (lab / "lab/tests/test_placeholder.py").write_text(
        "def test_p():\n    assert True\n", encoding="utf-8"
    )
    run_git(lab, "init", "-q", "-b", "main")
    commit_all(lab)
    return lab


def run_runner(lab: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(lab / "lab/run-scenarios.sh"), *args],
        capture_output=True,
        text=True,
        env={**os.environ},
        cwd=lab,
        timeout=120,
    )


def verify(lab: Path, evidence: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(lab / "lab/tools/openhop_case_summary.py"),
            "--verify-selection",
            str(evidence),
            "--profile",
            "scenarios",
        ],
        capture_output=True,
        text=True,
        cwd=lab,
        timeout=60,
    )


def test_record_case_writes_passes_and_failures_only(tmp_path: Path):
    passed = scenario_evidence.record_case(tmp_path, "demo", "PASS", "lab/logs/d.log")
    failed = scenario_evidence.record_case(tmp_path, "broken", "FAIL", "lab/logs/b.log")
    assert passed == tmp_path / "demo.json"
    assert failed == tmp_path / "broken.json"
    assert json.loads(passed.read_text())["case"] == "demo"
    assert json.loads(passed.read_text())["pass"] is True
    assert json.loads(failed.read_text())["pass"] is False
    assert scenario_evidence.record_case(tmp_path, "skip", "SKIP", "x") is None
    assert scenario_evidence.record_case(tmp_path, "deferred", "DEFERRED", "x") is None
    assert not (tmp_path / "skip.json").exists()
    with pytest.raises(ValueError, match="verdict"):
        scenario_evidence.record_case(tmp_path, "odd", "WEIRD", "x")


def test_a_run_stamps_binds_and_carries_its_pass_forward(tmp_path: Path):
    lab = init_lab(tmp_path)
    evidence = tmp_path / "evidence" / "run"
    result = run_runner(lab, "--evidence-dir", str(evidence), "demo")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Evidence directory:" in result.stdout
    stamp = json.loads((evidence / "source-selection.json").read_text())
    assert stamp["profile"] == "scenarios"
    case = json.loads((evidence / "demo.json").read_text())
    assert case["case"] == "demo"
    assert case["pass"] is True
    assert case["log"].startswith("lab/logs/scenarios/demo-")
    summary = json.loads((evidence / "summary.json").read_text())
    assert summary["counts"] == {"PASS": 1, "FAIL": 0, "NOT RUN": 0}
    assert summary["source_selection"]["profile"] == "scenarios"
    # The shared verifier carries the pass forward, and appends on re-verify.
    carried = verify(lab, evidence)
    assert carried.returncode == 0, carried.stderr
    assert "1 cases: 1 pass" in carried.stdout
    again = verify(lab, evidence)
    assert again.returncode == 0, again.stderr
    history = json.loads((evidence / "selection-carry-forward.json").read_text())
    assert len(history) == 2


def test_a_failing_scenario_is_bound_but_not_carried_forward(tmp_path: Path):
    lab = init_lab(tmp_path)
    evidence = tmp_path / "evidence" / "failed"
    result = run_runner(lab, "--evidence-dir", str(evidence), "broken")
    assert result.returncode == 1
    assert json.loads((evidence / "broken.json").read_text())["pass"] is False
    carried = verify(lab, evidence)
    assert carried.returncode == 1
    assert "REFUSED" in carried.stderr
    assert not (evidence / "selection-carry-forward.json").exists()


def test_skipped_scenarios_write_no_case_file(tmp_path: Path):
    lab = init_lab(tmp_path)
    evidence = tmp_path / "evidence" / "skipped"
    result = run_runner(lab, "--evidence-dir", str(evidence), "skipping")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP" in result.stdout
    assert sorted(path.name for path in evidence.glob("*.json")) == [
        "source-selection.json",
        "summary.json",
    ]
    carried = verify(lab, evidence)
    assert carried.returncode == 1
    assert "no case evidence" in carried.stderr


def test_content_changes_refuse_and_excluded_changes_accept(tmp_path: Path):
    lab = init_lab(tmp_path)
    evidence = tmp_path / "evidence" / "fenced"
    result = run_runner(lab, "--evidence-dir", str(evidence), "demo")
    assert result.returncode == 0, result.stdout + result.stderr
    # lab/tests/ is excluded from the lab tooling digest: the fence accepts.
    (lab / "lab/tests/test_placeholder.py").write_text(
        "def test_p():\n    assert True  # changed\n", encoding="utf-8"
    )
    commit_all(lab, "lab tests only")
    assert verify(lab, evidence).returncode == 0
    # The runner script itself is included content: the fence refuses.
    runner = lab / "lab/run-scenarios.sh"
    runner.write_text(runner.read_text() + "\n# trailing change\n", encoding="utf-8")
    commit_all(lab, "runner change")
    refused = verify(lab, evidence)
    assert refused.returncode == 1
    assert "source selection differs" in refused.stderr


def test_a_dirty_lab_tree_refuses_the_run_before_any_scenario(tmp_path: Path):
    lab = init_lab(tmp_path)
    (lab / "lab/run-scenarios.sh").write_text(
        (lab / "lab/run-scenarios.sh").read_text() + "\n# uncommitted\n",
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence" / "dirty"
    result = run_runner(lab, "--evidence-dir", str(evidence), "demo")
    assert result.returncode == 1
    assert "refused" in result.stderr
    # Refused before any evidence or scenario ran: an empty directory, no
    # stamp, no case files, no scenario log.
    assert sorted(path.name for path in evidence.glob("*")) == []
    assert not list((lab / "lab/logs/scenarios").glob("demo-*.log"))


def test_a_deferred_only_run_records_nothing(tmp_path: Path):
    lab = init_lab(tmp_path)
    evidence = tmp_path / "evidence" / "deferred"
    result = run_runner(lab, "--evidence-dir", str(evidence), "threenode-same-mesh")
    assert result.returncode == 0
    assert "(deferred)" in result.stdout
    assert not evidence.exists()


def test_the_default_evidence_directory_is_per_run(tmp_path: Path):
    lab = init_lab(tmp_path)
    result = run_runner(lab, "demo")
    assert result.returncode == 0, result.stdout + result.stderr
    runs = list((lab / "evidence" / "scenarios").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "source-selection.json").is_file()
    assert (runs[0] / "summary.json").is_file()


def test_a_reused_directory_is_refused_before_a_skipped_rerun(tmp_path):
    lab = init_lab(tmp_path)
    evidence = tmp_path / "evidence" / "run"
    assert run_runner(lab, "--evidence-dir", str(evidence), "demo").returncode == 0
    previous = (evidence / "demo.json").read_bytes()
    rerun = run_runner(lab, "--evidence-dir", str(evidence), "skipping")
    assert rerun.returncode == 1
    assert "fresh directory" in rerun.stderr
    assert (evidence / "demo.json").read_bytes() == previous
    assert not list((lab / "lab/logs/scenarios").glob("skipping-*.log"))


def test_untracked_scenario_cannot_produce_evidence(tmp_path):
    lab = init_lab(tmp_path)
    script = lab / "scenarios/scenario-untracked.sh"
    script.write_text(SCENARIO_DEMO)
    script.chmod(0o755)
    evidence = tmp_path / "evidence" / "untracked"
    assert run_runner(lab, "--evidence-dir", str(evidence), "untracked").returncode == 1
    assert not evidence.exists()


def test_partial_scenario_run_cannot_be_carried_forward(tmp_path):
    lab = init_lab(tmp_path)
    evidence = tmp_path / "evidence" / "interrupted"
    assert run_runner(lab, "--evidence-dir", str(evidence), "demo").returncode == 0
    (evidence / "scenario-run-complete.json").unlink()
    result = verify(lab, evidence)
    assert result.returncode == 1
    assert "successful completion" in result.stderr
