#!/usr/bin/env python3
"""Read MEDRE's own mapping store for the bridged events (evidence only).

Runs INSIDE the medre-openhop-bridge container and prints, for each nonce,
the canonical event MEDRE created, its native identity refs and its
delivery receipts. Read-only SQLite transaction: all queries observe one consistent snapshot,
including committed WAL pages. No database copy or scratch directory.

This is the MEDRE-side observer: it shows that MEDRE created exactly ONE
canonical event per bridged message (no duplicate client event at
companion A) and that each event carries a native id and a delivery
receipt.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB = Path("/data/state/medre.sqlite")


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description or []]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def main() -> int:
    if not DB.exists():
        print(json.dumps({"error": f"{DB} not found"}))
        return 1
    conn = sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.execute("BEGIN")
    conn.row_factory = sqlite3.Row
    try:
        events = rows(
            conn,
            "SELECT event_id, event_kind, source_adapter, source_channel_id, "
            "source_transport_id, source_native_adapter, source_native_channel_id, "
            "source_native_message_id, payload, metadata, created_at "
            "FROM canonical_events ORDER BY created_at",
        )
        refs = rows(
            conn,
            "SELECT event_id, adapter, native_channel_id, native_message_id, "
            "direction, created_at FROM native_message_refs ORDER BY created_at",
        )
        receipts = rows(
            conn,
            "SELECT receipt_id, event_id, route_id, target_adapter, target_channel, "
            "status, receipt_kind, adapter_message_id, capability_level, "
            "delivery_strategy, created_at FROM delivery_receipts ORDER BY created_at",
        )
        outbox = rows(
            conn,
            "SELECT outbox_id, event_id, route_id, target_adapter, target_channel, "
            "status, attempt_number, created_at FROM delivery_outbox ORDER BY created_at",
        )
        facts = rows(
            conn,
            "SELECT event_id, matched_routes, fanout_group FROM event_routing_facts "
            "ORDER BY recorded_at",
        )
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "canonical_events": events,
                "native_message_refs": refs,
                "delivery_receipts": receipts,
                "delivery_outbox": outbox,
                "event_routing_facts": facts,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
