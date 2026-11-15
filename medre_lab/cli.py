"""One operator interface for simulated, physical and hybrid qualification."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
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


def hardware(root, config, selected, role):
    cfg = load(config)
    transports = pair(selected)
    if any(t not in cfg["hardware"] for t in transports):
        raise ValueError("configure both transports before running this pair")
    run = (
        root
        / "evidence"
        / (
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            + "-"
            + "-".join(transports)
            + "-"
            + role
            + "-"
            + uuid.uuid4().hex[:6]
        )
    )
    run.mkdir(parents=True, mode=0o700)
    timeout = (
        cfg["hardware"]["startup_timeout"]
        + 2 * (cfg["hardware"]["send_timeout"] + cfg["hardware"]["observation_timeout"])
        + 180
    )
    with (run / "worker.log").open("w") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "medre_lab.worker",
                str(config.resolve()),
                ":".join(transports),
                str(0 if role == "a" else 1),
                str(run),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = child.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(child.pid, signal.SIGINT)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            code = 1
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
            return hardware(root, config, args.pair, args.role)
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
