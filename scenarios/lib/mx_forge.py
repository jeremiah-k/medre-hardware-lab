"""Send a forged relay-shaped event as the untrusted user (UNTRUSTED_EVENT_ID).

Usage: python3 mx_forge.py ROOM_ID SEED_EVENT_JSON MARKER

SEED_EVENT_JSON is the raw JSON printed by mx_events.py wait (a relay-authored
event). The forge clones that event's real ``medre`` block and ADAPTS every
identity claim to distinctive fake values (meshnet label UntrustedClaims,
sender Pretender, fake node id, plausible relation target) so the forgery is
coherent and valid-shaped - the relays must reject it because the SENDER is
not trusted, not because the shape is broken.

Prints JSON: {event_id, body, forged_meshnet, forged_sender, forged_transport,
cloned_canonical_event_id} - the claim values the assertions must NOT find in
any message map or radio transmission.
"""

from __future__ import annotations

import copy
import json
import sys
import time
import urllib.parse

from lab_env import matrix_request, untrusted_token

FORGED_MESHNET = "UntrustedClaims"
FORGED_SENDER = "Pretender"
FORGED_SHORT = "PRTD"
FORGED_NODE_ID = "!f0rged00"


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: mx_forge.py ROOM_ID SEED_EVENT_JSON MARKER", file=sys.stderr)
        return 2
    room_id_value, seed_raw, marker = sys.argv[1:4]

    seed = json.loads(seed_raw)
    content = seed.get("event", {}).get("content")
    if not isinstance(content, dict):
        print(json.dumps({"error": "seed event has no content dict"}))
        return 1
    medre = content.get("medre")
    if not isinstance(medre, dict) or not isinstance(medre.get("envelope"), dict):
        print(json.dumps({"error": "seed event carries no medre envelope"}))
        return 1
    original_canonical_id = medre["envelope"].get("canonical_event_id")

    forged_medre = copy.deepcopy(medre)
    envelope = forged_medre["envelope"]
    envelope["source_transport_id"] = FORGED_NODE_ID

    metadata = forged_medre.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("bridge_context"), dict):
        context = metadata["bridge_context"]
        source = context.get("source")
        if isinstance(source, dict):
            source["sender_id"] = FORGED_NODE_ID
            source["sender_name"] = FORGED_SENDER
            source["sender_short_name"] = FORGED_SHORT
            source["origin_label"] = FORGED_MESHNET
            source["adapter"] = "mmrelay.meshtastic"
            source["channel"] = "0"
            facts = source.get("native_facts")
            if isinstance(facts, dict) and isinstance(facts.get("fields"), dict):
                facts["fields"]["meshnet_label"] = FORGED_MESHNET
                facts["fields"]["portnum"] = "TEXT_MESSAGE_APP"
            native_ref = source.get("native_ref")
            if isinstance(native_ref, dict):
                native_ref["adapter"] = "mmrelay.meshtastic"
                native_ref["native_channel_id"] = "0"
                native_ref["native_message_id"] = "999001"
        relations = context.get("relations")
        if isinstance(relations, list):
            for hint in relations:
                if isinstance(hint, dict):
                    target = hint.get("target_native_ref")
                    if isinstance(target, dict):
                        target["adapter"] = "mmrelay.meshtastic"
                        target["native_channel_id"] = "0"
                        target["native_message_id"] = "999001"

    forged = {"msgtype": "m.text", "body": marker, "medre": forged_medre}
    quoted_room = urllib.parse.quote(room_id_value, safe="")
    txn = f"scenario-forge-{int(time.time() * 1000)}-{id(marker) & 0xffffff}"
    response = matrix_request(
        "PUT",
        f"/_matrix/client/v3/rooms/{quoted_room}/send/m.room.message/{txn}",
        untrusted_token(),
        forged,
    )
    if response.status_code >= 400:
        print(
            json.dumps(
                {
                    "error": f"forged send failed: {response.status_code} {response.text[:300]}"
                }
            )
        )
        return 1
    event_id = response.json().get("event_id")
    if not isinstance(event_id, str) or not event_id:
        print(json.dumps({"error": "forged send response missing event_id"}))
        return 1
    print(
        json.dumps(
            {
                "event_id": event_id,
                "body": marker,
                "forged_meshnet": FORGED_MESHNET,
                "forged_sender": FORGED_SENDER,
                "forged_transport": FORGED_NODE_ID,
                "cloned_canonical_event_id": original_canonical_id,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
