"""Offline regressions: peer host-BLE-link hygiene at the owning boundary.

No radios and no BlueZ: every case injects a fake command runner or a fake
link object. The ownership contract under test:

- disarm verifies the peer host link actually dropped (bounded wait) and
  heals a lingering link with the documented clean disconnect;
- arm recovers a stale link before spawning the child, or refuses honestly;
- a link that cannot be verified dropped fails honestly — never a padded
  pass — with JSONL evidence recorded in the run directory.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from medre_lab.ble_link import BlueZLink, LinkHygieneError


class FakeBlueZ:
    """Scriptable busctl/bluetoothctl stand-in."""

    def __init__(
        self,
        connected: bool | None = True,
        drop_works: bool = True,
        reads_work: bool = True,
    ) -> None:
        self.connected = connected
        self.drop_works = drop_works
        self.reads_work = reads_work
        self.commands: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs) -> subprocess.CompletedProcess:
        self.commands.append(list(argv))
        if argv[:2] == ["busctl", "get-property"]:
            if not self.reads_work:
                return subprocess.CompletedProcess(argv, 1, "", "no such object")
            state = {True: "true", False: "false", None: "true"}[self.connected]
            return subprocess.CompletedProcess(argv, 0, f"b {state}\n", "")
        if argv[:2] == ["busctl", "call"] or argv[:1] == ["bluetoothctl"]:
            if not self.drop_works:
                return subprocess.CompletedProcess(argv, 1, "", "refused")
            self.connected = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command: {argv}")

    def disconnects(self) -> list[list[str]]:
        return [
            argv
            for argv in self.commands
            if argv[:2] == ["busctl", "call"] or argv[:1] == ["bluetoothctl"]
        ]


def _link(fake: FakeBlueZ, tmp_path: Path) -> BlueZLink:
    return BlueZLink(
        "AA:BB:CC:DD:EE:01",
        "hci0",
        tmp_path,
        runner=fake,
        drop_wait=0.1,
        heal_wait=0.1,
        poll_interval=0.02,
    )


def test_is_connected_reads_busctl_property(tmp_path: Path) -> None:
    fake = FakeBlueZ(connected=True)
    assert _link(fake, tmp_path).is_connected() is True
    fake.connected = False
    assert _link(fake, tmp_path).is_connected() is False
    expected = [
        "busctl",
        "get-property",
        "org.bluez",
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01",
        "org.bluez.Device1",
        "Connected",
    ]
    assert fake.commands == [expected, expected]


def test_is_connected_unreadable_is_none(tmp_path: Path) -> None:
    fake = FakeBlueZ(reads_work=False)
    assert _link(fake, tmp_path).is_connected() is None


def test_read_without_controller_uses_bluetoothctl(tmp_path: Path, monkeypatch) -> None:
    def runner(argv, **kwargs):
        if argv[:1] == ["bluetoothctl"]:
            return subprocess.CompletedProcess(
                argv, 0, "[CHG] Device AA:BB:CC:DD:EE:01 Connected: no\n", ""
            )
        raise AssertionError(f"unexpected command: {argv}")

    link = BlueZLink("AA:BB:CC:DD:EE:01", None, tmp_path, runner=runner, drop_wait=0.1)
    assert link.is_connected() is False


def test_verify_dropped_passes_without_heal_when_already_down(
    tmp_path: Path,
) -> None:
    fake = FakeBlueZ(connected=False)
    record = _link(fake, tmp_path).verify_dropped("disarm")
    assert record["verified"] is True and record["heals"] == 0
    assert fake.disconnects() == []


def test_verify_dropped_heals_lingering_link_with_documented_disconnect(
    tmp_path: Path,
) -> None:
    fake = FakeBlueZ(connected=True)
    record = _link(fake, tmp_path).verify_dropped("disarm")
    assert record["verified"] is True and record["heals"] == 1
    assert fake.disconnects() == [
        [
            "busctl",
            "call",
            "org.bluez",
            "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01",
            "org.bluez.Device1",
            "Disconnect",
        ]
    ]


def test_verify_dropped_second_heal_recovers(tmp_path: Path) -> None:
    fake = FakeBlueZ(connected=True)
    attempts = {"n": 0}

    def runner(argv, **kwargs):
        fake.commands.append(list(argv))
        if argv[:2] == ["busctl", "call"]:
            attempts["n"] += 1
            if attempts["n"] >= 2:
                fake.connected = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        return fake.__call__(argv, **kwargs)

    link = BlueZLink(
        "AA:BB:CC:DD:EE:01",
        "hci0",
        tmp_path,
        runner=runner,
        drop_wait=0.1,
        heal_wait=0.1,
        poll_interval=0.02,
    )
    record = link.verify_dropped("disarm")
    assert record["verified"] is True and record["heals"] == 2


def test_verify_dropped_fails_honestly_when_stuck(tmp_path: Path) -> None:
    fake = FakeBlueZ(connected=True, drop_works=False)
    with pytest.raises(LinkHygieneError, match="did not drop"):
        _link(fake, tmp_path).verify_dropped("disarm")
    lines = (tmp_path / "meshcore-peer-link-hygiene.jsonl").read_text().splitlines()
    record = json.loads(lines[-1])
    assert record["verified"] is False and record["heals"] == 0
    assert record["errors"]


def test_verify_dropped_fails_when_link_unreadable(tmp_path: Path) -> None:
    fake = FakeBlueZ(reads_work=False)
    with pytest.raises(LinkHygieneError, match="unreadable"):
        _link(fake, tmp_path).verify_dropped("disarm")


def test_ensure_available_recovers_stale_link(tmp_path: Path) -> None:
    fake = FakeBlueZ(connected=True)
    record = _link(fake, tmp_path).ensure_available("arm")
    assert record["known_clear"] is True and record["heals"] == 1
    assert len(fake.disconnects()) == 1


def test_ensure_available_refuses_stuck_stale_link(tmp_path: Path) -> None:
    fake = FakeBlueZ(connected=True, drop_works=False)
    with pytest.raises(LinkHygieneError, match="refused the documented disconnect"):
        _link(fake, tmp_path).ensure_available("arm")


def test_ensure_available_proceeds_when_state_unreadable(tmp_path: Path) -> None:
    fake = FakeBlueZ(reads_work=False)
    record = _link(fake, tmp_path).ensure_available("arm")
    assert record["known_clear"] is False and record["heals"] == 0


def test_evidence_log_records_every_stage(tmp_path: Path) -> None:
    fake = FakeBlueZ(connected=False)
    link = _link(fake, tmp_path)
    link.ensure_available("arm")
    link.verify_dropped("disarm")
    records = [
        json.loads(line)
        for line in (tmp_path / "meshcore-peer-link-hygiene.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [record["stage"] for record in records] == ["arm", "disarm"]
    assert records[0]["known_clear"] is True
    assert records[1]["verified"] is True


_FAKE_PEER_SCRIPT = (
    "import sys, time\n"
    "open(sys.argv[5], 'w').write('1')\n"  # ready handshake path
    "time.sleep(30)\n"
)

_FAILING_PEER_SCRIPT = "import sys\nraise SystemExit(3)\n"


def _listener(monkeypatch, script: str, link):
    """Listener with a fake in-process peer child and an injected link."""
    from medre_lab.native import meshcore_live_peer

    monkeypatch.setattr(meshcore_live_peer, "_PEER_SCRIPT", script)
    monkeypatch.setattr(meshcore_live_peer, "_PEER_BLE", "AA:BB:CC:DD:EE:01")
    return meshcore_live_peer.MeshCorePeerListener(30.0, link=link)


class FakeLink:
    """Recording stand-in for BlueZLink at the listener boundary."""

    def __init__(self, probe=None, arm_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._probe = probe
        self._arm_error = arm_error

    def ensure_available(self, stage: str) -> dict:
        self.calls.append(("ensure_available", stage))
        if self._probe is not None:
            self._probe("arm")
        if self._arm_error is not None:
            raise self._arm_error
        return {}

    def verify_dropped(self, stage: str) -> dict:
        self.calls.append(("verify_dropped", stage))
        if self._probe is not None:
            self._probe(stage)
        return {}


def test_listener_arm_checks_link_before_spawn(monkeypatch) -> None:
    order = []

    def probe(stage):
        if stage != "arm":
            return
        listener = getattr(probe, "listener", None)
        if listener is not None:
            order.append((stage, listener._owner.proc is None))

    link = FakeLink(probe=probe)
    listener = _listener(monkeypatch, _FAKE_PEER_SCRIPT, link)
    probe.listener = listener
    try:
        listener.__enter__()
    finally:
        listener.__exit__(None, None, None)
    assert link.calls == [("ensure_available", "arm"), ("verify_dropped", "disarm")]
    assert order == [("arm", True)]


def test_listener_disarm_verifies_drop_after_child_retired(monkeypatch) -> None:
    reaped = {}

    def probe(stage):
        if stage == "disarm":
            assert probe.listener._owner.proc is not None
            reaped["poll"] = probe.listener._owner.proc.poll()

    link = FakeLink(probe=probe)
    listener = _listener(monkeypatch, _FAKE_PEER_SCRIPT, link)
    probe.listener = listener
    try:
        listener.__enter__()
    finally:
        listener.__exit__(None, None, None)
    assert reaped["poll"] is not None


def test_listener_failed_arm_still_verifies_link(monkeypatch) -> None:
    link = FakeLink()
    listener = _listener(monkeypatch, _FAILING_PEER_SCRIPT, link)
    with pytest.raises(AssertionError, match="died before readiness"):
        listener.__enter__()
    assert link.calls == [
        ("ensure_available", "arm"),
        ("verify_dropped", "arm-failed"),
    ]
    assert listener._owner.stdio_closed


def test_listener_refused_stale_link_prevents_spawn(monkeypatch) -> None:
    link = FakeLink(arm_error=LinkHygieneError("stale link refuses"))
    listener = _listener(monkeypatch, _FAKE_PEER_SCRIPT, link)
    with pytest.raises(LinkHygieneError, match="stale link refuses"):
        listener.__enter__()
    assert listener._owner.proc is None
    assert link.calls == [("ensure_available", "arm")]


def test_listener_default_link_binds_env_peer_and_controller(
    tmp_path, monkeypatch
) -> None:
    from medre_lab.native import meshcore_live_peer

    monkeypatch.setattr(meshcore_live_peer, "_PEER_BLE", "AA:BB:CC:DD:EE:02")
    monkeypatch.setenv("MESHCORE_PEER_BLE_ADAPTER", "hci7")
    monkeypatch.setenv("MEDRE_LAB_RUN_DIR", str(tmp_path))
    listener = meshcore_live_peer.MeshCorePeerListener(5.0)
    assert isinstance(listener._link, BlueZLink)
    assert listener._link._controller == "hci7"
    assert listener._link._address == "AA:BB:CC:DD:EE:02"


def test_unverifiable_teardown_is_honest_failure_not_padded_pass(
    tmp_path, monkeypatch
) -> None:
    """A link that stays connected through the heal ladder raises in __exit__."""
    fake = FakeBlueZ(connected=False)

    def runner(argv, **kwargs):
        fake.commands.append(list(argv))
        if argv[:2] == ["busctl", "call"]:
            # A disconnect that reports success but never drops the ACL:
            # the honest reading stays Connected: yes forever.
            return subprocess.CompletedProcess(argv, 0, "", "")
        return fake.__call__(argv, **kwargs)

    class LingeringLink(BlueZLink):
        """Real hygiene against a board whose ACL lingers past the child."""

        def verify_dropped(self, stage: str) -> dict:
            if stage == "disarm":
                fake.connected = True  # child retired, ACL link lingers
            return super().verify_dropped(stage)

    link = LingeringLink(
        "AA:BB:CC:DD:EE:01",
        "hci0",
        tmp_path,
        runner=runner,
        drop_wait=0.1,
        heal_wait=0.1,
        poll_interval=0.02,
    )
    listener = _listener(monkeypatch, _FAKE_PEER_SCRIPT, link)
    listener.__enter__()
    with pytest.raises(LinkHygieneError, match="did not drop"):
        listener.__exit__(None, None, None)
    record = json.loads(
        (tmp_path / "meshcore-peer-link-hygiene.jsonl").read_text().splitlines()[-1]
    )
    assert record["verified"] is False and record["heals"] == 2


@pytest.mark.parametrize("error", ["timeout", "oserror", "nonzero"])
@pytest.mark.parametrize("stage", ["ensure_available", "verify_dropped"])
def test_heal_command_failure_rechecks_and_records_drop(tmp_path, error, stage):
    fake = FakeBlueZ(connected=True)

    def runner(argv, **kwargs):
        if argv[:2] == ["busctl", "call"]:
            fake.connected = False
            if error == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if error == "oserror":
                raise OSError("command failed")
            return subprocess.CompletedProcess(argv, 1, "", "already disconnected")
        return fake(argv, **kwargs)

    link = _link(runner, tmp_path)
    result = getattr(link, stage)(stage)
    assert result.get("known_clear", result.get("verified")) is True
    assert len(result["errors"]) == 1
    assert result["heals"] == 0
    assert (
        json.loads((tmp_path / "meshcore-peer-link-hygiene.jsonl").read_text())
        == result
    )


def test_property_timeout_budget_and_malformed_response(tmp_path):
    timeouts = []

    def runner(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "unexpected response", "")

    link = BlueZLink(
        "AA:BB:CC:DD:EE:01", "hci0", tmp_path, runner=runner, poll_interval=0.01
    )
    assert link.wait_dropped(0.05) is None
    assert timeouts and all(0 < timeout <= 0.05 for timeout in timeouts)
    assert timeouts[-1] <= timeouts[0]


def test_hygiene_record_failure_cannot_pass_or_replace_primary(tmp_path):
    absent = tmp_path / "absent"
    with pytest.raises(OSError):
        _link(FakeBlueZ(connected=False), absent).verify_dropped("disarm")
    with pytest.raises(LinkHygieneError, match="did not drop") as exc:
        _link(FakeBlueZ(connected=True, drop_works=False), absent).verify_dropped(
            "disarm"
        )
    assert any("evidence write failed" in note for note in exc.value.__notes__)


def test_listener_readiness_timeout_reaps_before_heal(monkeypatch):
    def probe(stage):
        if stage == "arm-failed":
            assert listener._owner.proc is not None
            assert listener._owner.proc.poll() is not None

    listener = _listener(
        monkeypatch, "import time; time.sleep(30)", FakeLink(probe=probe)
    )
    listener._owner._ready_timeout = 0.1
    with pytest.raises(AssertionError, match="never signalled ready"):
        listener.__enter__()
    assert listener._owner.stdio_closed


def test_full_window_rejects_observation_gap(monkeypatch):
    script = (
        "import json, sys, time\n"
        "open(sys.argv[4], 'w').write(json.dumps({'observation_gap': True}) + '\\n')\n"
        "open(sys.argv[5], 'w').write('1')\n"
        "time.sleep(0.4)\n"
        "print(json.dumps({'received': []}))\n"
    )
    listener = _listener(monkeypatch, script, FakeLink())
    with listener:
        with pytest.raises(AssertionError, match="disconnected during collection"):
            listener.packets(timeout=3)


def test_failed_arm_retains_primary_when_heal_fails(monkeypatch):
    class BrokenCleanup(FakeLink):
        def verify_dropped(self, stage):
            raise LinkHygieneError("cleanup failure")

    listener = _listener(monkeypatch, _FAILING_PEER_SCRIPT, BrokenCleanup())
    with pytest.raises(AssertionError, match="died before readiness") as exc:
        listener.__enter__()
    assert "cleanup failure" in " ".join(exc.value.__notes__)
