"""Publish complete fault settings for the lab's live proxy."""

import json
import os
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_fault(path: Path, fault: dict) -> None:
    payload = dict(fault)
    if payload.get("stall_server_to_client_on_request"):
        payload["request_stall_generation"] = uuid.uuid4().hex
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
