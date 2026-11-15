"""Bootstrap the persistent LXMF counterpart identity (normal profile).

Runs INSIDE the medre-normal container (which bakes the pinned RNS/LXMF
stack):

    docker compose --profile normal run --rm medre-normal \
        python /opt/lab-tools/lxmf_peer_bootstrap.py /data-lxmf/peer.identity

Creates the counterpart's Reticulum identity once (or reloads the existing
one) and prints its LXMF delivery-destination hash. lab/up.sh records the
hash and substitutes it into the generated medre-normal config (the
`lxmf-bridge` route's dest_channel), so medre's outbound LXMF deliveries
address a STABLE counterpart identity across bring-ups.

The destination hash is the pure hash of (identity, "lxmf", "delivery") —
exactly the delivery destination LXMRouter.register_delivery_identity
creates for this identity — so no Reticulum instance is needed here.

Output: one line `LXMF_PEER_BOOTSTRAP_RESULT={json}` (the marker pattern
mirrors the medre-adapter-lxmf local-integration probes).
"""

from __future__ import annotations

import json
import os
import sys

RESULT_PREFIX = "LXMF_PEER_BOOTSTRAP_RESULT="
APP_NAME = "lxmf"
ASPECT = "delivery"  # LXMF.LXMF.APP_NAME / LXMRouter delivery aspect


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: lxmf_peer_bootstrap.py <identity-path>", file=sys.stderr)
        return 2
    identity_path = sys.argv[1]

    import RNS  # pinned via the baked medre-adapter-lxmf dependency spec

    os.makedirs(os.path.dirname(identity_path) or ".", exist_ok=True)
    if os.path.isfile(identity_path):
        identity = RNS.Identity.from_file(identity_path)
        if identity is None:
            print(f"FAILED to load identity from {identity_path}", file=sys.stderr)
            return 1
        created = False
    else:
        identity = RNS.Identity()
        identity.to_file(identity_path)
        created = True

    # The delivery destination hash LXMRouter will register for this identity
    # (RNS.Destination(identity, IN, SINGLE, "lxmf", "delivery")). The hash is
    # direction-independent, so computing it via the static hash is exact.
    destination_hash = RNS.Destination.hash(identity, APP_NAME, ASPECT)

    print(RESULT_PREFIX + json.dumps({
        "created": created,
        "identity_path": identity_path,
        "identity_hash": identity.hash.hex(),
        "destination_hash": destination_hash.hex(),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
