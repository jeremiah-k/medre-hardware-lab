"""Offline tests for the mixed-transport continuity case.

No radio, SDK, MEDRE installation, or network is required: the A-3
evaluator runs on synthetic transcripts, the Matrix peers run against a
stubbed client function, and the durable-state predicates run against a
minimal real SQLite database with the observed row shapes — the same
pattern as ``test_worker_continuity.py``.

The three campaign regressions that must FAIL on dishonesty are encoded
here explicitly: a fault that never occurred, healthy-route traffic
observed only after the rejoin, and evidence bound to the wrong
generation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from medre_lab import a3_eval, cli, mixed_continuity
from medre_lab.a3_eval import evaluate_a3
from medre_lab.matrix_live_peer import MatrixPeerListener, run_matrix_send

SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_events (event_id TEXT PRIMARY KEY, source_adapter TEXT, source_native_channel_id TEXT, source_native_message_id TEXT);
CREATE TABLE IF NOT EXISTS event_body_content (event_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_receipts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT UNIQUE,
    adapter_message_id TEXT,
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
    receipt_id TEXT,
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


@pytest.fixture
def db(tmp_path):
    con = sqlite3.connect(tmp_path / "state.sqlite")
    con.executescript(SCHEMA)
    con.commit()
    yield con, tmp_path
    con.close()


def insert_event(
    con,
    nonce,
    rows,
    receipts=(),
):
    """Insert one canonical event for *nonce* with explicit per-adapter rows.

    ``rows`` maps adapter -> dict(outbox_id=..., status=..., in_progress=...).
    ``receipts`` is a list of (adapter, outbox_id, status, failure_kind).
    """
    event_id = f"ev-{nonce}"
    con.execute(
        "INSERT INTO canonical_events VALUES (?, 'matrix0', '!a:x', ?)",
        (event_id, f"$ingress-{nonce}"),
    )
    con.execute(
        "INSERT INTO event_body_content (event_id, body) VALUES (?, ?)",
        (event_id, nonce),
    )
    for adapter, spec in rows.items():
        con.execute(
            "INSERT INTO delivery_outbox (outbox_id, event_id, target_adapter, status,"
            " attempt_number, active_attempt, locked_at, worker_id, lease_until)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                spec["outbox_id"],
                event_id,
                adapter,
                spec.get("status", "pending"),
                spec.get("attempt_number", 0),
                1 if spec.get("in_progress") else None,
                None,
                None,
                None,
            ),
        )
        if spec.get("in_progress"):
            con.execute(
                "INSERT INTO radio_attempt_progress VALUES (?, 1, ?)",
                (
                    spec["outbox_id"],
                    json.dumps([{"ordinal": 0, "state": "unsent", "handoff_id": None}]),
                ),
            )
    for index, (adapter, outbox_id, status, failure) in enumerate(receipts):
        con.execute(
            "INSERT INTO delivery_receipts (receipt_id, outbox_id, event_id,"
            " target_adapter, status, failure_kind) VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"rc-{outbox_id}-{adapter}-{status}-{index}",
                outbox_id,
                event_id,
                adapter,
                status,
                failure,
            ),
        )
    con.execute(
        "UPDATE delivery_outbox SET receipt_id=(SELECT receipt_id FROM delivery_receipts WHERE delivery_receipts.outbox_id=delivery_outbox.outbox_id AND status='sent') WHERE status='sent'"
    )
    con.commit()


def head_state(con, nonce):
    """Head claimed, reserved, pre-handoff at meshcore; matrix1 sibling sent."""
    insert_event(
        con,
        nonce,
        {
            "meshcore": {
                "outbox_id": f"ob-m-{nonce}",
                "status": "in_progress",
                "in_progress": True,
            },
            "matrix1": {
                "outbox_id": f"ob-b-{nonce}",
                "status": "sent",
                "attempt_number": 1,
            },
        },
        receipts=[("matrix1", f"ob-b-{nonce}", "sent", None)],
    )
    return mixed_continuity.capture_nonce_state(_db_path(con), nonce)


def _db_path(con) -> object:
    return Path(con.execute("PRAGMA database_list").fetchone()[2])


def test_head_boundary_ignores_sibling_matrix_receipts(db):
    con, _ = db
    snapshot = head_state(con, "n-head")
    assert mixed_continuity._head_boundary_mixed(snapshot) is True


def test_head_boundary_violated_by_mesh_receipt(db):
    con, _ = db
    insert_event(
        con,
        "n-mesh-rcpt",
        {
            "meshcore": {
                "outbox_id": "ob-m",
                "status": "in_progress",
                "in_progress": True,
            },
            "matrix1": {"outbox_id": "ob-b", "status": "sent", "attempt_number": 1},
        },
        receipts=[
            ("matrix1", "ob-b", "sent", None),
            ("meshcore", "ob-m", "failed", "adapter_transient"),
        ],
    )
    snapshot = mixed_continuity.capture_nonce_state(_db_path(con), "n-mesh-rcpt")
    assert mixed_continuity._head_boundary_mixed(snapshot) is False


def test_tail_pending_never_claimed(db):
    con, _ = db
    insert_event(
        con,
        "n-tail",
        {
            "meshcore": {"outbox_id": "ob-m", "status": "pending"},
            "matrix1": {"outbox_id": "ob-b", "status": "pending"},
        },
    )
    snapshot = mixed_continuity.capture_nonce_state(_db_path(con), "n-tail")
    assert mixed_continuity._tail_pending_mixed(snapshot) is True
    con.execute(
        "UPDATE delivery_outbox SET status='in_progress', active_attempt=1,"
        " attempt_number=1 WHERE outbox_id='ob-m'"
    )
    con.commit()
    snapshot = mixed_continuity.capture_nonce_state(_db_path(con), "n-tail")
    assert mixed_continuity._tail_pending_mixed(snapshot) is False


def test_final_sent_tolerates_transient_fault_failures(db):
    """Released-core transient vocabulary: scheduled retries are not permanent.

    medre 0.1.2 records fault-window adapter retries as
    ``failure_kind="adapter_transient"`` (its retryable rule); the lab must
    classify against that vocabulary, not a bare ``"transient"`` literal.
    """
    con, _ = db
    insert_event(
        con,
        "n-affected",
        {"meshcore": {"outbox_id": "ob-m", "status": "sent", "attempt_number": 5}},
        receipts=[
            ("meshcore", "ob-m", "failed", "adapter_transient"),
            ("meshcore", "ob-m", "failed", "adapter_transient"),
            ("meshcore", "ob-m", "sent", None),
            ("matrix1", "ob-b", "sent", None),
        ],
    )
    con.execute(
        "INSERT INTO delivery_outbox (outbox_id, event_id, target_adapter,"
        " status, attempt_number) VALUES ('ob-b', 'ev-n-affected',"
        " 'matrix1', 'sent', 1)"
    )
    con.execute(
        "UPDATE delivery_outbox SET receipt_id=(SELECT receipt_id FROM delivery_receipts WHERE delivery_receipts.outbox_id=delivery_outbox.outbox_id AND status='sent') WHERE status='sent'"
    )
    con.execute(
        "UPDATE delivery_receipts SET attempt_number=5 WHERE outbox_id='ob-m' AND status='sent'"
    )
    con.commit()
    snapshot = mixed_continuity.capture_nonce_state(_db_path(con), "n-affected")
    assert mixed_continuity._final_sent_mixed(snapshot, "meshcore") is True
    # The healthy route's identity stays readable while the affected route's
    # transient retries are in the same event.
    identity = mixed_continuity._mixed_receipt_identity(snapshot, "matrix1")
    assert identity is not None


def test_final_sent_rejects_the_bare_transient_misreading(db):
    """``failure_kind="transient"`` matches no released-core kind: permanent."""
    con, _ = db
    insert_event(
        con,
        "n-unknown-kind",
        {"meshcore": {"outbox_id": "ob-m", "status": "sent", "attempt_number": 1}},
        receipts=[
            ("meshcore", "ob-m", "failed", "transient"),
            ("meshcore", "ob-m", "sent", None),
        ],
    )
    snapshot = mixed_continuity.capture_nonce_state(_db_path(con), "n-unknown-kind")
    assert mixed_continuity._final_sent_mixed(snapshot, "meshcore") is False


def test_final_sent_rejects_permanent_failure(db):
    con, _ = db
    insert_event(
        con,
        "n-dead",
        {"meshcore": {"outbox_id": "ob-m", "status": "sent", "attempt_number": 1}},
        receipts=[("meshcore", "ob-m", "sent", None)],
    )
    assert mixed_continuity._final_sent_mixed(
        mixed_continuity.capture_nonce_state(_db_path(con), "n-dead"), "meshcore"
    )
    con.execute(
        "INSERT INTO delivery_receipts (receipt_id,event_id,target_adapter,status,failure_kind) VALUES ('bad','ev-n-dead','matrix1','failed','exhausted')"
    )
    con.commit()
    assert not mixed_continuity._final_sent_mixed(
        mixed_continuity.capture_nonce_state(_db_path(con), "n-dead"), "meshcore"
    )


# ---------------------------------------------------------------------------
# A-3 evaluator: the PASS shape and the three dishonesty regressions
# ---------------------------------------------------------------------------


def passing_transcript() -> dict:
    return {
        "fault": {
            "occurred": True,
            "disconnect_command_succeeded": True,
            "action": "worker-owned busctl disconnect of the MEDRE-owned board",
            "disconnect_offset": 100.0,
            "rejoin_offset": 140.0,
            "last_disconnected_offset": 139.5,
            "connection_transitions": [
                {"connected": False, "offset": 99.9},
                {"connected": True, "offset": 139.8},
            ],
        },
        "unaffected": {
            "manifest": [
                {
                    "nonce": "N1",
                    "event_id": "ev-N1",
                    "outbox_ids": {"matrix1": "ob-b-N1"},
                },
                {
                    "nonce": "N2",
                    "event_id": "ev-N2",
                    "outbox_ids": {"matrix1": "ob-b-N2"},
                },
            ],
            "coverage_complete": True,
            "observations": [
                {
                    "nonce": "N1",
                    "event_id": "$N1",
                    "observed_offset": 103.5,
                    "occurrences": 1,
                },
                {
                    "nonce": "N2",
                    "event_id": "$N2",
                    "observed_offset": 104.1,
                    "occurrences": 1,
                },
            ],
        },
        "affected": {
            "manifest": [
                {
                    "nonce": "N1",
                    "event_id": "ev-N1",
                    "outbox_ids": {"meshcore": "ob-m-N1"},
                },
                {
                    "nonce": "N2",
                    "event_id": "ev-N2",
                    "outbox_ids": {"meshcore": "ob-m-N2"},
                },
            ],
            "coverage_complete": True,
            "observations_during_fault": [],
            "post_rejoin_delivered_nonces": ["N1", "N2"],
            "post_rejoin_outcome": "all_delivered",
        },
        "generation_binding": {
            "N1": {
                "event_id": "ev-N1",
                "matrix_ingress_event_id": "$in1",
                "matrix_source_matches": True,
                "matrix_delivery_event_id": "$N1",
                "dest_outbox_ids": {"matrix1": "ob-b-N1", "meshcore": "ob-m-N1"},
                "single_event": True,
            },
            "N2": {
                "event_id": "ev-N2",
                "matrix_ingress_event_id": "$in2",
                "matrix_source_matches": True,
                "matrix_delivery_event_id": "$N2",
                "dest_outbox_ids": {"matrix1": "ob-b-N2", "meshcore": "ob-m-N2"},
                "single_event": True,
            },
        },
        "sampler": {
            "baseline": [{"start": t, "end": t + 0.1} for t in (1, 6, 11)],
            "interruption": [
                {"start": t, "end": t + 0.1} for t in (101, 106, 111, 116)
            ],
            "recovery": [{"start": t, "end": t + 0.1} for t in (141, 146, 151)],
        },
    }


def test_a3_passes_on_the_honest_shape():
    verdict = evaluate_a3(passing_transcript())
    assert verdict["pass"] is True, verdict
    assert verdict["failed"] == []


def test_regression_fault_never_occurred_fails():
    transcript = passing_transcript()
    transcript["fault"]["occurred"] = False
    transcript["fault"]["disconnect_offset"] = None
    transcript["fault"]["rejoin_offset"] = None
    verdict = evaluate_a3(transcript)
    assert verdict["pass"] is False
    assert "fault_actually_occurred" in verdict["failed"]


def test_regression_healthy_traffic_only_after_rejoin_fails():
    transcript = passing_transcript()
    # Healthy deliveries land only after the MeshCore rejoin: the exact gap
    # this campaign exists to close must FAIL, not pass.
    for observation, offset in zip(
        transcript["unaffected"]["observations"], (141.5, 142.1), strict=True
    ):
        observation["observed_offset"] = offset
    verdict = evaluate_a3(transcript)
    assert verdict["pass"] is False
    assert "healthy_route_delivered_inside_declared_window" in verdict["failed"]
    assert "healthy_delivery_precedes_rejoin" in verdict["failed"]


def test_regression_wrong_generation_binding_fails():
    transcript = passing_transcript()
    transcript["generation_binding"]["N1"]["event_id"] = "ev-OTHER"
    verdict = evaluate_a3(transcript)
    assert verdict["pass"] is False
    assert "generation_binding_exact" in verdict["failed"]


def test_regression_wrong_outbox_generation_fails():
    transcript = passing_transcript()
    transcript["generation_binding"]["N2"]["dest_outbox_ids"]["meshcore"] = "ob-OTHER"
    transcript["affected"]["manifest"][1]["outbox_ids"]["meshcore"] = "ob-m-N2"
    verdict = evaluate_a3(transcript)
    assert verdict["pass"] is False
    assert "generation_binding_exact" in verdict["failed"]


def test_regression_duplicate_observations_fail():
    transcript = passing_transcript()
    transcript["unaffected"]["observations"].append(
        {"nonce": "N1", "observed_offset": 105.0}
    )
    verdict = evaluate_a3(transcript)
    assert verdict["pass"] is False
    assert "healthy_route_delivered_inside_declared_window" in verdict["failed"]


def test_regression_missing_witness_observation_fails():
    transcript = passing_transcript()
    transcript["unaffected"]["observations"] = transcript["unaffected"]["observations"][
        :1
    ]
    verdict = evaluate_a3(transcript)
    assert verdict["pass"] is False
    assert "healthy_route_delivered_inside_declared_window" in verdict["failed"]


def test_regression_affected_route_delivered_during_fault_fails():
    transcript = passing_transcript()
    transcript["affected"]["observations_during_fault"] = [
        {"nonce": "N1", "observed_offset": 120.0}
    ]
    verdict = evaluate_a3(transcript)
    assert verdict["pass"] is False
    assert "affected_route_silent_during_fault" in verdict["failed"]


def test_regression_sparse_sampler_fails():
    transcript = passing_transcript()
    transcript["sampler"]["interruption"] = ["only-one"]
    verdict = evaluate_a3(transcript)
    assert verdict["pass"] is False
    assert "sampler_coverage" in verdict["failed"]


def test_empty_transcript_fails():
    verdict = evaluate_a3({})
    assert verdict["pass"] is False


def test_worker_owned_action_required():
    transcript = passing_transcript()
    transcript["fault"]["action"] = "manual operator action"
    verdict = evaluate_a3(transcript)
    assert verdict["pass"] is False
    assert "fault_was_worker_owned" in verdict["failed"]


# ---------------------------------------------------------------------------
# Matrix peer listener (stubbed transport)
# ---------------------------------------------------------------------------


def stub_sync(monkeypatch, pages):
    """Replace matrix_request with a scripted sync sequence."""
    calls = []

    def fake(homeserver, method, path, body=None, token=None, timeout=30.0):
        calls.append(path)
        if path.endswith("/whoami"):
            return {"user_id": "@w:x"}
        if "_matrix/client/v3/sync?timeout=0" in path:
            return {"next_batch": "s0", "rooms": {"join": {"!b:x": {}}}}
        index = len([c for c in calls if "since=" in c]) - 1
        return pages[min(index, len(pages) - 1)]

    monkeypatch.setattr("medre_lab.matrix_live_peer.matrix_request", fake)
    return calls


def test_listener_collects_dedups_and_offsets(monkeypatch):
    stub_sync(
        monkeypatch,
        [
            {
                "next_batch": "s1",
                "rooms": {
                    "join": {
                        "!b:x": {
                            "timeline": {
                                "events": [
                                    {
                                        "type": "m.room.message",
                                        "event_id": "$e1",
                                        "sender": "@m1:x",
                                        "origin_server_ts": 1,
                                        "content": {"body": "MLAB-nonce-1"},
                                    },
                                    {
                                        "type": "m.room.message",
                                        "event_id": "$e1",
                                        "sender": "@m1:x",
                                        "content": {"body": "MLAB-nonce-1"},
                                    },
                                ]
                            }
                        }
                    }
                },
            },
            {
                "next_batch": "s2",
                "rooms": {
                    "join": {
                        "!b:x": {
                            "timeline": {
                                "events": [
                                    {
                                        "type": "m.room.message",
                                        "event_id": "$e2",
                                        "sender": "@m1:x",
                                        "content": {"body": "MLAB-nonce-2"},
                                    },
                                ]
                            }
                        }
                    }
                },
            },
        ],
    )
    listener = MatrixPeerListener("http://x", "tok", ("!b:x",), "@w:x")
    with listener:
        packets = listener.packets_until(lambda ps: len(ps) >= 2, 2.0)
    assert [p["text"] for p in packets] == ["MLAB-nonce-1", "MLAB-nonce-2"]
    assert packets[0]["event_id"] != packets[1]["event_id"]
    assert packets[1]["observed_offset"] >= packets[0]["observed_offset"]


def test_listener_ignores_other_rooms_and_non_messages(monkeypatch):
    stub_sync(
        monkeypatch,
        [
            {
                "next_batch": "s1",
                "rooms": {
                    "join": {
                        "!other:x": {
                            "timeline": {
                                "events": [
                                    {
                                        "type": "m.room.message",
                                        "event_id": "$x",
                                        "content": {"body": "MLAB-nonce-1"},
                                    }
                                ]
                            }
                        },
                        "!b:x": {
                            "timeline": {
                                "events": [
                                    {
                                        "type": "m.room.member",
                                        "event_id": "$y",
                                        "content": {"membership": "join"},
                                    }
                                ]
                            }
                        },
                    }
                },
            },
        ],
    )
    listener = MatrixPeerListener("http://x", "tok", ("!b:x",), "@w:x")
    with listener:
        packets = listener.packets_until(lambda ps: False, 0.3)
    assert packets == []


def test_send_returns_event_id(monkeypatch):
    seen = {}

    def fake(homeserver, method, path, body=None, token=None, timeout=30.0):
        seen["method"] = method
        seen["path"] = path
        seen["body"] = body
        return {"event_id": "$sent1"}

    monkeypatch.setattr("medre_lab.matrix_live_peer.matrix_request", fake)
    result = run_matrix_send("http://x", "tok", "!a:x", "MLAB-nonce-9")
    assert result["event_id"] == "$sent1"
    assert result["nonce"] == "MLAB-nonce-9"
    assert seen["method"] == "PUT" and "send/m.room.message/" in seen["path"]
    assert seen["body"]["body"] == "MLAB-nonce-9"


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------


@pytest.fixture
def campaign_files(tmp_path, monkeypatch):
    port = tmp_path / "board.serial"
    port.write_text("")
    toml = tmp_path / "lab.toml"
    toml.write_text(
        "[hardware]\n"
        'ble_adapter="hci0"\n'
        "startup_timeout=60\n"
        "send_timeout=100\n"
        "observation_timeout=50\n"
        "\n[hardware.meshcore]\n"
        "channel=1\n"
        'addresses=["c4:4f:33:6a:ae:a7", "C4:4F:33:6A:A5:2B"]\n'
        "\n[hardware.meshtastic]\n"
        "channel=0\n"
        f'ports=["{port}"]\n'
    )
    matrix = tmp_path / "campaign-matrix.json"
    accounts = {
        key: {"user_id": f"@campaign-{key}:x", "access_token": f"tok-{key}"}
        for key in ("m0", "m1", "sender", "witness")
    }
    matrix.write_text(
        json.dumps(
            {
                "homeserver": "http://127.0.0.1:8008",
                "rooms": {"a": {"room_id": "!a:x"}, "b": {"room_id": "!b:x"}},
                "accounts": accounts,
            }
        )
    )
    return toml, matrix


def test_load_mixed_accepts_single_meshtastic_port_and_uppercases(campaign_files):
    toml, matrix = campaign_files
    config = mixed_continuity.load_mixed(toml, matrix)
    assert config["hardware"]["meshcore"]["addresses"] == [
        "C4:4F:33:6A:AE:A7",
        "C4:4F:33:6A:A5:2B",
    ]
    assert len(config["hardware"]["meshtastic"]["ports"]) == 1
    assert config["matrix"]["rooms"]["a"] == "!a:x"


def test_load_mixed_resolves_ble_adapter_from_environment(campaign_files, monkeypatch):
    toml, matrix = campaign_files
    monkeypatch.setenv("MEDRE_LAB_BLE_ADAPTER", "hci1")
    config = mixed_continuity.load_mixed(toml, matrix)
    assert config["hardware"]["ble_adapter"] == "hci1"


def test_load_mixed_fingerprint_binds_both_files(campaign_files):
    toml, matrix = campaign_files
    first = mixed_continuity.load_mixed(toml, matrix)["matrix"]["fingerprint"]
    matrix.write_text(matrix.read_text().replace("!a:x", "!a2:x"))
    second = mixed_continuity.load_mixed(toml, matrix)["matrix"]["fingerprint"]
    assert first != second


def test_load_mixed_rejects_unknown_section(campaign_files):
    toml, matrix = campaign_files
    toml.write_text(toml.read_text() + "\n[routes]\nx=1\n")
    with pytest.raises(mixed_continuity.MixedConfigError):
        mixed_continuity.load_mixed(toml, matrix)


def test_load_mixed_rejects_two_meshcore_addresses_dup(campaign_files):
    toml, matrix = campaign_files
    toml.write_text(toml.read_text().replace("C4:4F:33:6A:A5:2B", "C4:4F:33:6A:AE:A7"))
    with pytest.raises(mixed_continuity.MixedConfigError):
        mixed_continuity.load_mixed(toml, matrix)


def test_load_mixed_rejects_missing_port(campaign_files):
    toml, matrix = campaign_files
    toml.write_text(toml.read_text().replace("board.serial", "absent.serial"))
    with pytest.raises(mixed_continuity.MixedConfigError):
        mixed_continuity.load_mixed(toml, matrix)


def test_cli_mixed_continuity_requires_matrix_file(tmp_path):
    with pytest.raises(ValueError):
        cli.mixed_continuity(
            tmp_path,
            str(tmp_path / "absent.json"),
            "a",
            str(tmp_path / "runs"),
            3,
            tmp_path / "lab.toml",
        )


# ---------------------------------------------------------------------------
# Evaluator helper checks
# ---------------------------------------------------------------------------


def test_min_sampler_coverage_is_three():
    assert a3_eval.A3_MIN_SAMPLER_COVERAGE == 3


@pytest.mark.parametrize(
    "fault",
    [
        "missing_outbox",
        "reused_event",
        "reused_witness",
        "ambiguous_rejoin",
        "missing_coverage",
        "late_sample",
        "command_failed",
        "wrong_delivery",
    ],
)
def test_adversarial_transcript_boundaries(fault):
    transcript = passing_transcript()
    a = transcript["affected"]
    u = transcript["unaffected"]
    b = transcript["generation_binding"]
    if fault == "missing_outbox":
        a["manifest"][0]["outbox_ids"]["meshcore"] = None
        b["N1"]["dest_outbox_ids"]["meshcore"] = None
    elif fault == "reused_event":
        b["N2"]["event_id"] = b["N1"]["event_id"]
        for route in (a, u):
            route["manifest"][1]["event_id"] = b["N1"]["event_id"]
    elif fault == "reused_witness":
        u["observations"][1]["event_id"] = "$N1"
        b["N2"]["matrix_delivery_event_id"] = "$N1"
    elif fault == "ambiguous_rejoin":
        u["observations"][1]["observed_offset"] = 139.75
    elif fault == "missing_coverage":
        a["coverage_complete"] = False
    elif fault == "late_sample":
        transcript["sampler"]["interruption"] = [
            {"start": 141 + i, "end": 141.5 + i} for i in range(3)
        ]
    elif fault == "command_failed":
        transcript["fault"]["disconnect_command_succeeded"] = False
    else:
        b["N1"]["matrix_delivery_event_id"] = "$different"
    assert not evaluate_a3(transcript)["pass"]


def test_duplicate_outbox_cannot_bind_generation(db):
    con, _ = db
    head_state(con, "dup")
    assert set(
        mixed_continuity.bind_generation(_db_path(con), "dup")["dest_outbox_ids"]
    ) == {"meshcore", "matrix1"}
    con.execute(
        "INSERT INTO delivery_outbox (outbox_id,event_id,target_adapter,status) VALUES ('extra','ev-dup','meshcore','pending')"
    )
    con.commit()
    assert not mixed_continuity.bind_generation(_db_path(con), "dup")["dest_outbox_ids"]


def test_completed_destination_ignores_sibling_settlement_but_not_receipt_change(db):
    con, _ = db
    snapshot = head_state(con, "stable")
    leg = {"nonce": "stable", "destination": "matrix1", "durable": snapshot}
    con.execute(
        "UPDATE delivery_outbox SET status='pending' WHERE target_adapter='meshcore'"
    )
    con.commit()
    mixed_continuity._verify_completed(_db_path(con), [leg])
    con.execute(
        "UPDATE delivery_receipts SET receipt_id='replacement' WHERE target_adapter='matrix1'"
    )
    con.execute(
        "UPDATE delivery_outbox SET receipt_id='replacement' WHERE target_adapter='matrix1'"
    )
    con.commit()
    with pytest.raises(AssertionError, match="changed"):
        mixed_continuity._verify_completed(_db_path(con), [leg])


def test_receipt_must_match_exact_outbox(db):
    con, _ = db
    head_state(con, "bound")
    con.execute("UPDATE delivery_receipts SET outbox_id='unrelated'")
    con.commit()
    assert not mixed_continuity._final_sent_mixed(
        mixed_continuity.capture_nonce_state(_db_path(con), "bound"), "matrix1"
    )


def test_stopped_head_must_remain_claimed(db):
    con, _ = db
    head_state(con, "head")
    con.execute(
        "UPDATE delivery_outbox SET status='pending' WHERE target_adapter='meshcore'"
    )
    con.commit()
    assert not mixed_continuity._head_boundary_mixed(
        mixed_continuity.capture_nonce_state(_db_path(con), "head")
    )


def test_matrix_limited_timeline_refuses_coverage(monkeypatch):
    stub_sync(
        monkeypatch,
        [
            {
                "next_batch": "s1",
                "rooms": {
                    "join": {"!b:x": {"timeline": {"limited": True, "events": []}}}
                },
            }
        ],
    )
    with MatrixPeerListener("http://x", "tok", ("!b:x",), "@w:x") as listener:
        with pytest.raises(AssertionError, match="timeline gap"):
            listener.packets_until(lambda _: False, 1)
        assert listener._since == "s0"


def test_same_matrix_room_refused(campaign_files):
    toml, matrix = campaign_files
    matrix.write_text(matrix.read_text().replace("!b:x", "!a:x"))
    with pytest.raises(mixed_continuity.MixedConfigError):
        mixed_continuity.load_mixed(toml, matrix)


def test_nonstring_ble_address_refused(campaign_files):
    toml, matrix = campaign_files
    toml.write_text(toml.read_text().replace('"c4:4f:33:6a:ae:a7"', "123"))
    with pytest.raises(mixed_continuity.MixedConfigError):
        mixed_continuity.load_mixed(toml, matrix)


@pytest.mark.asyncio
async def test_failed_sampler_does_not_escape_retirement():
    import asyncio

    async def fail():
        raise RuntimeError("bad sampler")

    task = asyncio.create_task(fail())
    await asyncio.sleep(0)
    result = {"pass": True}
    await mixed_continuity._retire_task(task, result, "sampler")
    assert result == {"pass": False, "sampler_error": "RuntimeError"}


@pytest.mark.asyncio
async def test_nonzero_sampler_json_not_credited():
    import subprocess
    import sys

    with pytest.raises(subprocess.CalledProcessError):
        await mixed_continuity._sample_command(
            [sys.executable, "-c", "print('{}'); raise SystemExit(2)"]
        )


def test_cli_pins_one_effective_config_for_both_workers(
    campaign_files, monkeypatch, tmp_path
):
    toml, matrix = campaign_files
    monkeypatch.setattr(mixed_continuity, "verify_controller", lambda _: "hci0")
    observed = []

    def spawn(argv, log, timeout):
        config_path = Path(argv[4])
        config = json.loads(config_path.read_text())
        observed.append(config)
        assert config_path.stat().st_mode & 0o077 == 0
        assert config["matrix"]["store_dir"] == str(matrix.parent / "matrix-stores")
        phase = argv[7]
        Path(argv[6], f"{phase}-verdict.json").write_text(
            json.dumps({"phase": phase, "pass": True})
        )
        matrix.write_text("{}")
        return 0

    monkeypatch.setattr(cli, "_spawn_worker", spawn)
    assert (
        cli.mixed_continuity(
            tmp_path, str(matrix), "a", str(tmp_path / "runs"), 2, toml
        )
        == 0
    )
    assert len(observed) == 2 and observed[0] == observed[1]


def test_matrix_request_headers_redirect_and_total_deadline():
    import http.server
    import threading
    import time

    from medre_lab.matrix_live_peer import matrix_request

    calls = []
    slow_started = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):
            calls.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get("Content-Type"),
                )
            )
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"event_id":"$ok"}')

        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization"), None))
            if self.path.endswith("redirect"):
                self.send_response(302)
                self.send_header("Location", "/_matrix/other")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(20):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        slow_started.set()
                        time.sleep(0.1)
                except (BrokenPipeError, ConnectionResetError):
                    pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        assert matrix_request(
            origin, "PUT", "/_matrix/send", {"body": "hi"}, "secret", 3
        ) == {"event_id": "$ok"}
        assert calls[-1] == ("/_matrix/send", "Bearer secret", "application/json")
        with pytest.raises(RuntimeError, match="no observation credited"):
            matrix_request(
                origin, "GET", "/_matrix/redirect", token="secret", timeout=3
            )
        assert [call[0] for call in calls] == ["/_matrix/send", "/_matrix/redirect"]
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            matrix_request(origin, "GET", "/_matrix/slow", token="secret", timeout=1.0)
        assert slow_started.is_set()
        assert time.monotonic() - start < 2.0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("value", [None, {}, ""])
def test_missing_affected_silence_evidence_is_not_a_pass(value):
    transcript = passing_transcript()
    transcript["affected"]["observations_during_fault"] = value
    assert "affected_route_silent_during_fault" in evaluate_a3(transcript)["failed"]


@pytest.mark.parametrize(
    "outcome, sightings, expected",
    [
        ("not_delivered", ["N1"], False),
        ("not_delivered", [], True),
        ("partial", [], False),
        ("partial", ["N1", "N2"], True),
    ],
)
def test_affected_outcomes_match_sightings(outcome, sightings, expected):
    transcript = passing_transcript()
    transcript["affected"].update(
        post_rejoin_outcome=outcome, post_rejoin_delivered_nonces=sightings
    )
    assert evaluate_a3(transcript)["pass"] is expected


def test_transient_rejoin_cannot_extend_first_fault():
    transcript = passing_transcript()
    transcript["fault"]["connection_transitions"].insert(
        1, {"connected": True, "offset": 101.0}
    )
    transcript["fault"]["connection_transitions"].insert(
        2, {"connected": False, "offset": 102.0}
    )
    assert "fault_actually_occurred" in evaluate_a3(transcript)["failed"]


async def test_connection_hook_preserves_health_callback_and_rejects_intervening_rejoin():
    import time
    from types import SimpleNamespace

    notifications = []
    session = SimpleNamespace(
        connected=True, _on_connection_change=notifications.append
    )
    original = session._on_connection_change
    adapter = SimpleNamespace(
        _session=session, diagnostics=lambda: {"session": {"connected": False}}
    )
    with mixed_continuity._observe_mesh_transitions(adapter) as transitions:
        for connected in (False, True, False):
            session._on_connection_change(connected)
        assert notifications == [False, True, False]
        with pytest.raises(RuntimeError, match="continuous outage"):
            await mixed_continuity._observe_rejoin(
                adapter, 0.1, [{"monotonic": time.monotonic()}], {}, transitions
            )
    assert session._on_connection_change is original


@pytest.mark.parametrize(
    "response",
    [
        {"rooms": {"leave": {"!b:x": {}}}},
        {
            "rooms": {
                "join": {
                    "!b:x": {
                        "timeline": {
                            "events": [
                                {
                                    "type": "m.room.member",
                                    "state_key": "@w:x",
                                    "content": {"membership": "leave"},
                                }
                            ]
                        }
                    }
                }
            }
        },
    ],
)
def test_witness_departure_invalidates_coverage(monkeypatch, response):
    stub_sync(monkeypatch, [{"next_batch": "s1", **response}])
    with MatrixPeerListener("http://x", "tok", ("!b:x",), "@w:x") as listener:
        with pytest.raises(AssertionError, match="lost campaign room membership"):
            listener.packets_until(lambda _: False, 0.1)
        assert listener.last_sync_started_monotonic is None


def test_witness_requires_joined_rooms_on_arm(monkeypatch):
    stub_sync(monkeypatch, [])
    with pytest.raises(AssertionError, match="not joined"):
        MatrixPeerListener("http://x", "tok", ("!a:x", "!b:x"), "@w:x").__enter__()


def test_witness_omitted_room_is_not_departure(monkeypatch):
    stub_sync(monkeypatch, [{"next_batch": "s1"}])
    with MatrixPeerListener("http://x", "tok", ("!b:x",), "@w:x") as listener:
        assert listener.packets_until(lambda _: True, 0.1) == []
        assert (
            listener.ready_monotonic
            <= listener.last_sync_started_monotonic
            <= listener.last_sync_monotonic
        )


def test_existing_matrix_store_is_made_private_without_reset(tmp_path):
    import stat

    store = tmp_path / "matrix-stores"
    store.mkdir(mode=0o755)
    data = store / "native.db"
    data.write_bytes(b"existing state")
    mixed_continuity._private_matrix_store(store)
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert data.read_bytes() == b"existing state"
    link = tmp_path / "alias"
    link.symlink_to(store, target_is_directory=True)
    with pytest.raises(mixed_continuity.MixedConfigError):
        mixed_continuity._private_matrix_store(link)


async def test_baseline_refuses_existing_state_without_baseline_verdict(tmp_path):
    (tmp_path / "state.sqlite").write_bytes(b"retained")
    with pytest.raises(RuntimeError, match="fresh run"):
        await mixed_continuity.run_mixed_baseline({}, 0, tmp_path)
    assert (tmp_path / "state.sqlite").read_bytes() == b"retained"


async def test_sampler_publishes_work_before_pressure_retires(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    pressure_started = asyncio.Event()
    dispose = asyncio.Event()

    async def command(argv):
        if "pressure" in argv:
            pressure_started.set()
            await dispose.wait()
        return SimpleNamespace(stdout="{}")

    monkeypatch.setattr(mixed_continuity, "_medre_binary", lambda: Path("medre"))
    monkeypatch.setattr(mixed_continuity, "_sample_command", command)
    task = asyncio.create_task(
        mixed_continuity.run_sampler(
            tmp_path, tmp_path / "state.sqlite", {"current": "recovery"}, 2
        )
    )
    try:
        await asyncio.wait_for(pressure_started.wait(), 3)
        records = [
            json.loads(line)
            for line in (tmp_path / "work-status-series.jsonl").read_text().splitlines()
        ]
        assert records[-1]["ok"] is True
        assert records[-1]["cycle"] == 0
    finally:
        await mixed_continuity._retire_task(task, {}, "sampler")


async def test_worker_cancellation_during_sampler_retirement_is_recorded():
    import asyncio

    retiring = asyncio.Event()
    finish = asyncio.Event()

    async def sampler():
        try:
            await asyncio.Future()
        finally:
            retiring.set()
            await finish.wait()

    task = asyncio.create_task(sampler())
    await asyncio.sleep(0)
    result = {"pass": True}
    worker = asyncio.create_task(mixed_continuity._retire_task(task, result, "sampler"))
    await asyncio.wait_for(retiring.wait(), 2)
    finish.set()
    worker.cancel()
    await worker
    assert result["cancelled"] is True and result["pass"] is False
    assert task.done()


def test_healthy_receipt_binding_excludes_affected_permanent_failure(db):
    con, _ = db
    insert_event(
        con,
        "n-healthy",
        {
            "matrix1": {"outbox_id": "ob-b", "status": "sent", "attempt_number": 1},
            "meshcore": {"outbox_id": "ob-m", "status": "dead_lettered"},
        },
        receipts=[
            ("matrix1", "ob-b", "sent", None),
            ("meshcore", "ob-m", "failed", "exhausted"),
        ],
    )
    snapshot = mixed_continuity.capture_nonce_state(_db_path(con), "n-healthy")
    assert (
        mixed_continuity._mixed_receipt_identity(
            snapshot, "matrix1", outbox_id="ob-b", destination_only=True
        )
        is not None
    )
    assert (
        mixed_continuity._mixed_receipt_identity(snapshot, "matrix1", outbox_id="ob-b")
        is None
    )


def test_affected_recovery_uses_final_exact_receipts_not_earlier_wait(db):
    con, _ = db
    insert_event(con, "late", {"meshcore": {"outbox_id": "ob-m", "status": "pending"}})
    binding = {"late": {"event_id": "ev-late", "dest_outbox_ids": {"meshcore": "ob-m"}}}
    before = mixed_continuity.capture_nonce_state(_db_path(con), "late")
    assert (
        mixed_continuity._affected_recovery_outcome(
            ["late"], binding, [before], ["late"]
        )
        == "partial"
    )
    con.execute(
        "UPDATE delivery_outbox SET status='sent',attempt_number=1,receipt_id='late-sent' WHERE outbox_id='ob-m'"
    )
    # Match the real durable identity columns, not just the destination name.
    con.execute(
        "INSERT INTO delivery_receipts (receipt_id,event_id,target_adapter,status,failure_kind,outbox_id,delivery_plan_id,target_channel,attempt_number,receipt_kind) SELECT 'late-sent',event_id,target_adapter,'sent',NULL,outbox_id,delivery_plan_id,target_channel,attempt_number,'attempt' FROM delivery_outbox WHERE outbox_id='ob-m'"
    )
    con.commit()
    after = mixed_continuity.capture_nonce_state(_db_path(con), "late")
    assert (
        mixed_continuity._affected_recovery_outcome(
            ["late"], binding, [after], ["late"]
        )
        == "all_delivered"
    )
    assert (
        mixed_continuity._affected_recovery_outcome(["late"], binding, [after], [])
        == "not_delivered"
    )
    binding["late"]["dest_outbox_ids"]["meshcore"] = "other"
    assert (
        mixed_continuity._affected_recovery_outcome(
            ["late"], binding, [after], ["late"]
        )
        == "partial"
    )
