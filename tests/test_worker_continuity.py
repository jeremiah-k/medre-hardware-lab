"""Offline sequencing, ownership and failed-cleanup tests for the continuity case.

No radio, SDK, or MEDRE installation is required: the runtime assembly,
ownership, worker helpers and peer listeners are stubbed with the same
SimpleNamespace pattern as ``test_worker_sequence.py``, while the
durable-state captures run against a minimal real SQLite database with the
observed row shapes.
"""

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from medre_lab import cli, continuity, ownership, worker

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_body_content (event_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_receipts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT UNIQUE,
    outbox_id TEXT,
    event_id TEXT NOT NULL,
    delivery_plan_id TEXT NOT NULL DEFAULT '',
    target_adapter TEXT NOT NULL,
    target_channel TEXT,
    status TEXT NOT NULL,
    receipt_kind TEXT NOT NULL DEFAULT 'attempt',
    failure_kind TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    confirmation_level TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS delivery_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    delivery_plan_id TEXT NOT NULL DEFAULT '',
    target_adapter TEXT NOT NULL,
    target_channel TEXT,
    status TEXT NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 0,
    active_attempt INTEGER,
    locked_at TEXT,
    lease_until TEXT,
    worker_id TEXT,
    updated_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS radio_attempt_progress (outbox_id TEXT, attempt_number INTEGER, parts_json TEXT);
"""


def make_db(run) -> sqlite3.Connection:
    con = sqlite3.connect(run / "state.sqlite", check_same_thread=False)
    con.executescript(SCHEMA)
    con.commit()
    return con


def insert_event(con, nonce, adapter, *, receipt_status=None, outbox_status=None):
    con.execute(
        "INSERT INTO event_body_content (event_id, body) VALUES (?, ?)",
        (f"ev-{nonce}", nonce),
    )
    if receipt_status is not None:
        con.execute(
            "INSERT INTO delivery_receipts (receipt_id, outbox_id, event_id, target_adapter,"
            " status) VALUES (?, ?, ?, ?, ?)",
            (f"rc-{nonce}", f"ob-{nonce}", f"ev-{nonce}", adapter, receipt_status),
        )
    if outbox_status is not None:
        con.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, target_adapter, status, attempt_number, active_attempt) VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"ob-{nonce}",
                f"ev-{nonce}",
                adapter,
                outbox_status,
                int(outbox_status == "sent"),
                1 if outbox_status == "in_progress" else None,
            ),
        )
        if outbox_status == "in_progress":
            con.execute(
                "INSERT INTO radio_attempt_progress VALUES (?, 1, ?)",
                (
                    f"ob-{nonce}",
                    json.dumps([{"ordinal": 0, "state": "unsent", "handoff_id": None}]),
                ),
            )
    con.commit()


class StubListener:
    """Far-side peer listener with preset and received packets."""

    instances: list = []

    def __init__(self, kind, seconds):
        self.kind = kind
        self.seconds = seconds
        self.active = False
        self.packets: list = []
        StubListener.instances.append(self)

    def __enter__(self):
        self.active = True
        return self

    def __exit__(self, *exc):
        self.active = False

    def packets_until(self, predicate, timeout):
        import time

        deadline = time.monotonic() + timeout
        while True:
            if predicate(self.packets):
                return list(self.packets)
            if time.monotonic() >= deadline:
                return list(self.packets)
            time.sleep(0.02)

    def receive(self, text):
        self.packets.append({"text": text})


class StubApp:
    def __init__(self, meshcore_diag=None, on_start=None, diagnostics=None):
        self.started = False
        self.stopped = False
        self.events: list = []
        self.on_start = on_start
        self._diagnostics = diagnostics or (
            lambda: {"session": dict(meshcore_diag or {})}
        )
        self.adapters = {
            "meshcore": SimpleNamespace(
                diagnostics=self._diagnostics,
                health_check=self._health,
            ),
            "lxmf": SimpleNamespace(health_check=self._health),
        }

    async def _health(self):
        return SimpleNamespace(health="healthy")

    async def start(self):
        self.started = True
        self.events.append("start")
        if self.on_start is not None:
            self.on_start()

    async def stop(self):
        self.stopped = True
        self.events.append("stop")


def stub_config(**overrides):
    hw = {"startup_timeout": 5, "send_timeout": 5, "observation_timeout": 0.1}
    hw.update(overrides)
    return {"hardware": hw}


@pytest.fixture()
def fast_polls(monkeypatch):
    monkeypatch.setattr(continuity, "_DB_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(continuity, "RECONCILE_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(continuity, "UNCERTAIN_SETTLE_WAIT_SECONDS", 2.0)


class Harness:
    """Stubbed worker helpers plus a scripted durable-state database."""

    def __init__(self, monkeypatch, app, run, burst_mode="normal"):
        self.monkeypatch = monkeypatch
        self.app = app
        self.con = make_db(run)
        self.sent: list = []
        self.send_count = 0
        self.burst_mode = burst_mode
        StubListener.instances = []

        def make_listener(kind, seconds):
            listener = StubListener(kind, seconds)
            return listener

        def fake_configure(config, transports, role, run_dir):
            listeners = {
                "meshcore": lambda seconds: make_listener("meshcore", seconds),
                "lxmf": lambda seconds: make_listener("lxmf", seconds),
            }

            def sender(source):
                destination = "lxmf" if source == "meshcore" else "meshcore"

                def send(nonce):
                    assert not any(
                        listener.active and listener.kind == source
                        for listener in StubListener.instances
                    )
                    self.sent.append(nonce)
                    self.send_count += 1
                    if self.send_count <= 4:
                        insert_event(
                            self.con,
                            nonce,
                            destination,
                            receipt_status="sent",
                            outbox_status="sent",
                        )
                        self.deliver(destination, nonce)
                        return {"nonce": nonce}
                    if self.burst_mode == "normal":
                        status = "in_progress" if self.send_count == 5 else "pending"
                        insert_event(self.con, nonce, "meshcore", outbox_status=status)
                    else:
                        insert_event(
                            self.con,
                            nonce,
                            "meshcore",
                            outbox_status="in_progress",
                        )
                    return {"nonce": nonce}

                return send

            senders = {kind: sender(kind) for kind in ("meshcore", "lxmf")}
            return listeners, senders, {"meshcore": "1", "lxmf": "dest"}, []

        monkeypatch.setattr(continuity, "build", lambda *args, **kwargs: app)
        monkeypatch.setattr(continuity, "own", lambda endpoints: _nullctx())
        monkeypatch.setattr(continuity, "own_run", lambda run_dir: _nullctx())
        monkeypatch.setattr(worker, "configure", fake_configure)
        monkeypatch.setattr(
            worker, "provenance", lambda transports: {"stub": {"version": "0"}}
        )

        async def fake_drain(app):
            return [{"adapter": kind, "outcome": "skipped"} for kind in app.adapters]

        monkeypatch.setattr(worker, "wait_startup_drain_retired", fake_drain)

    @staticmethod
    def deliver(kind, text):
        for listener in StubListener.instances:
            if listener.kind == kind and listener.active:
                listener.receive(text)

    def burst_never_pending(self):
        self.burst_mode = "head_only"


def _nullctx():
    @contextmanager
    def ctx():
        yield

    return ctx()


def run_async(coro_fn):
    box = {}

    async def runner():
        box["ok"] = await coro_fn()

    asyncio.run(runner())
    return box["ok"]


def scripted_session(tmp_path, state):
    """Session diagnostics script: sentinel armed, then disconnect, then rejoin.

    The first diagnostic read after arming is the stage's connectivity
    pre-check; the disconnect is observed from the second read onward, and
    the rejoin completes on the next read. On success the session also
    resets its reconnect-attempt counter to zero (the real adapter's
    behavior), so the rejoin must be detected without requiring the
    counter to stay elevated.
    """
    import time

    armed_at = None

    def diagnostics():
        nonlocal armed_at
        if (tmp_path / "interrupt-armed.json").exists():
            if armed_at is None:
                armed_at = time.monotonic()
            if time.monotonic() - armed_at < 0.7:
                state.update(connected=False, reconnecting=True)
            else:
                state.update(connected=True, reconnecting=False, reconnect_attempts=0)
        return {"session": dict(state)}

    return diagnostics


def test_baseline_completes_all_stages_in_order(tmp_path, monkeypatch, fast_polls):
    state = {"connected": True, "reconnect_attempts": 0, "reconnecting": False}
    app = StubApp(diagnostics=scripted_session(tmp_path, state))
    harness = Harness(monkeypatch, app, tmp_path)
    original_stop = app.stop

    async def stop_with_listener():
        assert any(listener.active for listener in StubListener.instances)
        await original_stop()

    app.stop = stop_with_listener

    ok = run_async(
        lambda: continuity.run_baseline(
            stub_config(), "meshcore:lxmf", 0, tmp_path, 5.0
        )
    )
    assert ok is True
    verdict = json.loads((tmp_path / "baseline-verdict.json").read_text())
    assert verdict["pass"] is True
    stages = [t["stage"] for t in verdict["timeline"]]
    assert stages[0] == "baseline-start"
    assert "owner-started" in stages
    assert "link-disconnected" in stages
    assert "link-rejoined" in stages
    assert "pending-captured" in stages
    assert "owner-stopped" in stages
    # The destination listener retires after the MEDRE owner stops.
    assert app.events == ["start", "stop"]
    assert all(not listener.active for listener in StubListener.instances)
    assert verdict["interrupt"]["rejoined"] is True
    assert verdict["interrupt"]["disconnect_diagnostics"]["connected"] is False
    assert verdict["interrupt"]["rejoin_diagnostics"]["connected"] is True
    # The recovery loop already reset its counter between polls; the rejoin
    # evidence records the peak counter without requiring it to stay elevated.
    assert verdict["interrupt"]["rejoin_diagnostics"]["reconnect_attempts"] == 0
    assert verdict["interrupt"]["rejoin_peak_reconnect_attempts"] == 0
    assert verdict["pending_capture"]["captured"] is True
    assert len(verdict["legs"]) == 4
    assert all(leg["observed_once"] for leg in verdict["legs"])
    for leg in verdict["legs"]:
        assert leg["event_id"].startswith("ev-")
    state_file = json.loads((tmp_path / "continuity-state.json").read_text())
    assert state_file["pending_capture"]["pending"]["nonce"] in harness.sent
    assert state_file["pending_capture"]["uncertain"]["nonce"] in harness.sent
    assert not (tmp_path / "interrupt-armed.json").exists()


def test_baseline_interrupt_timeout_fails_honestly(tmp_path, monkeypatch, fast_polls):
    state = {"connected": True, "reconnect_attempts": 0, "reconnecting": False}
    # Never disconnected: diagnostics always report the live session.
    app = StubApp(diagnostics=lambda: {"session": dict(state)})
    Harness(monkeypatch, app, tmp_path)

    ok = run_async(
        lambda: continuity.run_baseline(
            stub_config(), "meshcore:lxmf", 0, tmp_path, 1.0
        )
    )
    assert ok is False
    verdict = json.loads((tmp_path / "baseline-verdict.json").read_text())
    assert verdict["pass"] is False
    assert "not disconnected within" in verdict["error"]
    # Failed cleanup: listeners closed, owner stopped, sentinel removed.
    assert all(not listener.active for listener in StubListener.instances)
    assert app.stopped is True
    assert verdict.get("stop_on_error") is True
    assert not (tmp_path / "interrupt-armed.json").exists()


def test_baseline_missing_pending_capture_fails(tmp_path, monkeypatch, fast_polls):
    state = {"connected": True, "reconnect_attempts": 0, "reconnecting": False}
    app = StubApp(diagnostics=scripted_session(tmp_path, state))
    Harness(monkeypatch, app, tmp_path, burst_mode="head_only")

    ok = run_async(
        lambda: continuity.run_baseline(
            stub_config(), "meshcore:lxmf", 0, tmp_path, 5.0
        )
    )
    assert ok is False
    verdict = json.loads((tmp_path / "baseline-verdict.json").read_text())
    assert verdict["pass"] is False
    assert "not observed pending" in verdict["error"]
    assert app.stopped is True
    assert all(not listener.active for listener in StubListener.instances)


def test_reopen_reconciles_pending_and_records_uncertain(
    tmp_path, monkeypatch, fast_polls
):
    con = make_db(tmp_path)
    legs = [
        ("MLAB-" + "a" * 12, "lxmf"),
        ("MLAB-" + "b" * 12, "meshcore"),
        ("MLAB-" + "c" * 12, "lxmf"),
        ("MLAB-" + "d" * 12, "meshcore"),
    ]
    pending = "MLAB-" + "e" * 12
    uncertain = "MLAB-" + "f" * 12
    for nonce, dest in legs:
        insert_event(con, nonce, dest, receipt_status="sent", outbox_status="sent")
    insert_event(con, uncertain, "meshcore", outbox_status="pending")
    insert_event(con, pending, "meshcore", outbox_status="pending")
    (tmp_path / "continuity-state.json").write_text(
        json.dumps(
            {
                "pending_capture": {
                    "pending": {"nonce": pending},
                    "uncertain": {"nonce": uncertain},
                    "stopped": {
                        "pending": continuity.capture_nonce_state(
                            tmp_path / "state.sqlite", pending
                        ),
                        "uncertain": continuity.capture_nonce_state(
                            tmp_path / "state.sqlite", uncertain
                        ),
                    },
                },
                "completed_legs": [
                    {
                        "nonce": nonce,
                        "destination": dest,
                        "event_id": f"ev-{nonce}",
                        "receipt_id": f"rc-{nonce}",
                    }
                    for nonce, dest in legs
                ],
            }
        )
    )

    reconciled = {"done": False}

    def on_start():
        # The reopened dispatcher progresses the proven-unfinished pending
        # work to a sent receipt; the far side observes the fresh send.
        if not reconciled["done"]:
            reconciled["done"] = True
            con.execute(
                "UPDATE delivery_outbox SET status='sent' WHERE event_id=?",
                (f"ev-{pending}",),
            )
            con.execute(
                "INSERT INTO delivery_receipts (receipt_id, outbox_id, event_id,"
                " target_adapter, status) VALUES (?, ?, ?, 'meshcore', 'sent')",
                (f"rc-{pending}-2", f"ob-{pending}", f"ev-{pending}"),
            )
            con.commit()
            Harness.deliver("meshcore", pending)

    app = StubApp(on_start=on_start)
    Harness(monkeypatch, app, tmp_path)

    ok = run_async(
        lambda: continuity.run_reopen(stub_config(), "meshcore:lxmf", 0, tmp_path)
    )
    assert ok is True
    verdict = json.loads((tmp_path / "reopen-verdict.json").read_text())
    assert verdict["pass"] is True
    assert verdict["persisted"]["completed_identities_retained"] is True
    assert verdict["disposition"]["pending_reconciled"] is True
    # The uncertain case's disposition is recorded, whatever core decides.
    assert "uncertain_final_statuses" in verdict["disposition"]
    assert verdict["uncertain_after"]["events"]
    assert "uncertain_progressed" in verdict["disposition"]
    # A fresh marker still flows after reopen.
    assert len(verdict["legs"]) == 2
    assert verdict["legs"][0]["observed_once"] is True
    assert app.events == ["start", "stop"]


def test_reopen_requires_baseline_state(tmp_path, monkeypatch, fast_polls):
    make_db(tmp_path)
    app = StubApp()
    Harness(monkeypatch, app, tmp_path)

    ok = run_async(
        lambda: continuity.run_reopen(stub_config(), "meshcore:lxmf", 0, tmp_path)
    )
    assert ok is False
    verdict = json.loads((tmp_path / "reopen-verdict.json").read_text())
    assert "requires a baseline state file" in verdict["error"]
    assert app.started is False


def test_reopen_fails_when_completed_identity_lost(tmp_path, monkeypatch, fast_polls):
    con = make_db(tmp_path)
    leg = "MLAB-" + "a" * 12
    pending = "MLAB-" + "e" * 12
    uncertain = "MLAB-" + "f" * 12
    insert_event(con, leg, "lxmf", receipt_status="sent", outbox_status="sent")
    insert_event(con, pending, "meshcore", outbox_status="pending")
    insert_event(con, uncertain, "meshcore", outbox_status="pending")
    con.execute("DELETE FROM delivery_receipts WHERE event_id=?", (f"ev-{leg}",))
    con.commit()
    (tmp_path / "continuity-state.json").write_text(
        json.dumps(
            {
                "pending_capture": {
                    "pending": {"nonce": pending},
                    "uncertain": {"nonce": uncertain},
                },
                "completed_legs": [
                    {
                        "nonce": leg,
                        "destination": "lxmf",
                        "event_id": f"ev-{leg}",
                        "receipt_id": f"rc-{leg}",
                    }
                ],
            }
        )
    )
    app = StubApp()
    Harness(monkeypatch, app, tmp_path)

    ok = run_async(
        lambda: continuity.run_reopen(stub_config(), "meshcore:lxmf", 0, tmp_path)
    )
    assert ok is False
    verdict = json.loads((tmp_path / "reopen-verdict.json").read_text())
    assert "did not persist" in verdict["error"]
    # The owner never started: the persisted-verification fence held.
    assert app.started is False


def test_own_run_blocks_while_first_owner_alive(tmp_path):
    acquired = threading.Event()
    release = threading.Event()

    def hold():
        with ownership.own_run(tmp_path):
            acquired.set()
            release.wait(5)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert acquired.wait(5)
    with pytest.raises(RuntimeError, match="only permitted after the first owner"):
        with ownership.own_run(tmp_path):
            pass  # pragma: no cover - must not be reached
    release.set()
    thread.join(5)
    # After the first owner retires the lock is available again.
    with ownership.own_run(tmp_path):
        pass


def test_worker_main_dispatches_pair_and_phases(monkeypatch, tmp_path):
    calls = []

    async def fake_pair(config, selection, role, run):
        calls.append(("pair", selection, role))
        return True

    async def fake_baseline(config, selection, role, run, interrupt_wait):
        calls.append(("baseline", selection, role, interrupt_wait))
        return True

    async def fake_reopen(config, selection, role, run):
        calls.append(("reopen", selection, role))
        return True

    monkeypatch.setattr(worker, "run_pair", fake_pair)
    monkeypatch.setattr(continuity, "run_baseline", fake_baseline)
    monkeypatch.setattr(continuity, "run_reopen", fake_reopen)
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[hardware]\nstartup_timeout=5\n")

    assert worker.main([str(cfg), "meshcore:lxmf", "0", str(tmp_path)]) == 0
    assert calls == [("pair", ("meshcore", "lxmf"), 0)]

    calls.clear()
    assert (
        worker.main([str(cfg), "meshcore:lxmf", "0", str(tmp_path), "baseline", "42"])
        == 0
    )
    assert calls == [("baseline", "meshcore:lxmf", 0, 42.0)]

    calls.clear()
    assert worker.main([str(cfg), "meshcore:lxmf", "0", str(tmp_path), "reopen"]) == 0
    assert calls == [("reopen", "meshcore:lxmf", 0)]

    with pytest.raises(SystemExit, match="unknown continuity phase"):
        worker.main([str(cfg), "meshcore:lxmf", "0", str(tmp_path), "bogus"])


def _lab_config(tmp_path) -> "object":
    config = tmp_path / "lab.toml"
    config.write_text(
        "[hardware]\n"
        "startup_timeout=5\nsend_timeout=5\nobservation_timeout=5\n"
        "[hardware.meshcore]\n"
        'channel=1\naddresses=["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"]\n'
        "[hardware.lxmf]\n"
        'profiles=["p-a", "p-b"]\nidentities=["i-a", "i-b"]\n'
    )
    return config


def test_cli_continuity_records_process_boundary(tmp_path, monkeypatch):
    config = _lab_config(tmp_path)
    run_root = tmp_path / "runs"
    spawned = []

    def fake_spawn(argv, log_path, timeout, watch=None):
        run = log_path.parent
        phase = argv[7]
        spawned.append((phase, argv[8] if len(argv) > 8 else None, watch is not None))
        (run / f"{phase}-verdict.json").write_text(
            json.dumps({"phase": phase, "pass": True}) + "\n"
        )
        if watch is not None:
            # The watcher thread must stop once the child "exits".
            class FakeChild:
                def poll(self):
                    return 0

            threading.Thread(target=watch, args=(FakeChild(),), daemon=True).start()
        return 0

    monkeypatch.setattr(cli, "_spawn_worker", fake_spawn)
    code = cli.continuity(
        tmp_path, config, "meshcore:lxmf", "a", 30.0, True, str(run_root)
    )
    assert code == 0
    assert [phase for phase, _, _ in spawned] == ["baseline", "reopen"]
    # Baseline carries the interrupt wait; reopen does not.
    assert spawned[0][1] == "30.0"
    assert spawned[1][1] is None
    # Auto-disconnect watcher armed for the baseline phase only.
    assert spawned[0][2] is True
    assert spawned[1][2] is False
    runs = list(run_root.iterdir())
    merged = json.loads((runs[0] / "continuity-verdict.json").read_text())
    assert merged["pass"] is True
    assert set(merged["process_boundary"]) == {"baseline", "reopen"}
    assert merged["process_boundary"]["baseline"]["exit_code"] == 0
    assert merged["process_boundary"]["reopen"]["pass"] is True


def test_cli_continuity_records_worker_absent_honestly(tmp_path, monkeypatch):
    config = _lab_config(tmp_path)
    run_root = tmp_path / "runs"

    def fake_spawn(argv, log_path, timeout, watch=None):
        return 1  # dies before any verdict

    monkeypatch.setattr(cli, "_spawn_worker", fake_spawn)
    code = cli.continuity(
        tmp_path, config, "meshcore:lxmf", "a", 30.0, False, str(run_root)
    )
    assert code == 1
    runs = list(run_root.iterdir())
    baseline = json.loads((runs[0] / "baseline-verdict.json").read_text())
    assert baseline["stage"] == "worker-exited-before-verdict"
    merged = json.loads((runs[0] / "continuity-verdict.json").read_text())
    assert merged["pass"] is False


def test_capture_nonce_state_reports_outbox_and_receipts(tmp_path):
    con = make_db(tmp_path)
    insert_event(con, "MLAB-x", "meshcore", receipt_status="sent", outbox_status="sent")
    snapshot = continuity.capture_nonce_state(tmp_path / "state.sqlite", "MLAB-x")
    assert len(snapshot["events"]) == 1
    assert snapshot["events"][0]["event_id"] == "ev-MLAB-x"
    assert snapshot["events"][0]["receipts"][0]["status"] == "sent"
    assert snapshot["events"][0]["outbox"][0]["status"] == "sent"
    assert continuity._sent_receipt_event_id(snapshot, "meshcore") == "ev-MLAB-x"
    assert (
        continuity._sent_receipt_event_id(snapshot, "meshcore", outbox_id="ob-MLAB-x")
        == "ev-MLAB-x"
    )
    assert (
        continuity._sent_receipt_event_id(
            snapshot, "meshcore", outbox_id="replacement-generation"
        )
        is None
    )
    assert continuity._has_pending_outbox(snapshot) is False
    missing = continuity.capture_nonce_state(tmp_path / "state.sqlite", "MLAB-nope")
    assert missing["events"] == []
    assert continuity._sent_receipt_event_id(missing, "meshcore") is None


@pytest.mark.parametrize(
    "raw,exit_code,expected",
    [
        ("{", 0, 1),
        ('{"phase":"baseline","pass":true}', 1, 1),
        ('{"phase":"baseline","pass":true}', 130, 130),
    ],
)
def test_cli_never_reopens_failed_or_cancelled_worker(
    tmp_path, monkeypatch, raw, exit_code, expected
):
    config = _lab_config(tmp_path)
    calls = []

    def spawn(argv, log_path, timeout, watch=None):
        calls.append(argv[7])
        (log_path.parent / "baseline-verdict.json").write_text(raw)
        return exit_code

    monkeypatch.setattr(cli, "_spawn_worker", spawn)
    assert (
        cli.continuity(
            tmp_path, config, "meshcore:lxmf", "a", 5, False, str(tmp_path / "runs")
        )
        == expected
    )
    assert calls == ["baseline"]
    report = json.loads(
        next((tmp_path / "runs").iterdir())
        .joinpath("continuity-verdict.json")
        .read_text()
    )
    assert report["pass"] is False


@pytest.mark.parametrize("wait", [float("nan"), float("inf"), 0, -1])
def test_cli_rejects_invalid_interrupt_wait(tmp_path, wait):
    config = _lab_config(tmp_path)
    with pytest.raises(ValueError, match="finite and positive"):
        cli.continuity(
            tmp_path, config, "meshcore:lxmf", "a", wait, False, str(tmp_path / "runs")
        )
    assert not (tmp_path / "runs").exists()


def test_duplicate_canonical_marker_is_not_credited(tmp_path):
    con = make_db(tmp_path)
    insert_event(con, "marker", "meshcore", receipt_status="sent")
    con.execute("INSERT INTO event_body_content VALUES ('duplicate', 'marker')")
    con.commit()
    assert (
        continuity._sent_receipt_event_id(
            continuity.capture_nonce_state(tmp_path / "state.sqlite", "marker"),
            "meshcore",
        )
        is None
    )


def test_contender_leaves_owner_evidence_untouched(tmp_path):
    state = tmp_path / "continuity-state.json"
    state.write_text("owner evidence")
    with (
        ownership.own_run(tmp_path),
        pytest.raises(RuntimeError, match="live MEDRE owner"),
    ):
        run_async(
            lambda: continuity.run_baseline(
                stub_config(), "meshcore:lxmf", 0, tmp_path, 5
            )
        )
    assert state.read_text() == "owner evidence"
    assert not (tmp_path / "baseline-verdict.json").exists()
