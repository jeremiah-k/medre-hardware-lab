"""Read the echo_observed plugin's observation log.

Runs INSIDE the lab-tools container. The plugin (lab/plugins/echo_observed.py)
appends one JSON object per observed event to a per-relay file whose path the
rig publishes through PLUGIN_OBS_<X> (scenarios/config.env).

Subcommands:
    tail PATH [--lines N]      print the last N records as a JSON array
    find  PATH --event-id E [--timeout S]
                               poll until a record with that event_id appears;
                               print the record, exit 1 on timeout
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any


def read_records(path: str) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    tail = sub.add_parser("tail")
    tail.add_argument("path")
    tail.add_argument("--lines", type=int, default=10)

    find = sub.add_parser("find")
    find.add_argument("path")
    find.add_argument("--event-id", required=True)
    find.add_argument("--timeout", type=float, default=60.0)

    args = parser.parse_args()

    if args.command == "tail":
        records = read_records(args.path)[-args.lines :]
        print(json.dumps({"path": args.path, "records": records}, ensure_ascii=False))
        return 0

    deadline = time.monotonic() + args.timeout
    while True:
        for record in reversed(read_records(args.path)):
            if record.get("event_id") == args.event_id:
                print(
                    json.dumps(
                        {"record": record, "path": args.path}, ensure_ascii=False
                    )
                )
                return 0
        if time.monotonic() >= deadline:
            records = read_records(args.path)
            print(
                json.dumps(
                    {
                        "error": f"event {args.event_id} not observed in {args.path}",
                        "records_seen": len(records),
                        "tail": records[-5:],
                    },
                    ensure_ascii=False,
                )
            )
            return 1
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
