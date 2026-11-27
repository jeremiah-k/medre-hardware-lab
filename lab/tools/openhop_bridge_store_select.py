#!/usr/bin/env python3
"""Select the bridge proof's store rows from MEDRE's mapping-store snapshot.

WHY THE MATCH IS ROW-WIDE
-------------------------
The bridged message's text lives in the canonical event's METADATA - the
adapter records the native frame under
``metadata.native.data.meshcore.wire_text`` (openhop direction) and
``metadata.native.data.meshtastic.decoded.text`` (radio direction) - while
the ``payload`` column carries ``"{}"`` or a ``{"portnum": ...}`` object.
Selecting on the payload column alone never matches the bridged text, so a
canonical event is selected when the nonce appears ANYWHERE in the stored
row: the canonical JSON of the whole row, payload and metadata alike.

The selection stays strict: every row whose canonical JSON contains a nonce
is selected, and the per-nonce counts are reported so the caller can judge
"exactly one canonical event per proof nonce" - zero or two matches stay a
failure. Receipts, outbox rows and native refs associate by the selected
events' IDs; a receipt can commit after the peer has observed delivery.

Pure function over the store snapshot; no Docker, no store access.
"""

from __future__ import annotations

import json


def nonce_in_row(nonce: str, row: dict) -> bool:
    """Return whether the nonce appears anywhere in the stored row.

    The row is serialised to its canonical JSON (sorted keys), so the
    nonce is matched in the payload column and the metadata column alike.
    """
    return nonce in json.dumps(row, sort_keys=True)


def select_nonce_events(state: dict, nonces) -> dict:
    """Select the proof's rows from a store snapshot.

    ``nonces`` is any iterable of proof nonces; duplicates collapse. The
    result carries the selected rows of each store section, the per-nonce
    canonical-event counts, and the selected event IDs.
    """
    wanted = {nonce for nonce in nonces if nonce}
    events = [
        row
        for row in state.get("canonical_events") or []
        if any(nonce_in_row(nonce, row) for nonce in wanted)
    ]
    event_ids = {row["event_id"] for row in events}
    return {
        "nonces": wanted,
        "events": events,
        "event_ids": event_ids,
        "events_per_nonce": {
            nonce: sum(1 for row in events if nonce_in_row(nonce, row))
            for nonce in wanted
        },
        "receipts": [
            row
            for row in state.get("delivery_receipts") or []
            if row.get("event_id") in event_ids
        ],
        "outbox": [
            row
            for row in state.get("delivery_outbox") or []
            if row.get("event_id") in event_ids
        ],
        "refs": [
            row
            for row in state.get("native_message_refs") or []
            if row.get("event_id") in event_ids
        ],
    }
