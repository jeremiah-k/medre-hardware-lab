#!/usr/bin/env python3
"""Maintain the lab version record (lab/VERSION).

The record is a YAML mapping of rig sections ("mmrelay", "images",
"openhop_profile", "openhop_bridge", ...). Bring-up scripts used to append
their section blindly, so the file accumulated DUPLICATE keys: an old
section with old pins sat next to the current one and a reader could not
tell which rig was actually running. YAML readers resolve a duplicate key
to the last occurrence, so the file looked right only by accident.

This helper keeps ONE section per key - the most recent write wins - and
gives the bring-up scripts a single place to set, remove or compact
sections.

Usage:
  version_record.py set SECTION [FILE]      # section body on stdin
  version_record.py remove SECTION [FILE]
  version_record.py compact [FILE]
  version_record.py show SECTION [FILE]
  version_record.py list [FILE]
  version_record.py pin_status CHECKOUT REF # classify one pin

The body passed to "set" is the section's indented content; a line that is
not indented already receives the two-space YAML indent. The preamble (the
comment header and anything before the first key) is preserved.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

DEFAULT_FILE = Path("lab/VERSION")
# A top-level key: at column 0, "name:" optionally followed by a value.
KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<value>.*)$")
INDENT = "  "

LOCAL_CANDIDATE = "local-candidate"
ORIGIN_TRACKING = "origin-tracking"


def split_blocks(text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split the record into its preamble and its (key, lines) blocks.

    A block starts at a top-level key line and holds that line plus the
    indented lines that follow it.
    """
    preamble: list[str] = []
    blocks: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        match = KEY_RE.match(line)
        if match and not line.startswith((" ", "\t", "-")):
            current = [line]
            blocks.append((match.group("key"), current))
            continue
        if current is None:
            preamble.append(line)
        else:
            current.append(line)
    return preamble, blocks


def render(preamble: list[str], blocks: list[tuple[str, list[str]]]) -> str:
    out: list[str] = list(preamble)
    while out and not out[-1].strip():
        out.pop()
    for _, lines in blocks:
        # The first line of a block is its key line and stays at column 0;
        # the body lines are indented.
        for index, line in enumerate(lines):
            if not line.strip():
                out.append("")
            elif index == 0 or line.startswith((" ", "\t")):
                out.append(line)
            else:
                out.append(INDENT + line)
    return "\n".join(out).rstrip("\n") + "\n"


def read(path: Path) -> tuple[list[str], list[tuple[str, list[str]]]]:
    if not path.exists():
        return [], []
    return split_blocks(path.read_text(encoding="utf-8"))


def write(path: Path, preamble: list[str], blocks: list[tuple[str, list[str]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(preamble, blocks), encoding="utf-8")


def set_section(path: Path, section: str, body: list[str]) -> None:
    """REPLACE the named section with the given body (append if it is new)."""
    preamble, blocks = read(path)
    blocks = [(key, lines) for key, lines in blocks if key != section]
    blocks.append((section, [f"{section}:"] + body))
    write(path, preamble, blocks)


def cmd_set(section: str, path: Path, body: list[str]) -> int:
    set_section(path, section, body)
    return 0


def cmd_remove(section: str, path: Path) -> int:
    preamble, blocks = read(path)
    blocks = [(key, lines) for key, lines in blocks if key != section]
    write(path, preamble, blocks)
    return 0


def cmd_compact(path: Path) -> int:
    """Keep only the MOST RECENT block of each key, in first-seen order.

    The record used to be appended to, so it accumulated duplicate keys.
    A YAML reader resolves a duplicate to the LAST occurrence, so the
    record only read correctly by accident. Compacting drops the older
    copies and keeps the newest block of every key.
    """
    preamble, blocks = read(path)
    latest: dict[str, list[str]] = {}
    order: list[str] = []
    for key, lines in blocks:
        if key not in latest:
            order.append(key)
        latest[key] = lines
    write(path, preamble, [(key, latest[key]) for key in order])
    return 0


def cmd_show(section: str, path: Path) -> int:
    _, blocks = read(path)
    for key, lines in reversed(blocks):
        if key == section:
            sys.stdout.write("\n".join(lines) + "\n")
            return 0
    return 1


def cmd_list(path: Path) -> int:
    _, blocks = read(path)
    for key, _ in blocks:
        sys.stdout.write(f"{key}\n")
    return 0


def pin_status(checkout: Path, ref: str) -> str:
    """Report local origin-tracking containment for one pin.

    The classification mirrors ``lab/openhop_pins.py``: ``origin-tracking``
    when the checkout's cached ``refs/remotes/origin/`` refs contain the
    commit, ``local-candidate`` otherwise. It describes the checkout's
    local refs and does not assert remote publication; a ref that cannot
    be resolved reports ``unknown``.
    """
    try:
        head = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    if not head:
        return "unknown"
    containing = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "for-each-ref",
            "--contains",
            head,
            "--format=%(refname)",
            "refs/remotes/origin/",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return ORIGIN_TRACKING if containing else LOCAL_CANDIDATE


def cmd_pin_status(checkout: Path, ref: str) -> int:
    sys.stdout.write(pin_status(checkout, ref) + "\n")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    command = argv[1]
    if command == "set":
        if len(argv) < 3:
            print("set needs a section name", file=sys.stderr)
            return 2
        section = argv[2]
        path = Path(argv[3]) if len(argv) > 3 else DEFAULT_FILE
        return cmd_set(section, path, sys.stdin.read().splitlines())
    if command == "remove":
        if len(argv) < 3:
            print("remove needs a section name", file=sys.stderr)
            return 2
        section = argv[2]
        path = Path(argv[3]) if len(argv) > 3 else DEFAULT_FILE
        return cmd_remove(section, path)
    if command == "compact":
        path = Path(argv[2]) if len(argv) > 2 else DEFAULT_FILE
        return cmd_compact(path)
    if command == "show":
        if len(argv) < 3:
            print("show needs a section name", file=sys.stderr)
            return 2
        section = argv[2]
        path = Path(argv[3]) if len(argv) > 3 else DEFAULT_FILE
        return cmd_show(section, path)
    if command == "list":
        path = Path(argv[2]) if len(argv) > 2 else DEFAULT_FILE
        return cmd_list(path)
    if command == "pin_status":
        if len(argv) < 4:
            print("pin_status needs a checkout and a ref", file=sys.stderr)
            return 2
        return cmd_pin_status(Path(argv[2]), argv[3])
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
