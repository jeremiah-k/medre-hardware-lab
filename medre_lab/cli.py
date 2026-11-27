"""One operator interface for simulated, physical and hybrid qualification."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .config import load, pair
from .continuity import _write_json


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


def _spawn_worker(argv: list[str], log_path: Path, timeout: float) -> int:
    """Run one owned worker subprocess with a bounded interrupt/kill ladder."""
    with log_path.open("w") as log:
        child = subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return child.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            os.killpg(child.pid, signal.SIGINT)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            return 130 if isinstance(exc, KeyboardInterrupt) else 1


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
        _write_json(
            path,
            {
                "phase": phase,
                "pass": False,
                "stage": "worker-exited-before-verdict",
                "exit_code": code,
                "details": "See private worker log; no qualification claim.",
            },
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
        _write_worker_absent_verdict(run, "verdict.json", "hardware", code)
        if code != 130:
            code = 1
    print(json.dumps({"pass": code == 0, "evidence": str(run)}))
    return code


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
    if not math.isfinite(interrupt_wait) or interrupt_wait <= 0:
        raise ValueError("interrupt wait must be finite and positive")
    cfg = load(config)
    transports = pair(selected)
    if any(t not in cfg["hardware"] for t in transports):
        raise ValueError("configure both transports before running this pair")
    if "meshcore" not in transports:
        raise ValueError(
            "continuity link interruption currently supports the meshcore BLE host link"
        )
    run = _hardware_run_dir(
        root, transports, role, Path(run_root) if run_root else None
    )
    run.mkdir(parents=True, mode=0o700)
    # Both workers use a private, resolved copy of the validated settings.
    # The original operator TOML may change while the baseline is running.
    from .config import write_effective_config

    snapshot = run / "effective-config.toml"
    write_effective_config(snapshot, cfg)
    hw = cfg["hardware"]
    # Include each native send plus both independent and durable observation
    # waits for healthy legs, pending capture/disposition and fixed fault waits.
    baseline_timeout = (
        hw["startup_timeout"]
        + 3 * (hw["send_timeout"] + hw["observation_timeout"])
        + interrupt_wait
        + 700
    )
    reopen_timeout = (
        hw["startup_timeout"]
        + 2 * hw["send_timeout"]
        + 5 * hw["observation_timeout"]
        + 900
    )
    boundary: dict = {}
    phases_ok = True
    for phase, timeout in (("baseline", baseline_timeout), ("reopen", reopen_timeout)):
        argv = [
            sys.executable,
            "-m",
            "medre_lab.worker",
            str(snapshot),
            ":".join(transports),
            str(0 if role == "a" else 1),
            str(run),
            phase,
        ]
        if phase == "baseline":
            argv.append(str(interrupt_wait))
            if auto_disconnect:
                argv.append("--auto-disconnect")
        started = time.time()
        code = _spawn_worker(argv, run / f"{phase}-worker.log", timeout)
        boundary[phase] = {
            "exit_code": code,
            "started_at": started,
            "ended_at": time.time(),
            "timeout_seconds": timeout,
            "pass": False,
            "log": str(run / f"{phase}-worker.log"),
        }
        _write_worker_absent_verdict(run, f"{phase}-verdict.json", phase, code)
        try:
            verdict = json.loads((run / f"{phase}-verdict.json").read_text())
            if not isinstance(verdict, dict) or verdict.get("phase") != phase:
                raise ValueError("invalid phase verdict")
            boundary[phase]["worker_verdict_pass"] = verdict.get("pass") is True
        except (ValueError, OSError) as exc:
            boundary[phase]["worker_verdict_pass"] = False
            boundary[phase]["verdict_error"] = type(exc).__name__
        boundary[phase]["pass"] = code == 0 and boundary[phase]["worker_verdict_pass"]
        phases_ok = phases_ok and boundary[phase]["pass"]
        if not boundary[phase]["pass"]:
            break  # Never open a replacement after cancellation or failed retirement.
    merged = {
        "case": "reconnect-reopen-continuity",
        "pass": phases_ok,
        "pair": list(transports),
        "role": role,
        "process_boundary": boundary,
        "scope": "Two-phase continuity case; physical interruption and MEDRE "
        "shutdown are separate evidenced actions.",
    }
    _write_json(run / "continuity-verdict.json", merged)
    print(json.dumps({"pass": phases_ok, "evidence": str(run)}))
    return 130 if code == 130 else (0 if phases_ok else 1)


def mixed_continuity(
    root,
    matrix_json: str,
    role: str,
    run_root: str | None,
    burst_count: int,
    hardware_toml: Path,
) -> int:
    """Mixed-transport A-3 continuity case: two owned worker phases."""
    from .mixed_continuity import effective_fingerprint, load_mixed, verify_controller

    if not 1 <= burst_count <= 6:
        raise ValueError("burst-count must be between 1 and 6")
    matrix_path = Path(matrix_json).expanduser()
    if not matrix_path.is_file():
        raise ValueError(f"campaign matrix file not found: {matrix_path}")
    config = load_mixed(hardware_toml, matrix_path)
    hw = config["hardware"]
    hw["ble_adapter"] = verify_controller(hw)
    config["matrix"]["fingerprint"] = effective_fingerprint(config)
    transports = ("matrix", "meshcore", "meshtastic")
    run = _hardware_run_dir(
        root, transports, role, Path(run_root) if run_root else None
    )
    run.mkdir(parents=True, mode=0o700)
    snapshot = run / "effective-mixed-config.json"
    # Private directory and atomic publication; never copy credentials to logs.
    _write_json(snapshot, config)
    snapshot.chmod(0o600)
    baseline_timeout = (
        hw["startup_timeout"]
        + (5 + burst_count) * hw["send_timeout"]
        + 7 * hw["observation_timeout"]
        + 240
        + 900
    )
    reopen_timeout = (
        hw["startup_timeout"]
        + 2 * hw["send_timeout"]
        + 3 * hw["observation_timeout"]
        + 900
    )
    boundary: dict = {}
    phases_ok = True
    code = 0
    for phase, timeout in (
        ("mixed-baseline", baseline_timeout),
        ("mixed-reopen", reopen_timeout),
    ):
        argv = [
            sys.executable,
            "-m",
            "medre_lab.mixed_worker",
            "--effective-config",
            str(snapshot),
            str(0 if role == "a" else 1),
            str(run),
            phase,
        ]
        if phase == "mixed-baseline":
            argv.append(str(burst_count))
        started = time.time()
        code = _spawn_worker(argv, run / f"{phase}-worker.log", timeout)
        boundary[phase] = {
            "exit_code": code,
            "duration_seconds": round(time.time() - started, 3),
            "timeout_seconds": timeout,
            "pass": False,
            "log": str(run / f"{phase}-worker.log"),
        }
        _write_worker_absent_verdict(run, f"{phase}-verdict.json", phase, code)
        try:
            verdict = json.loads((run / f"{phase}-verdict.json").read_text())
            if not isinstance(verdict, dict) or verdict.get("phase") != phase:
                raise ValueError("invalid phase verdict")
            boundary[phase]["worker_verdict_pass"] = verdict.get("pass") is True
        except (ValueError, OSError) as exc:
            boundary[phase]["worker_verdict_pass"] = False
            boundary[phase]["verdict_error"] = type(exc).__name__
        boundary[phase]["pass"] = code == 0 and boundary[phase]["worker_verdict_pass"]
        phases_ok = phases_ok and boundary[phase]["pass"]
        if not boundary[phase]["pass"]:
            break  # Never open a replacement after cancellation or failed retirement.
    merged = {
        "case": "mixed-transport-continuity",
        "pass": phases_ok,
        "transports": list(transports),
        "role": role,
        "burst_count": burst_count,
        "process_boundary": boundary,
        "scope": "Mixed-transport A-3 case; physical interruption and MEDRE "
        "shutdown are separate evidenced actions.",
    }
    _write_json(run / "continuity-verdict.json", merged)
    print(json.dumps({"pass": phases_ok, "evidence": str(run)}))
    return 130 if code == 130 else (0 if phases_ok else 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="lab repository checkout")
    parser.add_argument("--config", default="lab.toml", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "init", help="copy local config template, without hardware writes"
    )
    commands.add_parser("doctor", help="read-only host/config inventory; no RF")
    preset = commands.add_parser(
        "preset", help="save, inspect and load named rig configurations"
    ).add_subparsers(dest="action", required=True)
    preset.add_parser("list", help="list the saved rig presets")
    preset_show = preset.add_parser("show", help="print one preset's recorded choices")
    preset_show.add_argument("name")
    preset_save = preset.add_parser(
        "save", help="record the current rig configuration as a preset"
    )
    preset_save.add_argument("name")
    preset_save.add_argument(
        "--description",
        default=None,
        help="one-line description (default: derived from the topology)",
    )
    preset_save.add_argument(
        "--force", action="store_true", help="overwrite an existing preset of this name"
    )
    sources = commands.add_parser("sources").add_subparsers(
        dest="action", required=True
    )
    sources.add_parser(
        "fetch", help="clone the exact Docker source locks into sources/"
    )
    docker = commands.add_parser("docker").add_subparsers(dest="action", required=True)
    docker.add_parser("prepare")
    up = docker.add_parser("up")
    up.add_argument(
        "--profile",
        choices=("base", "normal", "stress", "openhop", "openhop-radio"),
        default="normal",
    )
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
    preflight = hw.add_parser(
        "mixed-preflight", help="validate mixed inputs and controller without RF"
    )
    preflight.add_argument("--hardware-toml", required=True, type=Path)
    preflight.add_argument("--matrix-json", required=True, type=Path)
    mixed = hw.add_parser(
        "mixed-continuity",
        help="mixed-transport A-3 case (matrix+meshcore+meshtastic, two owned phases)",
    )
    mixed.add_argument(
        "--matrix-json",
        required=True,
        help="private campaign matrix provisioning file (accounts/rooms/tokens)",
    )
    mixed.add_argument(
        "--hardware-toml",
        required=True,
        type=Path,
        help="private campaign hardware TOML (hci + board endpoints)",
    )
    mixed.add_argument("--role", choices=("a", "b"), default="a")
    mixed.add_argument(
        "--burst-count",
        type=int,
        choices=range(1, 7),
        default=3,
        help="fault-window burst size per route class (1-6)",
    )
    mixed.add_argument(
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
        if args.command == "preset":
            from . import presets

            if args.action == "list":
                return presets.list_presets(root)
            if args.action == "show":
                return presets.show_preset(root, args.name)
            return presets.save_preset(root, args.name, args.description, args.force)
        if args.command == "hardware":
            if args.action == "run":
                return hardware(root, config, args.pair, args.role, args.run_root)
            if args.action == "mixed-preflight":
                from .mixed_continuity import load_mixed, verify_controller

                cfg = load_mixed(args.hardware_toml, args.matrix_json)
                print(
                    json.dumps(
                        {
                            "controller": verify_controller(cfg["hardware"]),
                            "configuration_valid": True,
                        }
                    )
                )
                return 0
            if args.action == "mixed-continuity":
                return mixed_continuity(
                    root,
                    args.matrix_json,
                    args.role,
                    args.run_root,
                    args.burst_count,
                    args.hardware_toml,
                )
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
                command += [
                    {
                        "normal": "--normal",
                        "stress": "--with-medre",
                        "openhop": "--openhop",
                        "openhop-radio": "--openhop-radio",
                    }[args.profile]
                ]
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
