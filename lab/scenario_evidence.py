#!/usr/bin/env python3
"""Bind the compose scenario suite's evidence to its source selection.

WHY THIS EXISTS
---------------
The openHop qualification drivers stamp every evidence directory with a
source-selection record (lab/openhop_selection.py): content digests over
the pinned checkouts and the lab's own tree, so a recorded pass carries
forward exactly when the content it was measured against is still the
content on offer. The compose scenario suite (lab/run-scenarios.sh) had
no such binding: its verdicts landed in logs tied to nothing.

A suite run now writes the SAME stamp machinery into a per-run evidence
directory (default ``evidence/scenarios/<ts>``, or ``--evidence-dir``):

* ``source-selection.json`` -- the stamp, written BEFORE any scenario
  runs; the run refuses to start when the stamp cannot be established
  (dirty lab tree, or an evidence directory whose content moved);
* ``<scenario>.json`` -- one case file per EXECUTED scenario (PASS or
  FAIL). Skipped scenarios are not qualification evidence
  (lab/README.md) and write no case file;
* ``summary.json`` -- the aggregate, built with the shared aggregator
  (lab/tools/openhop_case_summary.py) and embedding the stamp.

Carrying a recorded pass forward is the shared verifier, unchanged:

  python3 lab/tools/openhop_case_summary.py --verify-selection <dir> \
      --profile scenarios

It regenerates the summary from the case files, appends an entry to
``selection-carry-forward.json``, and refuses on any content change. No
digest logic lives here: this module only drives the shared machinery
for the scenario profile. (It deliberately sits OUTSIDE lab/tools/: the
lab-tools image bakes that directory, and a fence helper is not an
image ingredient.)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import openhop_case_summary as case_summary  # noqa: E402

# The stamp profile this suite records; the verifier takes it via
# --profile and the openHop case-name tables stay keyed on theirs.
PROFILE = "scenarios"
LABEL = "compose-scenarios"


def bind(evidence_dir: Path, lab_root: Path) -> Path:
    """Stamp a fresh evidence directory with the current selection."""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    case_summary.bind_source_selection(evidence_dir, lab_root, PROFILE)
    return evidence_dir / case_summary.SCOPE_NAME


def record_case(evidence_dir: Path, name: str, verdict: str, log: str) -> Path | None:
    """Write one case file for an executed scenario.

    A PASS or FAIL scenario becomes a case file named after the case
    (the aggregator requires the identity); SKIP and DEFERRED verdicts
    write nothing -- they did not measure anything.
    """
    if verdict in ("SKIP", "DEFERRED"):
        return None
    if verdict not in ("PASS", "FAIL"):
        raise ValueError(
            f"scenario verdict {verdict!r} is not PASS, FAIL, SKIP or DEFERRED"
        )
    payload = {
        "case": name,
        "pass": verdict == "PASS",
        "log": log,
    }
    if verdict == "FAIL":
        payload["reason"] = "scenario failed; see the log for its assertions"
    path = Path(evidence_dir) / f"{name}.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def write_summary(evidence_dir: Path) -> Path:
    """Build the run's summary from its case files and write it."""
    built = case_summary.build_summary(evidence_dir, label=LABEL, profile=PROFILE)
    return case_summary.write_summary(evidence_dir, built)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    lab_root = Path(__file__).resolve().parent.parent
    parser.add_argument("--lab-root", type=Path, default=lab_root)
    parser.add_argument(
        "--bind",
        type=Path,
        metavar="EVIDENCE_DIR",
        help="stamp this fresh directory before any scenario runs",
    )
    parser.add_argument(
        "--record-case",
        nargs=4,
        type=str,
        metavar=("EVIDENCE_DIR", "NAME", "VERDICT", "LOG"),
        help="record one scenario verdict as a case file",
    )
    parser.add_argument(
        "--write-summary",
        type=Path,
        metavar="EVIDENCE_DIR",
        help="build and write the run's summary from its case files",
    )
    args = parser.parse_args(argv)
    if args.bind is not None:
        print(bind(args.bind, args.lab_root))
        return 0
    if args.record_case is not None:
        evidence_dir, name, verdict, log = args.record_case
        path = record_case(Path(evidence_dir), name, verdict, log)
        print(path if path is not None else f"{verdict} {name}: no case file")
        return 0
    if args.write_summary is not None:
        print(write_summary(args.write_summary))
        return 0
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
