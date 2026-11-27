#!/usr/bin/env python3
"""Run the sequential openHop Docker qualification and record each stage."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import openhop_config_access  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DOWN = ["bash", "lab/down.sh"]
LOCK_PATH = ROOT / "lab/state/openhop-qualification.lock"

# The generated repeater configs each bring-up stage mounts; the
# repeater containers read them as uid 1000. The check runs only for
# configs that exist: a bring-up generates and grants a config that is
# absent.
UP_STAGE_CONFIGS = {
    "lifecycle-up": [Path("lab/state/config/openhop-repeater.yaml")],
    "radio-up": [
        Path("lab/state/config/openhop-radio-repeater-a.yaml"),
        Path("lab/state/config/openhop-radio-repeater-b.yaml"),
    ],
    "bridge-up": [
        Path("lab/state/config/openhop-radio-repeater-a.yaml"),
        Path("lab/state/config/openhop-radio-repeater-b.yaml"),
    ],
}

# The open file description holding this checkout's qualification lock.
# flock locks belong to the description, not the path, so a second
# invocation - in this or any other process - is refused while a
# qualification runs, and the kernel releases the lock if the holder
# dies, leaving the file itself harmless.
_qualification_lock = None


def acquire_qualification_lock(path: Path | None = None):
    """Hold an exclusive advisory lock for the life of the process.

    A second qualification stops here with an error naming the lock
    file; the rig-down check runs only after the lock is ours.
    """
    global _qualification_lock
    if _qualification_lock is not None:
        return _qualification_lock
    target = LOCK_PATH if path is None else Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = target.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError(
            f"another qualification already holds {target}; the lock "
            "admits one qualification at a time per checkout"
        )
    handle.seek(0)
    handle.truncate()
    print(f"{os.getpid()}\n", file=handle)
    handle.flush()
    _qualification_lock = handle
    return handle


def stages(evidence: Path, skip_build: bool):
    build = ["--skip-build"] if skip_build else []
    yield "lifecycle-up", ["bash", "lab/up.sh", "--openhop", *build]
    yield "lifecycle", [
        "bash",
        "lab/openhop/lifecycle_check.sh",
        "--evidence-dir",
        str(evidence / "phase2-lifecycle"),
    ]
    yield "lifecycle-down", DOWN
    yield "radio-up", ["bash", "lab/up.sh", "--openhop-radio", *build]
    yield "packet-path", [
        "bash",
        "lab/openhop-radio/packet_proof.sh",
        "--evidence-dir",
        str(evidence / "radio-packet"),
    ]
    yield "radio-down", DOWN
    yield "bridge-up", ["bash", "lab/openhop-bridge/bridge_up.sh", *build]
    yield "bridge-proof", [
        "bash",
        "lab/openhop-bridge/bridge_proof.sh",
        "--evidence-dir",
        str(evidence / "bridge-proof"),
    ]
    for matrix in ("backlog", "ordering", "fault"):
        yield matrix, [
            sys.executable,
            f"lab/tools/openhop_{matrix}_matrix.py",
            "--fail-fast",
            "--evidence-dir",
            str(evidence / matrix),
        ]
    for matrix in ("backlog", "ordering", "fault"):
        yield f"verify-{matrix}", [
            sys.executable,
            "lab/tools/openhop_case_summary.py",
            "--verify-selection",
            str(evidence / matrix),
        ]
    yield "bridge-down", DOWN


def assert_rig_down() -> None:
    if os.environ.get("COMPOSE_PROJECT_NAME", "medre-lab") != "medre-lab":
        raise ValueError("qualification requires the medre-lab Compose project")
    for command in (["ps", "-a"], ["network", "ls"]):
        result = subprocess.run(
            [
                "docker",
                *command,
                "--format",
                "{{.Names}}" if command[0] == "ps" else "{{.Name}}",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if any(name.startswith("medre-lab") for name in result.stdout.splitlines()):
            raise ValueError("a MEDRE lab rig already exists")


def run_command(command: list[str], log) -> int:
    with subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        try:
            result = process.wait()
        except KeyboardInterrupt:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return 130
    return result if result >= 0 else 128 - result


def assert_configs_readable(stage: str) -> None:
    """Refuse a bring-up stage while an existing repeater config it
    mounts is unreadable by the repeater containers; the stage's
    generator writes and grants a config that does not exist yet."""
    for relative in UP_STAGE_CONFIGS.get(stage, []):
        path = ROOT / relative
        if path.exists() and not openhop_config_access.uid_can_read(path):
            raise ValueError(openhop_config_access.refusal(path))


def run_stage(evidence: Path, name: str, command: list[str]) -> int:
    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    print(f"{started.isoformat()} {name}", flush=True)
    log_path = evidence / f"{name}.log"
    with log_path.open("x", encoding="utf-8") as log:
        try:
            status = run_command(command, log)
        except OSError as exc:
            print(exc, file=log)
            status = 1
    record = {
        "stage": name,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.monotonic() - clock, 3),
        "exit_code": status,
        "log": log_path.name,
    }
    with (evidence / "timeline.jsonl").open("a", encoding="utf-8") as timeline:
        print(json.dumps(record), file=timeline)
    print(f"{name} exit={status} ({record['elapsed_seconds']:.3f}s)", flush=True)
    return status


def qualify(evidence: Path, *, skip_build: bool, lock_path: Path | None = None) -> int:
    acquire_qualification_lock(lock_path)
    assert_rig_down()
    evidence.mkdir(mode=0o700, parents=True)
    needs_down = False
    try:
        for name, command in stages(evidence, skip_build):
            # Before the stage starts anything: the refusal must not
            # leave a rig of ours that teardown has to chase.
            assert_configs_readable(name)
            if name.endswith("-up"):
                needs_down = True
            status = run_stage(evidence, name, command)
            if status:
                return status
            if command is DOWN:
                needs_down = False
    finally:
        if needs_down:
            run_stage(evidence, "cleanup", DOWN)
    return 0


def _interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument(
        "--skip-build", action="store_true", help="reuse the selected images"
    )
    args = parser.parse_args()
    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        return qualify(args.evidence_dir.resolve(), skip_build=args.skip_build)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"qualification refused: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
