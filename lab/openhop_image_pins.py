#!/usr/bin/env python3
"""Check baked openHop image selections against the selected source lock."""

import argparse
import json
import subprocess
from pathlib import Path

REPEATER = ("openhop-core", "openhop-repeater")
MEDRE = ("medre-openhop", "medre-adapter-meshcore", "medre-adapter-meshtastic-openhop")


def check_output(*command: str) -> str:
    return subprocess.check_output(command, text=True, timeout=30).strip()


def check(image: str, keys: tuple[str, ...], pins: dict) -> None:
    labels = (
        json.loads(
            check_output(
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                image,
            )
        )
        or {}
    )
    for key in keys:
        if labels.get(f"io.medre.lab.pin.{key}") != pins[key]["ref"]:
            raise ValueError(
                f"{image}: baked {key} pin is missing or different; rebuild the selected image"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--running",
        action="store_true",
        help="inspect the images actually used by containers",
    )
    parser.add_argument("--repeater", action="append", default=[])
    parser.add_argument("--medre", action="append", default=[])
    args = parser.parse_args()
    pins = json.loads(Path("sources.lock.json").read_text(encoding="utf-8"))
    try:
        for names, keys in ((args.repeater, REPEATER), (args.medre, MEDRE)):
            for name in names:
                image = (
                    check_output(
                        "docker",
                        "inspect",
                        "--type",
                        "container",
                        "--format",
                        "{{.Image}}",
                        name,
                    )
                    if args.running
                    else name
                )
                check(image, keys, pins)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"OpenHop image provenance failed: {exc}") from exc


if __name__ == "__main__":
    main()
