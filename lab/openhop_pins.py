#!/usr/bin/env python3
"""Record the openHop source pins for the lab version record.

The record must state the pins the rig ACTUALLY ran on, not the pins
any other bring-up left behind. This script derives them from two independent
sources and only prints them when they agree:

  * the source manifest (lab/state/sources-openhop.json), which names the
    ref every source is expected to be checked out at, and
  * the live checkout, whose HEAD and cached origin-ref containment say
    what is really there.

Every pin is classified against cached origin-tracking refs only. Neither
presence nor absence in these refs proves remote publication. Local
seed refs are never evidence of upstream publication.

All openHop rigs (Phase 2, the virtual-radio profile and the bridge) share
ONE set of source checkouts, so the pins live in ONE record section,
"openhop_sources". The per-rig sections point at it instead of repeating
a pin list that can go stale.

Usage:
  openhop_pins.py MANIFEST [--record FILE]

Without --record the pin lines are printed (YAML, indented, ready for the
version record). With --record the "openhop_sources" section of FILE is
REPLACED with them. A mismatch between the manifest ref and the checkout
HEAD is reported on stderr and makes the exit status 1.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from version_record import set_section  # noqa: E402

# Manifest module prefixes -> the record's key names.
MODULE_KEYS = (
    ("openhop_core", "openhop_core"),
    ("repeater", "openhop_repeater"),
    ("medre.core", "medre"),
    ("medre.adapters.meshcore", "adapter_meshcore"),
    ("medre.adapters.meshtastic", "adapter_meshtastic"),
)
# The order the record uses.
ORDER = (
    "openhop_core",
    "openhop_repeater",
    "medre",
    "adapter_meshcore",
    "adapter_meshtastic",
)
SECTION = "openhop_sources"


def git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    ).stdout.strip()


def head_of(checkout: Path) -> str:
    return git(checkout, "rev-parse", "HEAD")


def status_of(checkout: Path, commit: str) -> str:
    """Report local origin-tracking containment, not remote publication."""
    containing = git(
        checkout,
        "for-each-ref",
        "--contains",
        commit,
        "--format=%(refname)",
        "refs/remotes/origin/",
    )
    return "origin-tracking" if containing else "local-candidate"


def manifest_entries(manifest_path: Path) -> dict[str, dict]:
    """Map each source module to its manifest entry."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries: dict[str, dict] = {}
    for _image, mounts in data.items():
        for mount in mounts:
            module = mount["module"]
            for prefix, key in MODULE_KEYS:
                if module == prefix or module.startswith(prefix + "."):
                    seen = entries.get(key)
                    if seen is not None and any(
                        seen[field] != mount[field] for field in ("source", "ref")
                    ):
                        raise ValueError(f"conflicting source pins for {key}")
                    entries[key] = mount
                    break
    return entries


def pin_lines(manifest_path: Path) -> tuple[list[str], int]:
    """Return the record's pin lines and a non-zero status on mismatch."""
    entries = manifest_entries(manifest_path)
    status = 0
    lines: list[str] = []
    for key in ORDER:
        entry = entries.get(key)
        if entry is None:
            print(f"ERROR: {key} is missing from {manifest_path}", file=sys.stderr)
            status = 1
            continue
        checkout = Path(entry["source"])
        manifest_ref = entry["ref"]
        if (
            Path(git(checkout, "rev-parse", "--show-toplevel")).resolve()
            != checkout.resolve()
        ):
            raise ValueError(f"{key} is not a checkout root")
        head = head_of(checkout)
        if head != manifest_ref:
            print(
                f"ERROR: {key} is at {head} but the manifest pins {manifest_ref}",
                file=sys.stderr,
            )
            status = 1
        if git(checkout, "status", "--porcelain"):
            print(f"ERROR: {key} has uncommitted source changes", file=sys.stderr)
            status = 1
        pin_status = status_of(checkout, head or manifest_ref)
        lines.append(f"  {key}: {checkout} @ {head or manifest_ref} ({pin_status})")
    return lines, status


def main(argv: list[str]) -> int:
    args = argv[1:]
    record: Path | None = None
    if "--record" in args:
        index = args.index("--record")
        record = Path(args[index + 1])
        del args[index : index + 2]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2

    try:
        lines, status = pin_lines(Path(args[0]))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(
            f"ERROR: cannot verify source pins ({type(exc).__name__})", file=sys.stderr
        )
        return 1
    for line in lines:
        print(line)
    if record is not None and status == 0:
        set_section(record, SECTION, lines)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
