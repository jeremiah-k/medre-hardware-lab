"""Store-order evidence comes from insert order within the captured DB."""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import openhop_store_snapshot as snapshot  # noqa: E402


def test_snapshot_preserves_insertion_order_when_event_clocks_disagree(
    tmp_path, monkeypatch
):
    path = tmp_path / "medre.sqlite"
    monkeypatch.setattr(snapshot, "MEDRE_DB", path)
    with sqlite3.connect(path) as store:
        store.executescript("""
            CREATE TABLE canonical_events (
                event_id TEXT PRIMARY KEY, event_kind TEXT, source_adapter TEXT,
                source_channel_id TEXT, source_transport_id TEXT,
                source_native_adapter TEXT, source_native_channel_id TEXT,
                source_native_message_id TEXT, payload TEXT, metadata TEXT,
                created_at TEXT
            );
            CREATE TABLE native_message_refs (
                event_id TEXT, adapter TEXT, native_channel_id TEXT,
                native_message_id TEXT, direction TEXT, created_at TEXT
            );
            CREATE TABLE delivery_receipts (
                receipt_id TEXT, event_id TEXT, route_id TEXT, target_adapter TEXT,
                target_channel TEXT, status TEXT, receipt_kind TEXT,
                adapter_message_id TEXT, capability_level TEXT,
                delivery_strategy TEXT, created_at TEXT
            );
            INSERT INTO canonical_events (event_id, payload, created_at)
                VALUES ('older-arrival', 'marker', '2026-01-01T00:00:02Z'),
                       ('newer-arrival', 'marker', '2026-01-01T00:00:01Z'),
                       ('unselected', '{}', '2026-01-01T00:00:00Z');
            INSERT INTO native_message_refs (event_id) VALUES ('unselected');
            INSERT INTO delivery_receipts (event_id) VALUES ('older-arrival');
            """)
    args = SimpleNamespace(needle="marker", since="", until="")
    result = snapshot.medre_state(args)
    assert [row["event_id"] for row in result["canonical_events"]] == [
        "older-arrival",
        "newer-arrival",
    ]
    assert result["native_message_refs"] == []
    assert result["delivery_receipts"][0]["event_id"] == "older-arrival"
    args.since = "2025-12-31T18:00:02-06:00"
    result = snapshot.medre_state(args)
    assert [row["event_id"] for row in result["canonical_events"]] == ["older-arrival"]
