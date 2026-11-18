"""One operator interface for simulated, physical and hybrid qualification."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .config import load, pair


def root_path(value):
    root = Path(value).resolve()
    if (
        not (root / "compose.yaml").is_file()
        or not (root / "sources.lock.json").is_file()
    ):
        raise ValueError("--root must name this lab repository checkout")
    return root


def doctor(config):
    result = {
        "bluetooth_controllers": [],
        "serial_devices": [],
        "tools": {
            t: shutil.which(t) is not None for t in ("docker", "git", "bluetoothctl")
        },
    }
    for path in sorted(Path("/sys/class/bluetooth").glob("hci*")):
        result["bluetooth_controllers"].append(path.name)
    result["serial_devices"] = [
        str(p) for p in sorted(Path("/dev/serial/by-id").glob("*"))
    ]
    result["configuration_present"] = config.exists()
    if config.exists():
        cfg = load(config)
        problems = []
        for kind, table in cfg["hardware"].items():
            if kind in ("meshtastic", "lxmf"):
                for field in (
                    ("ports",) if kind == "meshtastic" else ("profiles", "identities")
                ):
                    for value in table[field]:
                        if not Path(value).exists():
                            problems.append(f"missing {kind}.{field} path")
        selected = cfg["hardware"].get("ble_adapter")
        if selected and selected not in result["bluetooth_controllers"]:
            problems.append("selected Bluetooth controller is absent")
        result["problems"] = problems
    print(json.dumps(result, indent=2))
    return 1 if result.get("problems") else 0


def _spawn_worker(argv: list[str], log_path: Path, timeout: float, watch=None) -> int:
    """Run one owned worker subprocess with a bounded interrupt/kill ladder.

    ``watch`` receives the live child handle once and runs detached; it must
    return when the child exits so the thread never outlives the worker.
    """
    with log_path.open("w") as log:
        child = subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if watch is not None:
            threading.Thread(target=watch, args=(child,), daemon=True).start()
        try:
            return child.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(child.pid, signal.SIGINT)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            return 1


def _hardware_run_dir(root: Path, transports, role: str, run_root: Path | None) -> Path:
    base = run_root if run_root is not None else root / "evidence"
    return base / (
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        + "-"
        + "-".join(transports)
        + "-"
        + role
        + "-"
        + uuid.uuid4().hex[:6]
    )


def _write_worker_absent_verdict(run: Path, name: str, phase: str, code: int) -> None:
    path = run / name
    if not path.exists():
        path.write_text(
            json.dumps(
                {
                    "phase": phase,
                    "pass": False,
                    "stage": "worker-exited-before-verdict",
                    "exit_code": code,
                    "details": "See private worker log; no qualification claim.",
                },
                indent=2,
            )
            + "\n"
        )


def hardware(root, config, selected, role, run_root: str | None = None):
    cfg = load(config)
    transports = pair(selected)
    if any(t not in cfg["hardware"] for t in transports):
        raise ValueError("configure both transports before running this pair")
    run = _hardware_run_dir(
        root, transports, role, Path(run_root) if run_root else None
    )
    run.mkdir(parents=True, mode=0o700)
    timeout = (
        cfg["hardware"]["startup_timeout"]
        + 2 * (cfg["hardware"]["send_timeout"] + cfg["hardware"]["observation_timeout"])
        + 180
    )
    code = _spawn_worker(
        [
            sys.executable,
            "-m",
            "medre_lab.worker",
            str(config.resolve()),
            ":".join(transports),
            str(0 if role == "a" else 1),
            str(run),
        ],
        run / "worker.log",
        timeout,
    )
    if not (run / "verdict.json").exists():
        (run / "verdict.json").write_text(
            json.dumps(
                {
                    "pass": False,
                    "stage": "worker-exited-before-verdict",
                    "exit_code": code,
                    "details": "See private worker.log; no qualification claim.",
                },
                indent=2,
            )
            + "\n"
        )
        code = 1
    print(json.dumps({"pass": code == 0, "evidence": str(run)}))
    return code


def _auto_disconnect_watch(run: Path, owned_address: str):
    """Perform the declared-link disconnect when the baseline arms the stage.

    The watcher is the harness-owned interruption action: it waits for the
    worker's interrupt sentinel, disconnects the MEDRE-owned MeshCore host
    link once, and records the action. The worker observes the actual
    disconnect and rejoin through adapter diagnostics. No setting, bond,
    key, or firmware state is touched; disconnecting an owned BLE host link
    is the declared fault mechanism.
    """

    def watch(child) -> None:
        log_path = run / "disconnect-action.log"
        performed = False
        while child.poll() is None:
            if not performed and (run / "interrupt-armed.json").exists():
                performed = True
                try:
                    proc = subprocess.run(
                        ["bluetoothctl", "disconnect", owned_address],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    log_path.write_text(
                        f"$ bluetoothctl disconnect {owned_address}\n"
                        f"exit={proc.returncode}\n{proc.stdout}{proc.stderr}"
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    log_path.write_text(f"disconnect action failed: {exc}")
            time.sleep(1.0)

    return watch


def continuity(
    root,
    config,
    selected,
    role,
    interrupt_wait: float,
    auto_disconnect: bool,
    run_root: str | None,
):
    """Two-phase continuity case: baseline owner, then fresh-process reopen."""
    cfg = load(config)
    transports = pair(selected)
    if any(t not in cfg["hardware"] for t in transports):
        raise ValueError("configure both transports before running this pair")
    if "meshcore" not in transports:
        raise ValueError(
            "continuity link interruption currently supports the meshcore BLE host link"
        )
    owned_address = cfg["hardware"]["meshcore"]["addresses"][0 if role == "a" else 1]
    run = _hardware_run_dir(
        root, transports, role, Path(run_root) if run_root else None
    )
    run.mkdir(parents=True, mode=0o700)
    hw = cfg["hardware"]
    baseline_timeout = (
        hw["startup_timeout"]
        + 3 * (hw["send_timeout"] + hw["observation_timeout"])
        + interrupt_wait
        + 700
    )
    reopen_timeout = (
        hw["startup_timeout"]
        + 2 * (hw["send_timeout"] + hw["observation_timeout"])
        + 900
    )
    boundary: dict = {}
    phases_ok = True
    for phase, timeout in (("baseline", baseline_timeout), ("reopen", reopen_timeout)):
        argv = [
            sys.executable,
            "-m",
            "medre_lab.worker",
            str(config.resolve()),
            ":".join(transports),
            str(0 if role == "a" else 1),
            str(run),
            phase,
        ]
        if phase == "baseline":
            argv.append(str(interrupt_wait))
        started = time.time()
        watch = (
            _auto_disconnect_watch(run, owned_address)
            if phase == "baseline" and auto_disconnect
            else None
        )
        code = _spawn_worker(argv, run / f"{phase}-worker.log", timeout, watch=watch)
        boundary[phase] = {
            "exit_code": code,
            "started_at": started,
            "ended_at": time.time(),
            "timeout_seconds": timeout,
            "pass": False,
            "log": str(run / f"{phase}-worker.log"),
        }
        _write_worker_absent_verdict(run, f"{phase}-verdict.json", phase, code)
        verdict = json.loads((run / f"{phase}-verdict.json").read_text())
        boundary[phase]["pass"] = bool(verdict.get("pass"))
        phases_ok = phases_ok and boundary[phase]["pass"]
    merged = {
        "case": "reconnect-reopen-continuity",
        "pass": phases_ok,
        "pair": list(transports),
        "role": role,
        "process_boundary": boundary,
        "scope": "Two-phase continuity case; physical interruption and MEDRE "
        "shutdown are separate evidenced actions.",
    }
    (run / "continuity-verdict.json").write_text(
        json.dumps(merged, indent=2, default=str) + "\n"
    )
    print(json.dumps({"pass": phases_ok, "evidence": str(run)}))
    return 0 if phases_ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="lab repository checkout")
    parser.add_argument("--config", default="lab.toml", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "init", help="copy local config template, without hardware writes"
    )
    commands.add_parser("doctor", help="read-only host/config inventory; no RF")
    sources = commands.add_parser("sources").add_subparsers(
        dest="action", required=True
    )
    sources.add_parser(
        "fetch", help="clone the exact Docker source locks into sources/"
    )
    docker = commands.add_parser("docker").add_subparsers(dest="action", required=True)
    docker.add_parser("prepare")
    up = docker.add_parser("up")
    up.add_argument("--profile", choices=("base", "normal", "stress"), default="normal")
    up.add_argument("--skip-build", action="store_true")
    suite = docker.add_parser("test")
    suite.add_argument("scenarios", nargs="*", default=[])
    docker.add_parser("down")
    hw = commands.add_parser("hardware").add_subparsers(dest="action", required=True)
    run = hw.add_parser("run")
    run.add_argument("--pair", required=True)
    run.add_argument("--role", choices=("a", "b"), default="a")
    run.add_argument(
        "--run-root",
        default=None,
        help="private directory for the run (default: <root>/evidence)",
    )
    cont = hw.add_parser(
        "continuity",
        help="opt-in disconnect/reopen continuity case (two owned phases)",
    )
    cont.add_argument("--pair", required=True)
    cont.add_argument("--role", choices=("a", "b"), default="a")
    cont.add_argument(
        "--interrupt-wait",
        type=float,
        default=300.0,
        help="seconds the baseline waits for the declared link to be disconnected",
    )
    cont.add_argument(
        "--no-auto-disconnect",
        action="store_true",
        help="leave the interruption to the operator; the harness only records",
    )
    cont.add_argument(
        "--run-root",
        default=None,
        help="private directory for the run (default: <root>/evidence)",
    )
    args = parser.parse_args(argv)
    try:
        root = root_path(args.root)
        config = args.config if args.config.is_absolute() else root / args.config
        if args.command == "init":
            if config.exists():
                raise ValueError("config already exists; edit it explicitly")
            shutil.copyfile(root / "examples/lab.toml", config)
            config.chmod(0o600)
            print(config)
            return 0
        if args.command == "doctor":
            return doctor(config)
        if args.command == "hardware":
            if args.action == "run":
                return hardware(root, config, args.pair, args.role, args.run_root)
            return continuity(
                root,
                config,
                args.pair,
                args.role,
                args.interrupt_wait,
                not args.no_auto_disconnect,
                args.run_root,
            )
        from . import docker as rig

        if args.command == "sources":
            rig.fetch(root)
            return 0
        if args.action == "down":
            rig.assert_no_other_rig(root)
            return subprocess.call(["bash", "lab/down.sh"], cwd=root)
        env = {**os.environ, **rig.prepare(root)}
        if args.action == "prepare":
            return 0
        rig.assert_no_other_rig(root)
        if args.action == "up":
            command = ["bash", "lab/up.sh"]
            if args.profile != "base":
                command += ["--normal" if args.profile == "normal" else "--with-medre"]
            if args.skip_build:
                command += ["--skip-build"]
        else:
            command = ["bash", "lab/run-scenarios.sh", *args.scenarios]
        return subprocess.call(command, cwd=root, env=env)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Lab setup failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
