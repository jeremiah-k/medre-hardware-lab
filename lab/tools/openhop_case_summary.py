#!/usr/bin/env python3
"""Aggregate the per-case evidence files of a lab profile into one summary.

WHY THIS EXISTS
---------------
Each driver writes ONE JSON file per case and then a ``summary.json``.
Building the summary from the cases the driver happened to run in the LAST
invocation made it wrong in the most confusing way: re-running one case
(say, after a fix) left the summary listing only that case, and the five
cases that had passed looked like they had never run.

The summary is therefore built from the EVIDENCE DIRECTORY: every case
file that is there is listed, with the status the case file itself
records.  A case file that says NOT RUN is listed as NOT RUN, not as a
failure and not as a pass.

The aggregation is a pure function of the files, so the summary can be
regenerated from the existing evidence WITHOUT re-running anything (no
Docker, no traffic).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openhop_selection as selection  # noqa: E402

LAB_ROOT = Path(__file__).resolve().parents[2]

SUMMARY_NAME = "summary.json"
SCOPE_NAME = "source-selection.json"
CARRY_FORWARD_NAME = "selection-carry-forward.json"

# The stamp format the first fence wrote: identifiers only. Such stamps
# are never rewritten; their selection is RECONSTRUCTED from the recorded
# lab revision and compared by content (see bind_source_selection).
LEGACY_STAMP_KEYS = {"lab_revision", "lock_sha256", "profile"}
FAULT_CASES = {
    "fault-idle-blackhole",
    "fault-latency-under",
    "fault-latency-over",
    "fault-mid-fetch-close",
    "fault-reconnect",
    "fault-repeated-waiting",
    "fault-reply-stall",
    "fault-reply-stall-drain",
    "fault-saturation",
    "fault-selective-drop",
}


def _run_identifiers(lab_root: Path, revision: str) -> dict:
    """The informational identifiers of a run: lab revision + lock digest."""
    lock_sha256 = hashlib.sha256(
        selection.lock_bytes_at(lab_root, revision)
    ).hexdigest()
    return {"lab_revision": revision, "lock_sha256": lock_sha256}


def _stamp_error(stamp: dict) -> str | None:
    """Describe a new-format stamp that is not self-consistent."""
    if stamp.get("selection_sha256") != selection.selection_digest(stamp):
        return "the recorded stamp is not self-consistent with its fields"
    return None


def bind_source_selection(evidence_dir: Path, lab_root: Path, profile: str) -> None:
    """Prevent merging cases produced under different source CONTENT.

    The fence binds evidence to the content the selection provides, never
    to identifiers: a commit that cannot change what the rig builds,
    mounts or runs accepts the directory again (see
    ``lab/openhop_selection.py`` for the content rule). This is a
    selection fence, not proof of installed/running source bytes; the
    live source-mount and independent observer checks remain required.
    Historical directories without the fence are summary-only.
    """
    if not selection.is_committed_clean(lab_root):
        raise ValueError("lab source is dirty; commit the qualification harness first")
    revision = selection.head_revision(lab_root)
    record = selection.selection_record(lab_root, profile, revision=revision)
    scope = evidence_dir / SCOPE_NAME
    if scope.exists():
        stamp = json.loads(scope.read_text(encoding="utf-8"))
        if set(stamp) == LEGACY_STAMP_KEYS:
            # Reconstruct the recorded selection from the recorded lab
            # revision; the stamp itself is never rewritten.
            recorded = selection.selection_record(
                lab_root, stamp["profile"], revision=stamp["lab_revision"]
            )
        else:
            inconsistent = _stamp_error(stamp)
            if inconsistent:
                raise ValueError(f"{inconsistent}; use a new evidence directory")
            recorded = stamp
        difference = selection.describe_difference(recorded, record)
        if difference is not None:
            raise ValueError(
                f"source selection changed: {difference}; "
                "use a new evidence directory"
            )
        return
    if any(evidence_dir.glob("*.json")):
        raise ValueError("historical evidence is unbound; use a new evidence directory")
    stamp = dict(record)
    stamp.update(_run_identifiers(lab_root, revision))
    with scope.open("x", encoding="utf-8") as stream:
        json.dump(stamp, stream, indent=2, sort_keys=True)
        stream.write("\n")


def verify_selection(
    evidence_dir: Path, lab_root: Path, profile: str | None = None
) -> tuple[bool, str]:
    """Carry a recorded pass forward when the content is the same.

    Returns (accepted, line). On acceptance the summary is regenerated
    from the case files, every case must be a pass, one line names the
    case counts, and an entry is APPENDED to
    ``selection-carry-forward.json``; the recorded stamp stays untouched.
    """
    if not selection.is_committed_clean(lab_root):
        raise ValueError("lab source is dirty; commit the qualification harness first")
    scope = evidence_dir / SCOPE_NAME
    if not scope.exists():
        if any(evidence_dir.glob("*.json")):
            raise ValueError(
                "historical evidence is unbound; use a new evidence directory"
            )
        raise ValueError("the directory records no selection and no case evidence")
    stamp = json.loads(scope.read_text(encoding="utf-8"))
    if set(stamp) == LEGACY_STAMP_KEYS:
        recorded = selection.selection_record(
            lab_root, stamp["profile"], revision=stamp["lab_revision"]
        )
        identifiers = {
            "lab_revision": stamp["lab_revision"],
            "lock_sha256": stamp["lock_sha256"],
        }
    else:
        inconsistent = _stamp_error(stamp)
        if inconsistent:
            raise ValueError(inconsistent)
        recorded = stamp
        identifiers = {
            "lab_revision": stamp["lab_revision"],
            "lock_sha256": stamp["lock_sha256"],
        }
    if profile is not None and recorded["profile"] != profile:
        raise ValueError(
            f"the profile differs (was {recorded['profile']!r}, now {profile!r})"
        )
    revision = selection.head_revision(lab_root)
    record = selection.selection_record(
        lab_root, recorded["profile"], revision=revision
    )
    difference = selection.describe_difference(recorded, record)
    if difference is not None:
        raise ValueError(f"source selection differs: {difference}")
    cases = collect_cases(evidence_dir)
    if not cases:
        raise ValueError("the directory holds no case evidence")
    names = {case["case"] for case in cases}
    missing = set()
    if recorded["profile"] == "openhop-fault":
        missing = FAULT_CASES - names
    elif recorded["profile"] == "openhop-bridge":
        prefixes = {name.partition("-")[0] for name in names}
        recognized = False
        for family, size in (("c", 6), ("o", 4)):
            planned = {f"{family}{index}" for index in range(1, size + 1)}
            if planned & prefixes:
                recognized = True
                missing.update(planned - prefixes)
        if not recognized:
            raise ValueError("the directory holds no backlog or ordering evidence")
    if missing:
        raise ValueError("missing planned cases: " + ", ".join(sorted(missing)))
    failing = [case for case in cases if case["status"] != "PASS"]
    if failing:
        named = ", ".join(f"{case['case']}: {case['status']}" for case in failing)
        raise ValueError(
            f"{len(failing)} of {len(cases)} cases are not passes ({named})"
        )
    line = f"{len(cases)} cases: {len(cases) - len(failing)} pass"
    entry = {
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selection_sha256": record["selection_sha256"],
        "from": identifiers,
        "to": _run_identifiers(lab_root, revision),
    }
    carry = evidence_dir / CARRY_FORWARD_NAME
    history: list[dict] = []
    if carry.exists():
        history = json.loads(carry.read_text(encoding="utf-8"))
    history.append(entry)
    write_summary(
        evidence_dir, build_summary(evidence_dir, profile=recorded["profile"])
    )
    carry.write_text(
        json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return True, line


def case_status(payload: dict) -> str:
    """Return the status a case file records.

    A case that did not run says so in its own file, and that is what the
    summary must say: NOT RUN is neither a pass nor a failure.
    """
    declared = payload.get("status")
    passed = payload.get("pass")
    if declared == "NOT RUN" and passed is False:
        return "NOT RUN"
    if passed is True and declared in (None, "PASS") and not payload.get("failures"):
        return "PASS"
    return "FAIL"


def collect_cases(evidence_dir: Path, prefix: str = "") -> list[dict]:
    """Return one entry per case file in the evidence directory.

    ``prefix`` restricts the scan to the cases of one profile (for example
    ``fault-``), so two profiles can share a directory without mixing.
    """
    entries: list[dict] = []
    for path in sorted(evidence_dir.glob(f"{prefix}*.json")):
        if path.name in {SUMMARY_NAME, SCOPE_NAME, CARRY_FORWARD_NAME}:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {
                "case": path.stem,
                "pass": False,
                "reason": "unreadable case evidence",
            }
        if not isinstance(payload, dict) or payload.get("case") != path.stem:
            payload = {
                "case": path.stem,
                "pass": False,
                "reason": "invalid case identity",
            }
        status = case_status(payload)
        entries.append(
            {
                "case": payload.get("case") or path.stem,
                "status": status,
                "evidence": str(path),
                "pass": status == "PASS",
                "failures": payload.get("failures") or [],
                "reason": payload.get("reason"),
            }
        )
    return entries


def build_summary(
    evidence_dir: Path,
    prefix: str = "",
    label: str = "protocol-simulated",
    generated_at: str | None = None,
    profile: str | None = None,
) -> dict:
    """Build the summary of a profile's cases from the evidence directory."""
    cases = collect_cases(evidence_dir, prefix)
    counts = {"PASS": 0, "FAIL": 0, "NOT RUN": 0}
    for entry in cases:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    summary = {
        "label": label,
        "case_count": len(cases),
        "counts": counts,
        "cases": cases,
    }
    scope = evidence_dir / SCOPE_NAME
    summary["source_selection"] = (
        json.loads(scope.read_text(encoding="utf-8")) if scope.exists() else None
    )
    if generated_at:
        summary["generated_at"] = generated_at
    if profile:
        summary["profile"] = profile
    return summary


def write_summary(evidence_dir: Path, summary: dict) -> Path:
    """Write the summary into the evidence directory and return its path."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / SUMMARY_NAME
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return path


def main(argv: list[str] | None = None, lab_root: Path | None = None) -> int:
    """Aggregate or verify evidence directories from the command line.

    ``--verify-selection DIR`` carries a directory's recorded pass forward
    when its selection still provides the same content as the current one
    (including through a stamp that records identifiers only). Without it,
    the named directory's summary is regenerated from its case files --
    which works for every stamp format, since the summary is a pure
    function of the files.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-selection",
        type=Path,
        metavar="EVIDENCE_DIR",
        help="carry a recorded pass forward when the content is unchanged",
    )
    parser.add_argument(
        "--profile",
        help="the profile the selection must name (with --verify-selection)",
    )
    parser.add_argument(
        "summary_dir",
        type=Path,
        nargs="?",
        metavar="EVIDENCE_DIR",
        help="regenerate this directory's summary from its case files",
    )
    parser.add_argument(
        "--prefix", default="", help="restrict the summary to one profile's cases"
    )
    parser.add_argument("--label", default="protocol-simulated")
    parser.add_argument(
        "--write",
        action="store_true",
        help="write summary.json into the directory (summary path only)",
    )
    args = parser.parse_args(argv)
    root = lab_root if lab_root is not None else LAB_ROOT
    if args.verify_selection is not None:
        try:
            accepted, line = verify_selection(args.verify_selection, root, args.profile)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        print(line)
        return 0
    if args.summary_dir is None:
        parser.print_usage(sys.stderr)
        return 2
    built = build_summary(args.summary_dir, prefix=args.prefix, label=args.label)
    if args.write:
        write_summary(args.summary_dir, built)
    print(json.dumps(built, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
