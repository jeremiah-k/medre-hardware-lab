"""Offline tests for the bridge proof's store-row selection.

The fixtures copy the REAL store shape captured by the failed bridge-proof
run (`payload` = ``{}`` on the openhop row with the nonce under
``metadata.native.data.meshcore.wire_text``, and the Meshtastic row with a
portnum payload and the nonce under ``metadata.native.data.meshtastic.
decoded.text``): the selection must match the nonce anywhere in the stored
row, count exactly one event per proof nonce, and associate receipts,
outbox rows and native refs by the selected event IDs.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "openhop_bridge_store_select.py"
)
spec = importlib.util.spec_from_file_location(
    "openhop_bridge_store_select", MODULE_PATH
)
assert spec and spec.loader
store_select = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store_select)

NONCE_MC = "openhop-bridge-mc2mesh-d5cb8b5d7edc4719b4a8486b6e066312"
NONCE_MESH = "openhop-bridge-mesh2mc-a2ebf096b5df427fbba855736a627aa0"


def meshcore_row(nonce: str = NONCE_MC, event_id: str = "evt-mc") -> dict:
    """The real openhop-direction row shape: payload empty, text in metadata."""
    return {
        "event_id": event_id,
        "event_kind": "message.created",
        "payload": "{}",
        "metadata": json.dumps(
            {
                "native": {
                    "data": {
                        "meshcore": {
                            "schema_version": 2,
                            "packet_id": 1795742300,
                            "channel": 0,
                            "classification": {
                                "action": "relay",
                                "category": "text",
                                "reason": "channel_text_packet",
                            },
                            "wire_text": f"MEDRE-RADIO-b: {nonce}",
                        }
                    }
                }
            }
        ),
        "source_adapter": "openhop-a",
        "source_channel_id": "0",
    }


def meshtastic_row(nonce: str = NONCE_MESH, event_id: str = "evt-mesh") -> dict:
    """The real radio-direction row shape: portnum payload, text in metadata."""
    return {
        "event_id": event_id,
        "event_kind": "message.created",
        "payload": '{"portnum":"text_message"}',
        "metadata": json.dumps(
            {
                "native": {
                    "data": {
                        "meshtastic": {
                            "schema_version": 1,
                            "packet_id": 5898555,
                            "from_id": "!4e89f472",
                            "channel": 0,
                            "decoded": {"text": nonce},
                        }
                    }
                }
            }
        ),
        "source_adapter": "radio-c",
        "source_channel_id": "0",
    }


def receipt(event_id: str, status: str = "sent") -> dict:
    return {
        "event_id": event_id,
        "receipt_id": f"rcpt-{event_id}",
        "receipt_kind": "attempt",
        "route_id": "openhop-bridge",
        "status": status,
    }


def outbox_row(event_id: str) -> dict:
    return {
        "event_id": event_id,
        "outbox_id": f"obox-{event_id}",
        "route_id": "openhop-bridge",
        "status": "sent",
    }


def ref_row(event_id: str) -> dict:
    return {"event_id": event_id, "direction": "inbound", "adapter": "openhop-a"}


def store(rows: list[dict]) -> dict:
    return {
        "canonical_events": rows,
        "delivery_receipts": [receipt(r["event_id"]) for r in rows],
        "delivery_outbox": [outbox_row(r["event_id"]) for r in rows],
        "native_message_refs": [ref_row(r["event_id"]) for r in rows],
    }


def test_both_real_shaped_rows_are_selected_exactly_once():
    result = store_select.select_nonce_events(
        store([meshcore_row(), meshtastic_row()]), {NONCE_MC, NONCE_MESH}
    )
    assert len(result["events"]) == 2
    assert result["events_per_nonce"] == {NONCE_MC: 1, NONCE_MESH: 1}
    assert {row["event_id"] for row in result["events"]} == {
        "evt-mc",
        "evt-mesh",
    }


def test_the_payload_column_alone_would_have_matched_nothing():
    """The regression: the old payload-column selector matched 0 rows."""
    rows = [meshcore_row(), meshtastic_row()]
    assert all(NONCE_MC not in (row.get("payload") or "") for row in rows)
    assert all(NONCE_MESH not in (row.get("payload") or "") for row in rows)


def test_associations_follow_the_selected_event_ids():
    result = store_select.select_nonce_events(
        store([meshcore_row(), meshtastic_row()]), {NONCE_MC, NONCE_MESH}
    )
    assert {row["event_id"] for row in result["receipts"]} == {
        "evt-mc",
        "evt-mesh",
    }
    assert {row["event_id"] for row in result["outbox"]} == {
        "evt-mc",
        "evt-mesh",
    }
    assert {row["event_id"] for row in result["refs"]} == {
        "evt-mc",
        "evt-mesh",
    }
    assert result["event_ids"] == {"evt-mc", "evt-mesh"}


def test_a_row_without_the_nonce_is_not_selected():
    rows = [meshcore_row(), meshtastic_row(), meshcore_row(event_id="evt-other")]
    rows[2]["metadata"] = json.dumps(
        {"native": {"data": {"meshcore": {"wire_text": "unrelated traffic"}}}}
    )
    result = store_select.select_nonce_events(
        {
            "canonical_events": rows,
            "delivery_receipts": [],
            "delivery_outbox": [],
            "native_message_refs": [],
        },
        {NONCE_MC, NONCE_MESH},
    )
    assert result["events_per_nonce"] == {NONCE_MC: 1, NONCE_MESH: 1}
    assert "evt-other" not in result["event_ids"]


def test_the_same_nonce_twice_is_reported_as_two():
    """The caller judges the count: two matches stay a strict failure."""
    rows = [
        meshcore_row(event_id="evt-a"),
        meshcore_row(event_id="evt-b"),
    ]
    result = store_select.select_nonce_events(
        {
            "canonical_events": rows,
            "delivery_receipts": [],
            "delivery_outbox": [],
            "native_message_refs": [],
        },
        {NONCE_MC, NONCE_MESH},
    )
    assert result["events_per_nonce"][NONCE_MC] == 2
    assert len(result["events"]) == 2


def test_a_nonce_with_no_match_reports_zero():
    result = store_select.select_nonce_events(
        store([meshcore_row()]), {NONCE_MC, NONCE_MESH}
    )
    assert result["events_per_nonce"][NONCE_MESH] == 0


def test_empty_and_missing_store_sections_do_not_crash():
    result = store_select.select_nonce_events({}, {NONCE_MC})
    assert result["events"] == []
    assert result["events_per_nonce"] == {NONCE_MC: 0}
    assert result["receipts"] == []
    assert result["outbox"] == []
    assert result["refs"] == []


def test_nonce_in_the_payload_column_is_also_selected():
    """A payload placement matches too: the selection is row-wide."""
    row = meshcore_row()
    row["metadata"] = "{}"
    row["payload"] = json.dumps({"text": NONCE_MC})
    result = store_select.select_nonce_events(
        {
            "canonical_events": [row],
            "delivery_receipts": [],
            "delivery_outbox": [],
            "native_message_refs": [],
        },
        {NONCE_MC},
    )
    assert result["events_per_nonce"] == {NONCE_MC: 1}


def test_empty_nonce_strings_are_ignored():
    result = store_select.select_nonce_events(store([meshcore_row()]), {NONCE_MC, ""})
    assert result["nonces"] == {NONCE_MC}
    assert result["events_per_nonce"] == {NONCE_MC: 1}
