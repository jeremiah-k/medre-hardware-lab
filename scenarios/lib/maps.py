"""SQLite message_map access for scenario assertions.

Runs INSIDE the lab-tools container (relay state is mounted read-only at
/state/mmrelay-*). One script, subcommand-driven; prints JSON evidence.

Reads (mode=ro, WAL-aware):
    rows-for-event  DB EVENT_ID          -> all map rows keyed by Matrix event id
    rows-for-text   DB SUBSTR            -> rows whose meshtastic_text contains SUBSTR
    rows-for-radio  DB PACKET_ID         -> rows keyed by meshtastic packet id
    count           DB                   -> SELECT COUNT(*) FROM message_map
    dump            DB [LIMIT]           -> recent rows (id desc)

Write (used only to seed the packet-id-0 reaction target and to remove the
seed in teardown; the seed row is created fresh per run and deleted after):
    seed            DB PACKET_ID EVENT_ID ROOM TEXT MESHNET
    delete-event    DB EVENT_ID
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import time
from typing import Any


def connect(path: str, write: bool) -> sqlite3.Connection:
    uri = f"file:{path}" + ("" if write else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout = 8000")
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def wait_row(
    db: str, where: str, args: tuple, timeout: float
) -> tuple[list[dict[str, Any]] | None, str]:
    """Poll a map query until rows appear or timeout.

    Returns (rows_or_None, last_error). rows is None only on timeout.
    """
    deadline = time.monotonic() + timeout
    sql = f"SELECT * FROM message_map WHERE {where}"
    last_error = ""
    while True:
        try:
            with contextlib.closing(connect(db, write=False)) as conn:
                rows = rows_to_dicts(conn.execute(sql, args))
        except sqlite3.Error as exc:
            rows = None
            last_error = str(exc)
        if rows:
            return rows, last_error
        if time.monotonic() >= deadline:
            return None, last_error
        time.sleep(1.0)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def db_arg(sub_parser: argparse.ArgumentParser) -> None:
        sub_parser.add_argument("db")

    p = sub.add_parser("rows-for-event"); db_arg(p); p.add_argument("event_id"); p.add_argument("--wait", type=float, default=0.0)
    p = sub.add_parser("rows-for-text"); db_arg(p); p.add_argument("substr"); p.add_argument("--wait", type=float, default=0.0)
    p = sub.add_parser("rows-for-radio"); db_arg(p); p.add_argument("packet_id"); p.add_argument("--wait", type=float, default=0.0)
    p = sub.add_parser("count"); db_arg(p)
    p = sub.add_parser("dump"); db_arg(p); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("seed")
    p.add_argument("db"); p.add_argument("packet_id"); p.add_argument("event_id"); p.add_argument("room"); p.add_argument("text"); p.add_argument("meshnet")
    p = sub.add_parser("delete-event"); db_arg(p); p.add_argument("event_id")

    args = parser.parse_args()

    if args.command in {"rows-for-event", "rows-for-text", "rows-for-radio"}:
        if args.command == "rows-for-event":
            where, params = "matrix_event_id = ?", (args.event_id,)
        elif args.command == "rows-for-radio":
            where, params = "meshtastic_id = ?", (str(args.packet_id),)
        else:
            where, params = "meshtastic_text LIKE ?", (f"%{args.substr}%",)
        if args.wait:
            rows, last_error = wait_row(args.db, where, params, args.wait)
            if rows is None:
                print(
                    json.dumps(
                        {
                            "rows": [],
                            "waited": True,
                            "db": args.db,
                            "error": last_error or "timed out waiting for map row",
                        },
                        ensure_ascii=False,
                    )
                )
                return 1
        else:
            with contextlib.closing(connect(args.db, write=False)) as conn:
                rows = rows_to_dicts(conn.execute(f"SELECT * FROM message_map WHERE {where}", params))
        print(json.dumps({"rows": rows, "db": args.db}, ensure_ascii=False))
        return 0

    if args.command == "count":
        with contextlib.closing(connect(args.db, write=False)) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM message_map").fetchone()
        print(json.dumps({"count": row["n"], "db": args.db}))
        return 0

    if args.command == "dump":
        with contextlib.closing(connect(args.db, write=False)) as conn:
            rows = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM message_map ORDER BY rowid DESC LIMIT ?", (args.limit,)
                )
            )
        print(json.dumps({"rows": rows, "db": args.db}, ensure_ascii=False))
        return 0

    if args.command == "seed":
        # message_map's only unique key is matrix_event_id (PRIMARY KEY); the
        # seed event id is fresh per run, so INSERT OR REPLACE never clobbers
        # real rows. meshtastic_id "0" is deliberately non-unique here: the
        # whole point is that packet id 0 must remain a resolvable target.
        with contextlib.closing(connect(args.db, write=True)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO message_map (meshtastic_id, matrix_event_id,"
                " matrix_room_id, meshtastic_text, meshtastic_meshnet)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(args.packet_id), args.event_id, args.room, args.text, args.meshnet),
            )
            conn.commit()
        print(json.dumps({"seeded": {"meshtastic_id": str(args.packet_id), "matrix_event_id": args.event_id}}))
        return 0

    if args.command == "delete-event":
        with contextlib.closing(connect(args.db, write=True)) as conn:
            cursor = conn.execute("DELETE FROM message_map WHERE matrix_event_id = ?", (args.event_id,))
            conn.commit()
            print(json.dumps({"deleted": cursor.rowcount}))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
