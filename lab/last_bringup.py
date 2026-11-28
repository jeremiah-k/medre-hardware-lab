#!/usr/bin/env python3
"""Record what was last brought up, as lab state for ``preset save``.

``lab/up.sh`` calls this at the end of every bring-up (all three paths:
plain, openHop, openHop virtual radio). The record names the CHOICES the
bring-up ran with -- the topology and the flags -- plus the
``sources.lock.json`` content hash the rig was provisioned from, so a
later ``medre-lab preset save`` can record exactly what is known to have
come up together. It is lab state (``lab/state/`` is gitignored) and it
deliberately records nothing generated: tokens, room ids and manifests
are re-derived from the choices, never copied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Keep in step with medre_lab.presets.TOPOLOGIES (a test pins both sides).
TOPOLOGIES = ("base", "normal", "stress", "openhop", "openhop-radio")

RECORD_RELPATH = Path("lab/state/last-bringup.json")


def lock_sha256(lab_root: Path) -> str:
    return hashlib.sha256(
        (Path(lab_root) / "sources.lock.json").read_bytes()
    ).hexdigest()


def write_record(
    lab_root: Path,
    topology: str,
    flags: list[str],
    recorded_at: str | None = None,
    *,
    expected_lock_sha256: str | None = None,
) -> dict:
    """Write lab/state/last-bringup.json and return the record."""
    if topology not in TOPOLOGIES:
        raise ValueError(
            f"topology {topology!r} must be one of: {', '.join(TOPOLOGIES)}"
        )
    if not (Path(lab_root) / "sources.lock.json").is_file():
        raise ValueError("sources.lock.json is missing; the record describes no rig")
    digest = lock_sha256(lab_root)
    if expected_lock_sha256 is not None and digest != expected_lock_sha256:
        raise ValueError("source lock changed during bring-up")
    record = {
        "topology": topology,
        "flags": list(flags),
        "recorded_at": recorded_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources_lock_sha256": digest,
    }
    target = Path(lab_root) / RECORD_RELPATH
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".last-bringup-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", required=True, choices=TOPOLOGIES)
    parser.add_argument(
        "--flag",
        action="append",
        default=[],
        help="a flag the bring-up ran with (repeatable, in order)",
    )
    parser.add_argument(
        "--lab-root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument("--expected-lock-sha256")
    args = parser.parse_args(argv)
    record = write_record(
        args.lab_root,
        args.topology,
        args.flag,
        expected_lock_sha256=args.expected_lock_sha256,
    )
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
