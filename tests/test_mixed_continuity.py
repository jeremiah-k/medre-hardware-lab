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
            ("meshcore", "ob-m", "failed", "transient"),
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
    con, _ = db
    insert_event(
        con,
        "n-affected",
        {"meshcore": {"outbox_id": "ob-m", "status": "sent", "attempt_number": 2}},
        receipts=[
            ("meshcore", "ob-m", "failed", "transient"),
            ("meshcore", "ob-m", "sent", None),
            ("matrix1", "ob-b", "sent", None),
        ],
    )
    con.execute(
        "INSERT INTO delivery_outbox (outbox_id, event_id, target_adapter,"
        " status, attempt_number) VALUES ('ob-b', 'ev-n-affected',"
        " 'matrix1', 'sent', 1)"
    )
    con.commit()
    snapshot = mixed_continuity.capture_nonce_state(_db_path(con), "n-affected")
    assert mixed_continuity._final_sent_mixed(snapshot, "meshcore") is True


def test_final_sent_rejects_permanent_failure(db):
    con, _ = db
    insert_event(
        con,
        "n-dead",
        {
            "meshcore": {
                "outbox_id": "ob-m",
                "status": "dead_lettered",
                "attempt_number": 6,
            }
        },
        receipts=[
            ("meshcore", "ob-m", "failed", "transient"),
            ("meshcore", "ob-m", "failed", "exhausted"),
        ],
    )
    snapshot = mixed_continuity.capture_nonce_state(_db_path(con), "n-dead")
    assert mixed_continuity._final_sent_mixed(snapshot, "meshcore") is False


# ---------------------------------------------------------------------------
# A-3 evaluator: the PASS shape and the three dishonesty regressions
# ---------------------------------------------------------------------------


def passing_transcript() -> dict:
    return {
        "fault": {
            "occurred": True,
            "action": "worker-owned busctl disconnect of the MEDRE-owned board",
            "disconnect_offset": 100.0,
            "rejoin_offset": 140.0,
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
            "observations": [
                {"nonce": "N1", "observed_offset": 103.5},
                {"nonce": "N2", "observed_offset": 104.1},
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
            "observations_during_fault": [],
            "post_rejoin_delivered_nonces": ["N1", "N2"],
            "post_rejoin_outcome": "all_delivered",
        },
        "generation_binding": {
            "N1": {
                "event_id": "ev-N1",
                "dest_outbox_ids": {"matrix1": "ob-b-N1", "meshcore": "ob-m-N1"},
                "single_event": True,
            },
            "N2": {
                "event_id": "ev-N2",
                "dest_outbox_ids": {"matrix1": "ob-b-N2", "meshcore": "ob-m-N2"},
                "single_event": True,
            },
        },
        "sampler": {
            "baseline": ["a", "b", "c"],
            "interruption": ["a", "b", "c", "d"],
            "recovery": ["a", "b", "c"],
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
    transcript["unaffected"]["observations"] = [
        {"nonce": "N1", "observed_offset": 141.5},
        {"nonce": "N2", "observed_offset": 142.1},
    ]
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
        if "_matrix/client/v3/sync?timeout=0" in path:
            return {"next_batch": "s0"}
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
        key: {"user_id": f"@campaign-{key}:x", "access_token": "tok"}
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
