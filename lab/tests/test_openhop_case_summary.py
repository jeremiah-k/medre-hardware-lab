"""Offline tests for the evidence-summary aggregator.

The summary must list EVERY case file in the evidence directory, with the
status the case file itself records. The defect this guards against: the
summary was built from the cases the LAST invocation ran, so re-running one
case dropped the other cases from the summary and the five cases that had
passed looked like they had never run.

These tests also pin the rule that matters for the report: a case that did
not run is NOT RUN, not a pass and not a failure.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "openhop_case_summary.py"
spec = importlib.util.spec_from_file_location("openhop_case_summary", MODULE_PATH)
assert spec and spec.loader
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def write_case(directory: Path, name: str, payload: dict) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_collect_cases_lists_every_case_file(tmp_path):
    write_case(tmp_path, "c1-a.json", {"case": "c1-a", "pass": True})
    write_case(tmp_path, "c2-b.json", {"case": "c2-b", "pass": True})
    write_case(tmp_path, "c3-c.json", {"case": "c3-c", "pass": False})
    cases = summary.collect_cases(tmp_path)
    assert [entry["case"] for entry in cases] == ["c1-a", "c2-b", "c3-c"]


def test_collect_cases_ignores_the_summary_file(tmp_path):
    write_case(tmp_path, "c1-a.json", {"case": "c1-a", "pass": True})
    write_case(tmp_path, "summary.json", {"cases": [], "case_count": 99})
    cases = summary.collect_cases(tmp_path)
    assert [entry["case"] for entry in cases] == ["c1-a"]


def test_collect_cases_respects_the_prefix(tmp_path):
    write_case(tmp_path, "c1-a.json", {"case": "c1-a", "pass": True})
    write_case(
        tmp_path,
        "fault-latency-under.json",
        {"case": "fault-latency-under", "pass": True},
    )
    assert len(summary.collect_cases(tmp_path, prefix="c")) == 1
    assert len(summary.collect_cases(tmp_path, prefix="fault-")) == 1


def test_status_distinguishes_pass_fail_and_not_run():
    assert summary.case_status({"pass": True}) == "PASS"
    assert summary.case_status({"pass": False}) == "FAIL"
    assert summary.case_status({"pass": False, "status": "NOT RUN"}) == "NOT RUN"
    # A NOT RUN case that also carries a reason is still NOT RUN.
    assert (
        summary.case_status({"pass": False, "status": "NOT RUN", "reason": "x"})
        == "NOT RUN"
    )


def test_build_summary_counts_every_case(tmp_path):
    write_case(tmp_path, "c1-a.json", {"case": "c1-a", "pass": True})
    write_case(tmp_path, "c2-b.json", {"case": "c2-b", "pass": True})
    write_case(tmp_path, "c3-c.json", {"case": "c3-c", "pass": False})
    write_case(
        tmp_path, "c4-d.json", {"case": "c4-d", "pass": False, "status": "NOT RUN"}
    )
    built = summary.build_summary(tmp_path, prefix="c")
    assert built["case_count"] == 4
    assert built["counts"] == {"PASS": 2, "FAIL": 1, "NOT RUN": 1}
    statuses = {entry["case"]: entry["status"] for entry in built["cases"]}
    assert statuses == {
        "c1-a": "PASS",
        "c2-b": "PASS",
        "c3-c": "FAIL",
        "c4-d": "NOT RUN",
    }


def test_every_entry_carries_its_evidence_path(tmp_path):
    path = write_case(tmp_path, "c1-a.json", {"case": "c1-a", "pass": True})
    built = summary.build_summary(tmp_path, prefix="c")
    assert built["cases"][0]["evidence"] == str(path)


def test_write_summary_round_trips(tmp_path):
    write_case(tmp_path, "c1-a.json", {"case": "c1-a", "pass": True})
    built = summary.build_summary(tmp_path, prefix="c")
    written = summary.write_summary(tmp_path, built)
    assert written.name == "summary.json"
    reloaded = json.loads(written.read_text(encoding="utf-8"))
    assert reloaded["case_count"] == 1
    assert reloaded["cases"][0]["status"] == "PASS"


def test_a_malformed_case_file_does_not_break_the_summary(tmp_path):
    write_case(tmp_path, "c1-a.json", {"case": "c1-a", "pass": True})
    (tmp_path / "c2-broken.json").write_text("{not json", encoding="utf-8")
    built = summary.build_summary(tmp_path, prefix="c")
    assert built["case_count"] == 2
    assert built["counts"] == {"PASS": 1, "FAIL": 1, "NOT RUN": 0}
    assert built["cases"][1]["reason"] == "unreadable case evidence"


def test_contradictory_and_misattributed_evidence_cannot_pass(tmp_path):
    write_case(
        tmp_path, "c1-a.json", {"case": "c1-a", "pass": True, "status": "NOT RUN"}
    )
    write_case(tmp_path, "c2-b.json", {"case": "c1-a", "pass": True})
    write_case(
        tmp_path, "c3-c.json", {"case": "c3-c", "pass": True, "failures": ["bad"]}
    )
    built = summary.build_summary(tmp_path, prefix="c")
    assert built["counts"] == {"PASS": 0, "FAIL": 3, "NOT RUN": 0}
    assert not any(case["pass"] for case in built["cases"])


def test_source_selection_cannot_mix_historical_or_changed_content(tmp_path):
    """The fence binds evidence to source CONTENT, not to identifiers.

    A lab whose committed changes touch only excluded paths (here: the
    lab's own tests) accepts the recorded directory again; a changed pin
    and an unbound historical directory are refused. The richer fence
    matrix lives in test_openhop_selection.py.
    """
    lab = tmp_path / "lab"
    lab.mkdir()
    subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=lab)
    (lab / ".gitignore").write_text("sources/\n", encoding="utf-8")
    lock = lab / "sources.lock.json"
    lock.write_text("{}", encoding="utf-8")
    lab_tests = lab / "tests" / "test_placeholder.py"
    lab_tests.parent.mkdir()
    lab_tests.write_text("def test_p():\n    assert True\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "-A"], cwd=lab)
    subprocess.check_call(
        [
            "git",
            "-c",
            "user.name=Lab Test",
            "-c",
            "user.email=lab@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=lab,
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    summary.bind_source_selection(evidence, lab, "bridge")
    write_case(evidence, "c1-a.json", {"case": "c1-a", "pass": True})
    summary.bind_source_selection(evidence, lab, "bridge")
    lab_tests.write_text(
        "def test_p():\n    assert True  # changed\n", encoding="utf-8"
    )
    subprocess.check_call(["git", "add", "-A"], cwd=lab)
    subprocess.check_call(
        [
            "git",
            "-c",
            "user.name=Lab Test",
            "-c",
            "user.email=lab@example.invalid",
            "commit",
            "-q",
            "-m",
            "lab tests only",
        ],
        cwd=lab,
    )
    summary.bind_source_selection(evidence, lab, "bridge")
    (evidence / summary.SCOPE_NAME).unlink()
    with pytest.raises(ValueError, match="historical evidence"):
        summary.bind_source_selection(evidence, lab, "bridge")
