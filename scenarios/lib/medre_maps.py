"""SQLite access to the MEDRE bridge state (canonical event storage).

Runs INSIDE the lab-tools container (the rig mounts the medre-state volume
read-only; the builder reports the exact path, published as MEDRE_DB /
MEDRE_DB_A / MEDRE_DB_B via lab/state/scenario.env). Mirrors maps.py's
read-only, WAL-aware, subcommand-driven contract and prints JSON evidence.

MEDRE storage schema (medre core sqlite, candidate tip): canonical rows in
``canonical_events`` (payload JSON carries {body, msgtype, ...}; rendered
body text is also projected into ``event_body_content``), native-space
mappings in ``native_message_refs`` (UNIQUE(adapter, native_channel_id,
native_message_id), direction inbound/outbound - this is the medre analog
of mmrelay's message_map), and typed links in ``event_relations``.

Reads (mode=ro):
    refs-for-native   DB NATIVE_ID [ADAPTER] -> refs for a Matrix event id or
                       radio packet id (the medre analog of rows-for-event /
                       rows-for-radio)
    events-for-native DB NATIVE_ID [ADAPTER] -> canonical events owning that
                       native id, each with ALL its native refs (both the
                       Matrix ref and the radio ref of one canonical event)
    refs-for-event    DB CANONICAL_EVENT_ID  -> refs + event row
    events-for-text   DB SUBSTR              -> canonical events whose stored
                       body (event_body_content or payload JSON) matches
    count             DB                     -> canonical_events +
                       native_message_refs row counts
    dump              DB [LIMIT]             -> recent events with refs

This reader is strictly read-only: unlike maps.py it has no seed/delete
subcommands (the medre bridge owns its state; scenarios never write it).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any

_TABLES_SQL = (
    "SELECT name FROM sqlite_master WHERE type='table' AND name IN"
    " ('canonical_events','native_message_refs','event_relations','event_body_content')"
)


class _ScratchConnection(sqlite3.Connection):
    """Connection subclass carrying its scratch-copy directory."""

    scratch_dir: str | None = None


def connect(path: str) -> sqlite3.Connection:
    """Open the medre state read-only via a scratch copy.

    The medre-state volume is mounted READ-ONLY into lab-tools and the db is
    in WAL mode (write/read version 2): a mode=ro connection cannot create
    the -shm file on a read-only directory and fails with "unable to open
    database file". Copying the db (plus -wal/-shm when present) to a
    writable temp dir and opening the copy gives consistent reads; a torn
    copy under a heavy writer surfaces as an SQLite error and the caller's
    wait/poll loop simply retries.
    """
    scratch = tempfile.mkdtemp(prefix="medre-maps-")
    local = os.path.join(scratch, "medre.sqlite")
    for suffix in ("", "-wal", "-shm"):
        source = path + suffix
        try:
            if os.path.exists(source):
                shutil.copyfile(source, local + suffix)
        except OSError:
            pass
    uri = f"file:{local}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10, factory=_ScratchConnection)
    conn.execute("PRAGMA busy_timeout = 8000")
    conn.row_factory = sqlite3.Row
    conn.scratch_dir = scratch
    return conn


@contextlib.contextmanager
def open_state(path: str):
    """Yield a read-only connection over a scratch copy, cleaning up after."""
    conn = connect(path)
    try:
        yield conn
    finally:
        scratch = getattr(conn, "scratch_dir", None)
        conn.close()
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _require_tables(conn: sqlite3.Connection) -> None:
    present = {row["name"] for row in conn.execute(_TABLES_SQL)}
    missing = {"canonical_events", "native_message_refs"} - present
    if missing:
        raise sqlite3.DatabaseError(
            f"medre state missing expected table(s): {sorted(missing)};"
            " is this the medre bridge's storage (builder contract:"
            " MEDRE_DB published in lab/state/scenario.env)?"
        )


def _event_rows(
    conn: sqlite3.Connection, where: str, args: tuple
) -> list[dict[str, Any]]:
    events = rows_to_dicts(
        conn.execute(
            "SELECT e.*, "  # nosec B608 # Fixed predicates; values bound.
            "e.source_native_adapter AS envelope_source_adapter, "
            "e.source_native_channel_id AS envelope_source_channel, "
            "e.source_native_message_id AS envelope_source_packet "
            f"FROM canonical_events e WHERE {where}",
            args,
        )
    )
    body_rows = rows_to_dicts(
        conn.execute(
            "SELECT event_id, field, body FROM event_body_content"  # nosec B608 # Fixed predicates; values bound.
            f" WHERE event_id IN (SELECT event_id FROM canonical_events e WHERE {where})",
            args,
        )
    )
    bodies: dict[str, dict[str, str]] = {}
    for row in body_rows:
        bodies.setdefault(row["event_id"], {})[row["field"]] = row["body"]
    refs = rows_to_dicts(
        conn.execute(
            "SELECT r.* FROM native_message_refs r"  # nosec B608 # Fixed predicates; values bound.
            f" WHERE r.event_id IN (SELECT event_id FROM canonical_events e WHERE {where})"
            " ORDER BY r.created_at",
            args,
        )
    )
    relations = rows_to_dicts(
        conn.execute(
            "SELECT rel.* FROM event_relations rel"  # nosec B608 # Fixed predicates; values bound.
            f" WHERE rel.event_id IN (SELECT event_id FROM canonical_events e WHERE {where})"
            " ORDER BY rel.id",
            args,
        )
    )
    for event in events:
        event_id = event.get("event_id")
        try:
            event["payload"] = json.loads(event.get("payload") or "{}")
        except json.JSONDecodeError:
            pass
        event["body_content"] = bodies.get(event_id, {})
        event["native_refs"] = [ref for ref in refs if ref.get("event_id") == event_id]
        event["relations"] = [
            relation for relation in relations if relation.get("event_id") == event_id
        ]
    return events


def _has_outbound_radio(events: list[dict[str, Any]]) -> bool:
    """True when any event carries an outbound numeric radio native ref."""
    import re as _re

    for event in events:
        for ref in event.get("native_refs", []):
            if ref.get("direction") == "outbound" and _re.fullmatch(
                r"[0-9]+", str(ref.get("native_message_id") or "")
            ):
                return True
    return False


def wait_events(
    db: str,
    where: str,
    args: tuple,
    timeout: float,
    require_outbound_radio: bool = False,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Poll an event query until rows appear or timeout (None on timeout).

    With require_outbound_radio the poll keeps waiting until a matching
    event also carries an outbound numeric radio native ref: the medre
    bridge writes that ref moments AFTER the canonical row lands (delivery
    confirmation lag), so first-sight of the event is too early for flows
    that need the bridge's own packet id.
    """
    deadline = time.monotonic() + timeout
    last_error = ""
    while True:
        try:
            with open_state(db) as conn:
                _require_tables(conn)
                events = _event_rows(conn, where, args)
        except sqlite3.Error as exc:
            events = []
            last_error = str(exc)
        if events and (not require_outbound_radio or _has_outbound_radio(events)):
            return events, last_error
        if time.monotonic() >= deadline:
            return None, last_error
        time.sleep(1.0)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def db_arg(sub_parser: argparse.ArgumentParser) -> None:
        sub_parser.add_argument("db")

    p = sub.add_parser("refs-for-native")
    db_arg(p)
    p.add_argument("native_id")
    p.add_argument("adapter", nargs="?", default=None)
    p.add_argument("--wait", type=float, default=0.0)

    p = sub.add_parser("events-for-native")
    db_arg(p)
    p.add_argument("native_id")
    p.add_argument("adapter", nargs="?", default=None)
    p.add_argument("--wait", type=float, default=0.0)
    p.add_argument(
        "--require-outbound-radio",
        action="store_true",
        help="keep polling until the event also holds an outbound numeric radio ref",
    )

    p = sub.add_parser("refs-for-event")
    db_arg(p)
    p.add_argument("event_id")
    p.add_argument("--wait", type=float, default=0.0)

    p = sub.add_parser("events-for-text")
    db_arg(p)
    p.add_argument("substr")
    p.add_argument("--wait", type=float, default=0.0)

    p = sub.add_parser("count")
    db_arg(p)
    p = sub.add_parser("dump")
    db_arg(p)
    p.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()

    def refs_where(native_id: str, adapter: str | None) -> tuple[str, tuple]:
        if adapter:
            return "adapter = ? AND native_message_id = ?", (adapter, native_id)
        return "native_message_id = ?", (native_id,)

    if args.command in {
        "refs-for-native",
        "events-for-native",
        "refs-for-event",
        "events-for-text",
    }:
        refs_predicate, refs_params = (
            refs_where(args.native_id, args.adapter)
            if args.command == "refs-for-native"
            else (None, None)
        )
        if args.command == "events-for-native":
            event_where, event_params = (
                "e.event_id IN (SELECT event_id FROM native_message_refs r WHERE"  # nosec B608 # Fixed predicates; values bound.
                f" {refs_where(args.native_id, args.adapter)[0]})",
                refs_where(args.native_id, args.adapter)[1],
            )
        elif args.command == "refs-for-event":
            event_where, event_params = "e.event_id = ?", (args.event_id,)
        elif args.command == "events-for-text":
            event_where, event_params = (
                "(e.payload LIKE ? OR e.event_id IN (SELECT event_id FROM"
                " event_body_content WHERE body LIKE ?))",
                (f"%{args.substr}%", f"%{args.substr}%"),
            )
        else:  # refs-for-native: poll via the joined event predicate
            event_where, event_params = (
                "e.event_id IN (SELECT event_id FROM native_message_refs r WHERE"  # nosec B608 # Fixed predicates; values bound.
                f" {refs_predicate})",
                refs_params,
            )

        if args.wait:
            events, last_error = wait_events(
                args.db,
                event_where,
                event_params,
                args.wait,
                require_outbound_radio=getattr(args, "require_outbound_radio", False),
            )
            if events is None:
                print(
                    json.dumps(
                        {
                            "rows": [],
                            "events": [],
                            "waited": True,
                            "db": args.db,
                            "error": last_error
                            or "timed out waiting for medre state row",
                        },
                        ensure_ascii=False,
                    )
                )
                return 1
        else:
            with open_state(args.db) as conn:
                _require_tables(conn)
                if args.command == "refs-for-native":
                    events = rows_to_dicts(
                        conn.execute(
                            f"SELECT r.* FROM native_message_refs r WHERE {refs_predicate}"  # nosec B608 # Fixed predicates; values bound.
                            " ORDER BY r.created_at",
                            refs_params,
                        )
                    )
                else:
                    events = _event_rows(conn, event_where, event_params)

        payload: dict[str, Any] = {"db": args.db, "events": events}
        if args.command == "refs-for-native":
            payload["rows"] = events
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if args.command == "count":
        with open_state(args.db) as conn:
            _require_tables(conn)
            events = conn.execute(
                "SELECT COUNT(*) AS n FROM canonical_events"
            ).fetchone()
            refs = conn.execute(
                "SELECT COUNT(*) AS n FROM native_message_refs"
            ).fetchone()
        print(
            json.dumps(
                {
                    "db": args.db,
                    "canonical_events": events["n"],
                    "native_message_refs": refs["n"],
                }
            )
        )
        return 0

    if args.command == "dump":
        with open_state(args.db) as conn:
            _require_tables(conn)
            events = _event_rows(
                conn,
                "e.event_id IN (SELECT event_id FROM canonical_events ORDER BY created_at DESC LIMIT ?)",
                (args.limit,),
            )
        print(json.dumps({"db": args.db, "events": events}, ensure_ascii=False))
        return 0

    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except sqlite3.Error as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
