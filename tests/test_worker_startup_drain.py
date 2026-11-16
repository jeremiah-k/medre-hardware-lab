"""Startup-drain diagnostics and bounds without wall time or native SDKs."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from medre_lab import worker
from medre_lab.worker import wait_startup_drain_retired


class _Clock:
    def __init__(self) -> None:
        self.wall = 1000.0
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.elapsed

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.wall += seconds
        self.elapsed += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(worker, "time", clock)
    # Replace only the worker's namespace, not asyncio's global event-loop clock.
    monkeypatch.setattr(worker, "asyncio", SimpleNamespace(sleep=clock.sleep))
    return clock


class _Adapter:
    def __init__(self, adapter_id: str, state: dict) -> None:
        self.adapter_id = adapter_id
        self.state = state
        self.calls = 0

    def diagnostics(self) -> dict:
        self.calls += 1
        return {"adapter_id": self.adapter_id, **self.state}


class _RearmingAdapter:
    """Mirror the provider's refreshed epoch on failed initial subscription."""

    def __init__(self, clock) -> None:
        self.adapter_id = "meshtastic"
        self.clock = clock
        self.armed = clock.time() - 0.4
        self.window = 0.5
        self.calls = 0
        self.rearmed_at = None

    def diagnostics(self) -> dict:
        self.calls += 1
        if self.calls == 2:
            self.armed = self.clock.time()
            self.rearmed_at = self.armed
        return {
            "adapter_start_epoch": self.armed,
            "startup_packet_drain_seconds": self.window,
        }


class _App:
    def __init__(self, *adapters) -> None:
        self.adapters = {a.adapter_id: a for a in adapters}


@pytest.mark.parametrize("diagnostics", [True, False])
async def test_skips_adapters_without_a_drain_window(clock, diagnostics) -> None:
    adapter = (
        _Adapter("meshcore", {"health": "healthy"})
        if diagnostics
        else SimpleNamespace(adapter_id="meshcore")
    )
    records = await wait_startup_drain_retired(_App(adapter))
    assert records == [
        {
            "adapter": "meshcore",
            "window_seconds": None,
            "outcome": "skipped",
            "reason": "adapter reports no startup drain window",
        }
    ]
    assert clock.sleeps == []
    if diagnostics:
        assert adapter.calls == 1


async def test_skips_disabled_drain_window(clock) -> None:
    adapter = _Adapter(
        "meshtastic",
        {"adapter_start_epoch": clock.time(), "startup_packet_drain_seconds": 0},
    )
    records = await wait_startup_drain_retired(_App(adapter))
    assert records[0]["reason"] == "startup drain disabled"
    assert records[0]["outcome"] == "skipped"
    assert adapter.calls == 1
    assert clock.sleeps == []


async def test_waits_until_armed_window_passes(clock) -> None:
    armed = clock.time() - 0.2
    records = await wait_startup_drain_retired(
        _App(
            _Adapter(
                "meshtastic",
                {"adapter_start_epoch": armed, "startup_packet_drain_seconds": 0.4},
            )
        ),
        poll_interval=0.05,
    )
    assert records[0]["outcome"] == "retired"
    assert records[0]["armed_epoch"] == armed
    assert records[0]["window_seconds"] == 0.4
    assert clock.time() >= armed + 0.4 + worker._DRAIN_EPOCH_MARGIN
    assert records[0]["waited_seconds"] == pytest.approx(clock.elapsed, abs=0.001)
    assert clock.sleeps and all(delay == 0.05 for delay in clock.sleeps)


async def test_rearmed_window_extends_the_wait(clock) -> None:
    adapter = _RearmingAdapter(clock)
    records = await wait_startup_drain_retired(_App(adapter), poll_interval=0.05)
    assert records[0]["outcome"] == "retired"
    assert adapter.rearmed_at is not None
    assert records[0]["armed_epoch"] == adapter.rearmed_at
    assert clock.time() >= (
        adapter.rearmed_at + adapter.window + worker._DRAIN_EPOCH_MARGIN
    )
    assert clock.elapsed >= adapter.window


@pytest.mark.parametrize("missing_epoch", [False, True])
async def test_unretiring_drain_fails_with_diagnostics_snapshot(clock, missing_epoch):
    class UnretiringAdapter:
        adapter_id = "meshtastic"

        def diagnostics(self):
            return {
                "adapter_start_epoch": None if missing_epoch else clock.time(),
                "startup_packet_drain_seconds": 0.3,
                "startup_drain_packets_dropped": 4,
            }

    with pytest.raises(RuntimeError, match="did not retire") as excinfo:
        await wait_startup_drain_retired(
            _App(UnretiringAdapter()),
            poll_interval=0.05,
            window_factor=2.0,
            window_slack=0.1,
        )
    assert "adapter meshtastic" in str(excinfo.value)
    assert "startup_drain_packets_dropped" in str(excinfo.value)
    assert 0.7 <= clock.elapsed < 0.8


async def test_mixed_adapters_only_wait_draining_ones(clock) -> None:
    armed = clock.time() - 1.0
    records = await wait_startup_drain_retired(
        _App(
            _Adapter(
                "meshtastic",
                {"adapter_start_epoch": armed, "startup_packet_drain_seconds": 0.2},
            ),
            _Adapter("lxmf", {"health": "healthy"}),
        )
    )
    assert [r["outcome"] for r in records] == ["retired", "skipped"]
    assert clock.sleeps == []
