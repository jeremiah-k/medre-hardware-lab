"""The concurrent probe stays owned through completion or cancellation."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from openhop_peer_send import PeerSender, peer_send_command  # noqa: E402


def test_collect_timeout_still_removes_the_exact_sender(monkeypatch):
    observed = []
    sender = PeerSender()

    def docker(command, **kwargs):
        observed.append(command)
        if command[1] == "wait":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", docker)
    sender.start(
        peer_send_command("openhop-bridge", 2, "p4test", 3.0, "openhop-repeater-b")
    )
    with pytest.raises(subprocess.TimeoutExpired):
        try:
            sender.collect(timeout=1.0)
        finally:
            sender.close()
    assert "--detach" in observed[0]
    assert sender.name == observed[0][observed[0].index("--name") + 1]
    assert observed[1:] == [
        ["docker", "wait", sender.name],
        ["docker", "rm", "-f", sender.name],
    ]
