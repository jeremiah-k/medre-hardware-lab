"""Verify each relay bot's Matrix device has a valid self-cross-signing chain.

Lifted from the upstream CI assert_matrix_device_cross_signed pattern
(keys/query + canonical-JSON Ed25519 verification via vodozemac), with the
canonical JSON encoding implemented locally so nio is not required.

Reads device IDs from /state/mmrelay-*/matrix/credentials.json (the relay
home volumes mounted read-only into lab-tools).
"""

from __future__ import annotations

import json
import sys

import vodozemac

from lab_lib import LAB_STATE_DIR, matrix_request

MMRELAY_DEVICE_COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def canonical_json(value: dict) -> bytes:
    """Matrix canonical JSON: sorted keys, no whitespace, UTF-8."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sole_ed25519_key(keys: dict, context: str) -> tuple[str, str]:
    matches = [
        (key_id, value)
        for key_id, value in keys.get("keys", {}).items()
        if key_id.startswith("ed25519:") and isinstance(value, str) and value
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one Ed25519 key for {context}, got {matches!r}")
    return matches[0]


def verify(public_key_b64: str, signed: dict, signature_b64: str) -> None:
    unsigned = {
        key: value for key, value in signed.items() if key not in ("signatures", "unsigned")
    }
    message = canonical_json(unsigned)
    public_key = vodozemac.Ed25519PublicKey.from_base64(public_key_b64)
    signature = vodozemac.Ed25519Signature.from_base64(signature_b64)
    public_key.verify_signature(message, signature)


def verify_device(admin_token: str, user_id: str, device_id: str) -> None:
    response = matrix_request(
        "POST",
        "/_matrix/client/v3/keys/query",
        admin_token,
        {"device_keys": {user_id: [device_id]}},
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"keys/query failed ({response.status_code}): {response.text[:300]}")
    payload = response.json()

    master = payload.get("master_keys", {}).get(user_id)
    self_signing = payload.get("self_signing_keys", {}).get(user_id)
    device = payload.get("device_keys", {}).get(user_id, {}).get(device_id)
    if not isinstance(master, dict):
        raise RuntimeError(f"Missing master cross-signing key for {user_id}")
    if not isinstance(self_signing, dict):
        raise RuntimeError(f"Missing self-signing key for {user_id}")
    if not isinstance(device, dict):
        raise RuntimeError(f"Missing device keys for {user_id} {device_id}")

    master_key_id, master_public = sole_ed25519_key(master, "master key")
    self_signing_key_id, self_signing_public = sole_ed25519_key(
        self_signing, "self-signing key"
    )
    self_signing_signature = (
        self_signing.get("signatures", {}).get(user_id, {}).get(master_key_id)
    )
    device_signature = device.get("signatures", {}).get(user_id, {}).get(self_signing_key_id)
    if not isinstance(self_signing_signature, str) or not self_signing_signature:
        raise RuntimeError("Self-signing key is not signed by the master key")
    if not isinstance(device_signature, str) or not device_signature:
        raise RuntimeError("Device is not signed by the self-signing key")

    verify(master_public, self_signing, self_signing_signature)
    verify(self_signing_public, device, device_signature)
    print(f"  cross-signing chain verified: {user_id} device {device_id}")


def main() -> int:
    runtime = json.load(
        open(f"{LAB_STATE_DIR}/runtime.json", encoding="utf-8")
    )
    admin_token = runtime["users"]["untrusted"]["access_token"]
    for index in range(ord("a"), ord("a") + MMRELAY_DEVICE_COUNT):
        relay = f"mmrelay-{chr(index)}"
        credentials_path = f"/state/{relay}/matrix/credentials.json"
        with open(credentials_path, encoding="utf-8") as handle:
            credentials = json.load(handle)
        verify_device(
            admin_token,
            credentials["user_id"],
            credentials["device_id"],
        )
    print("CROSSSIGN OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
