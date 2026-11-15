#!/usr/bin/env python3
"""Fail qualification if a pinned source checkout or its live import is absent.

Read-only: no checkouts, containers or runtime state are modified. The manifest
must come from the qualification plan, independently of the observed HEADs.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def git(source: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(source), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def check_source(entry: dict[str, str]) -> Path:
    source = Path(entry["source"]).resolve(strict=True)
    if Path(git(source, "rev-parse", "--show-toplevel")).resolve() != source:
        raise ValueError(f"{source}: expected a checkout root")
    revision = entry["ref"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("qualification refs must be full lowercase commit SHAs")
    if git(source, "rev-parse", "HEAD") != revision:
        raise ValueError(f"{source}: HEAD differs from qualification pin {revision}")
    if git(source, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError(f"{source}: tracked files are modified")
    relative = Path(entry["file"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("module file must stay inside its source checkout")
    file = (source / relative).resolve(strict=True)
    if not file.is_relative_to(source) or not file.is_file():
        raise ValueError(f"{source}: module file is missing or outside checkout")
    git(source, "ls-files", "--error-unmatch", str(relative))
    return source


def check_container(container: str, entries: list[dict[str, str]]) -> None:
    inspected = json.loads(
        subprocess.check_output(["docker", "inspect", container], text=True)
    )[0]
    if not inspected["State"]["Running"]:
        raise ValueError(f"{container}: container is not running")
    mounts = {m["Destination"]: m for m in inspected["Mounts"]}
    for entry in entries:
        source = check_source(entry)
        mount = mounts.get(entry["target"])
        if (
            not mount
            or mount["Type"] != "bind"
            or Path(mount["Source"]).resolve() != source
            or mount["RW"]
        ):
            raise ValueError(
                f"{container}: incorrect read-only source mount {entry['target']}"
            )
        expected = str(Path(entry["target"]) / entry["file"])
        code = (
            "import importlib,pathlib,sys; "
            "m=importlib.import_module(sys.argv[1]); "
            "actual=pathlib.Path(m.__file__).resolve(); "
            "expected=pathlib.Path(sys.argv[2]).resolve(strict=True); "
            "assert actual == expected, (str(actual), str(expected)); "
            "print(str(actual))"
        )
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "python",
                "-c",
                code,
                entry["module"],
                expected,
            ],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--host-only", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if not isinstance(manifest, dict) or not manifest:
        raise SystemExit("source manifest must name the containers under qualification")
    try:
        for container, entries in manifest.items():
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"{container}: empty source selection")
            if args.host_only:
                for entry in entries:
                    check_source(entry)
            else:
                check_container(container, entries)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Source provenance failed: {exc}") from exc
    print("Qualification source check passed")


if __name__ == "__main__":
    main()
