#!/usr/bin/env python3
"""Matrix evidence reader: a read-only store snapshot (runs INSIDE a container).

Two modes, one per store the matrices assert on:

``repeater``
    The openHop repeater's own SQLite store (``/var/lib/openhop_repeater/
    repeater.db``): the rows the companion queued for a client while NO
    client was attached, the channel table, and the identity fingerprint.
    This is the "did the repeater persist it with no client" fact.

``medre``
    MEDRE's own mapping store (``/data/state/medre.sqlite``): the canonical
    events, their native ids and their delivery receipts, scoped to a
    timestamp window and/or a nonce needle.

Both modes use a read-only SQLite transaction for a consistent WAL-aware
snapshot. Queries fail the evidence run rather than returning error rows.

Output: one JSON object on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPEATER_DB = Path("/var/lib/openhop_repeater/repeater.db")
REPEATER_IDENTITY = Path("/var/lib/openhop_repeater/identity.key")
MEDRE_DB = Path("/data/state/medre.sqlite")


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description or []]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def open_ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.execute("BEGIN")
    conn.row_factory = sqlite3.Row
    return conn


def repeater_state(args) -> dict:
    out: dict = {"mode": "repeater", "label": "protocol-simulated"}
    if REPEATER_IDENTITY.exists():
        out["identity_key_sha256"] = hashlib.sha256(
            REPEATER_IDENTITY.read_bytes()
        ).hexdigest()
    else:
        out["identity_key_sha256"] = None
    if not REPEATER_DB.exists():
        out["error"] = f"{REPEATER_DB} not found"
        return out

    conn = open_ro(REPEATER_DB)
    try:
        messages = rows(
            conn,
            "SELECT id, companion_hash, text, is_channel, channel_idx, "
            "packet_hash, created_at FROM companion_messages "
            "WHERE instr(coalesce(text, ''), ?) > 0 OR ? = '' ORDER BY id",
            (args.needle, args.needle),
        )
        out["companion_message_count"] = len(messages)
        out["companion_messages"] = messages
        out["companion_message_total"] = rows(
            conn, "SELECT COUNT(*) AS n FROM companion_messages"
        )[0]["n"]
        out["channels"] = rows(
            conn,
            "SELECT companion_hash, channel_idx, name, CASE WHEN secret IS NULL THEN 0 ELSE 1 END AS has_secret "
            "FROM companion_channels ORDER BY channel_idx",
        )
    finally:
        conn.close()
    return out


def medre_state(args) -> dict:
    out: dict = {"mode": "medre", "label": "protocol-simulated"}
    if not MEDRE_DB.exists():
        out["error"] = f"{MEDRE_DB} not found"
        return out

    conn = open_ro(MEDRE_DB)
    try:
        # The pinned MEDRE schema uses an ordinary SQLite rowid table
        # and leaves its rowid unset on insert. Use the observed insertion
        # order within the snapshot, independent of clock ties or reversal.
        events = rows(
            conn,
            "SELECT event_id, event_kind, source_adapter, source_channel_id, "
            "source_transport_id, source_native_adapter, source_native_channel_id, "
            "source_native_message_id, payload, metadata, created_at "
            "FROM canonical_events ORDER BY rowid",
        )
        if args.since:
            since = datetime.fromisoformat(args.since)
            events = [
                e for e in events if datetime.fromisoformat(e["created_at"]) >= since
            ]
        if args.until:
            until = datetime.fromisoformat(args.until)
            events = [
                e for e in events if datetime.fromisoformat(e["created_at"]) <= until
            ]
        if args.needle:
            events = [e for e in events if args.needle in json.dumps(e, sort_keys=True)]
        out["canonical_event_count"] = len(events)
        out["canonical_events"] = events
        out["canonical_event_total"] = rows(
            conn, "SELECT COUNT(*) AS n FROM canonical_events"
        )[0]["n"]

        event_ids = (json.dumps([event["event_id"] for event in events]),)
        refs = rows(
            conn,
            "SELECT event_id, adapter, native_channel_id, native_message_id, "
            "direction, created_at FROM native_message_refs "
            "WHERE event_id IN (SELECT value FROM json_each(?)) ORDER BY created_at",
            event_ids,
        )
        out["native_message_ref_count"] = len(refs)
        out["native_message_refs"] = refs

        receipts = rows(
            conn,
            "SELECT receipt_id, event_id, route_id, target_adapter, target_channel, "
            "status, receipt_kind, adapter_message_id, capability_level, "
            "delivery_strategy, created_at FROM delivery_receipts "
            "WHERE event_id IN (SELECT value FROM json_each(?)) ORDER BY created_at",
            event_ids,
        )
        out["delivery_receipt_count"] = len(receipts)
        out["delivery_receipts"] = receipts
    finally:
        conn.close()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("repeater", "medre"), required=True)
    parser.add_argument("--needle", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--until", default="")
    args = parser.parse_args(argv)

    try:
        out = repeater_state(args) if args.mode == "repeater" else medre_state(args)
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        out = {
            "mode": args.mode,
            "error": f"store snapshot failed: {type(exc).__name__}",
        }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if "error" not in out else 1


if __name__ == "__main__":
    sys.exit(main())
