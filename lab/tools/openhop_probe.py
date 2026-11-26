#!/usr/bin/env python3
"""Bounded MeshCore-protocol probe against the openHop companion.

This probe is the PROTOCOL-LEVEL evidence instrument for the openHop
qualification. It speaks the MeshCore frame protocol with the PINNED
upstream SDK (``meshcore==2.3.15``, the same distribution MEDRE's
medre-adapter-meshcore depends on) and performs exactly the handshake the
MEDRE adapter performs:

  1. TCP connect to the companion,
  2. APP_START (the SDK's ``connect()`` sends it),
  3. read SELF_INFO,
  4. read CHANNEL 0 (the SDK's ``get_channel(0)``),
  5. disconnect cleanly.

SINGLE-CLIENT RULE
------------------
openHop's companion server accepts ONE client at a time and EVICTS the
previous one. This probe therefore MUST only run while the MEDRE runtime is
STOPPED. The lifecycle check enforces that ordering; a probe run while
MEDRE holds the companion would silently break MEDRE's session.

WHAT IT PROVES / DOES NOT PROVE
-------------------------------
It proves that the companion terminates the MeshCore protocol handshake
(APP_START -> SELF_INFO) and answers a channel read. It does NOT prove that
MEDRE connected, that any message was delivered, or that anything was
transmitted over RF: the repeater runs with ``radio_type: null`` and its
NullRadio raises on every send. The MEDRE-side handshake evidence comes
from MEDRE's own logs (see lab/openhop/lifecycle_check.sh).

Output: a single JSON object on stdout. Exit code 0 on success, 1 on
failure (with the reason in the JSON).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

# Bounded waits. The companion answers locally; anything slower than this
# is a failure, not a slow success.
CONNECT_TIMEOUT_SECONDS = 20.0
COMMAND_TIMEOUT_SECONDS = 15.0
DISCONNECT_TIMEOUT_SECONDS = 10.0


async def probe(host: str, port: int) -> dict:
    try:
        import meshcore
    except ImportError as exc:  # pragma: no cover - environment contract
        return {
            "pass": False,
            "reason": f"pinned meshcore SDK unavailable: {exc}",
        }

    try:
        from importlib.metadata import version as _dist_version

        sdk_version = _dist_version("meshcore")
    except Exception:  # pragma: no cover - metadata is present in the image
        sdk_version = getattr(meshcore, "__version__", "unknown")

    client = None
    try:
        client = await asyncio.wait_for(
            meshcore.MeshCore.create_tcp(host, port, auto_reconnect=False),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {
            "pass": False,
            "reason": (
                f"connect to {host}:{port} timed out after "
                f"{CONNECT_TIMEOUT_SECONDS}s"
            ),
        }
    except Exception as exc:
        return {
            "pass": False,
            "reason": f"connect to {host}:{port} failed: {exc!r}",
        }

    if client is None:
        # The SDK returns None when the node does not answer the handshake.
        return {
            "pass": False,
            "reason": (
                f"{host}:{port} did not answer the MeshCore handshake "
                "(APP_START unanswered)"
            ),
        }

    evidence: dict = {
        "pass": False,
        "host": host,
        "port": port,
        "sdk_version": sdk_version,
    }
    try:
        # APP_START was sent by connect(); its answer is SELF_INFO, which the
        # SDK surfaces as an event AND as the self_info snapshot. The pinned
        # SDK's SELF_INFO payload carries the full 32-byte `public_key`.
        info = dict(getattr(client, "self_info", {}) or {})
        public_key = info.get("public_key")
        evidence["self_info"] = {
            "name": info.get("name"),
            "public_key": public_key,
            "radio_freq": info.get("radio_freq"),
        }
        if not info:
            return {
                **evidence,
                "reason": "APP_START answered without a SELF_INFO snapshot",
            }
        if not public_key:
            return {
                **evidence,
                "reason": "SELF_INFO carries no public_key",
            }

        try:
            channel_event = await asyncio.wait_for(
                client.commands.get_channel(0),
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return {
                **evidence,
                "reason": (
                    f"channel 0 read timed out after {COMMAND_TIMEOUT_SECONDS}s"
                ),
            }
        payload = getattr(channel_event, "payload", None)
        evidence["channel_0"] = {
            "event_type": str(getattr(channel_event, "type", "unknown")),
            "channel_name": (
                payload.get("channel_name") if isinstance(payload, dict) else None
            ),
            "channel_idx": (
                payload.get("channel_idx") if isinstance(payload, dict) else None
            ),
        }
        event_type = str(getattr(channel_event, "type", ""))
        if "CHANNEL_INFO" not in event_type:
            return {
                **evidence,
                "reason": f"channel 0 read returned {event_type!r}",
            }
    except Exception as exc:
        return {**evidence, "reason": f"protocol exchange failed: {exc!r}"}
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.disconnect(),
                    timeout=DISCONNECT_TIMEOUT_SECONDS,
                )
                evidence["clean_disconnect"] = True
            except Exception as exc:
                evidence["clean_disconnect"] = False
                evidence["disconnect_error"] = repr(exc)

    evidence["pass"] = True
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="openhop-repeater")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)
    result = asyncio.run(probe(args.host, args.port))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    sys.exit(main())
