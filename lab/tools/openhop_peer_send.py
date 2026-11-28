"""Own the one-off Compose sender while a matrix observes concurrent traffic."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path


def peer_send_command(
    profile: str, count: int, prefix: str, settle: float, host: str
) -> list[str]:
    """Build the one-shot SDK client command for a marker batch."""
    return [
        "docker",
        "compose",
        "--profile",
        profile,
        "run",
        "--rm",
        "--no-deps",
        "-v",
        f"{Path(__file__).resolve().with_name('openhop_peer_probe.py')}:/opt/openhop-peer-probe/openhop_peer_probe.py:ro",
        "openhop-bridge-probe",
        "python3",
        "/opt/openhop-peer-probe/openhop_peer_probe.py",
        "--mode",
        "send",
        "--host-b",
        host,
        "--port-b",
        "5000",
        "--count",
        str(count),
        "--prefix",
        prefix,
        "--settle",
        str(settle),
    ]


class PeerSender:
    """A detached sender with a unique name and explicit removal on exit."""

    def __init__(self) -> None:
        self.name = f"medre-lab-peer-send-{uuid.uuid4().hex}"
        self.started = False

    def start(self, command: list[str]) -> None:
        # Start and wait use separate calls so terminating an attached Docker
        # client cannot leave a one-off container sending during restoration.
        command = list(command)
        position = command.index("--rm")
        command[position : position + 1] = ["--detach", "--name", self.name]
        self.started = True
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)

    def collect(self, *, timeout: float) -> dict:
        waited = subprocess.run(
            ["docker", "wait", self.name],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        exit_status = int(waited.stdout.strip())
        logs = subprocess.run(
            ["docker", "logs", self.name],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        try:
            result = json.loads(logs.stdout.strip())
        except ValueError as exc:
            raise ValueError("peer sender did not produce a JSON result") from exc
        if not isinstance(result, dict):
            raise ValueError("peer sender result is not a JSON object")
        if exit_status:
            result["pass"] = False
        result["exit_status"] = exit_status
        return result

    def close(self) -> None:
        if not self.started:
            return
        result = subprocess.run(
            ["docker", "rm", "-f", self.name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode and "No such container" not in result.stderr:
            raise RuntimeError("could not remove the owned peer sender")
        self.started = False
