"""Receiver lifetime starts after startup and drain, with bounded leg budgets."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from medre_lab import worker


@pytest.mark.parametrize("drain_failure", [False, True])
async def test_receivers_start_after_drain_and_close_before_stop(
    monkeypatch, tmp_path, drain_failure
):
    elapsed = 0.0
    events = []
    receivers = []
    sent_nonce = None
    config = {
        "hardware": {
            "startup_timeout": 30,
            "send_timeout": 10,
            "observation_timeout": 30,
        }
    }
    app = SimpleNamespace(adapters={"meshtastic": SimpleNamespace()})

    async def start():
        nonlocal elapsed
        elapsed += 30
        events.append("start")

    async def health_check():
        nonlocal elapsed
        elapsed += 15
        events.append("healthy")
        return SimpleNamespace(health="healthy")

    async def stop():
        assert all(not r.active for r in receivers)
        events.append("stop")

    async def drain(candidate):
        nonlocal elapsed
        assert candidate is app
        elapsed += 120
        events.append("drain")
        if drain_failure:
            raise RuntimeError("drain did not retire")
        return [{"adapter": "meshtastic", "outcome": "retired"}]

    class Receiver:
        def __init__(self, lifetime):
            assert events[-1] in {"drain", "closed"}
            self.created_at = elapsed
            self.deadline = elapsed + lifetime
            self.active = False
            receivers.append(self)

        def __enter__(self):
            self.active = True
            events.append("listening")
            return self

        def __exit__(self, *exc):
            self.active = False
            events.append("closed")

        def packets_until(self, predicate, timeout):
            nonlocal elapsed
            assert timeout == config["hardware"]["observation_timeout"]
            elapsed += timeout
            assert self.active and elapsed <= self.deadline
            packets = [{"text": sent_nonce}]
            assert predicate(packets)
            return packets

    def send(nonce):
        nonlocal elapsed, sent_nonce
        assert receivers[-1].active
        elapsed += config["hardware"]["send_timeout"]
        sent_nonce = nonce
        return []

    def verify(run, legs):
        assert events[-1] == "stop"
        assert len(legs) == 2
        for leg in legs:
            leg["sent_receipt"] = True
        events.append("verified")

    app.start = start
    app.stop = stop
    app.adapters["meshtastic"].health_check = health_check
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: elapsed))
    monkeypatch.setattr(worker, "provenance", lambda transports: {})
    monkeypatch.setattr(worker, "own", lambda endpoints: nullcontext())
    monkeypatch.setattr(worker, "build", lambda *args: app)
    monkeypatch.setattr(worker, "verify_receipts", verify)
    monkeypatch.setattr(worker, "wait_startup_drain_retired", drain)
    monkeypatch.setattr(
        worker,
        "configure",
        lambda *args: (
            dict(meshtastic=Receiver, meshcore=Receiver),
            dict(meshtastic=send, meshcore=send),
            {},
            [],
        ),
    )
    passed = await worker.run_pair(config, ("meshtastic", "meshcore"), 0, tmp_path)
    result = json.loads((tmp_path / "verdict.json").read_text())
    assert passed is result["pass"] is (not drain_failure)
    if drain_failure:
        assert receivers == []
        assert events == ["start", "healthy", "drain", "stop"]
        assert "drain did not retire" in result["error"]
    else:
        assert len(receivers) == 2
        assert receivers[0].created_at == 165
        assert events == [
            "start",
            "healthy",
            "drain",
            "listening",
            "closed",
            "listening",
            "closed",
            "stop",
            "verified",
        ]
        assert result["startup_drain_wait"] == [
            {"adapter": "meshtastic", "outcome": "retired"}
        ]
        assert all(leg["sent_receipt"] for leg in result["legs"])
