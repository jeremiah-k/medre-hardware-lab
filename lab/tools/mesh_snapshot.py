"""Snapshot meshtasticd node identities into lab runtime.json.

Reads each node's node DB via the mtjk Python API (the dict keys are the
!hex node ids). Records the local node ID per node and whether its meshnet
peers are visible — the over-air mesh proof for the node pairs
(AlphaLab: meshtasticd-a + meshtasticd-a-peer, BetaLab: meshtasticd-b +
meshtasticd-b-peer). Node DB propagation after bring-up can take a couple of
minutes (periodic NodeInfo broadcasts), so visibility is polled briefly.

Under LAB_MEDRE_PROFILE=1 the dedicated medre nodes are included as third
members of each virtual meshnet (AlphaLab: + meshtasticd-a-medre, BetaLab:
+ meshtasticd-b-medre). Under LAB_NORMAL_PROFILE=1 the normal-profile MeshC
pair is included instead (GammaLab: meshtasticd-c-medre +
meshtasticd-c-peer). The snapshot runs BEFORE any relay/medre container
starts, so the API-client exclusivity of the relay-connected nodes is never
violated.
"""

from __future__ import annotations

import json
import os
import sys
import time

from lab_lib import runtime_path

# host -> expected over-air peers within the same virtual meshnet
NODE_PEERS = {
    "meshtasticd-a": ["meshtasticd-a-peer"],
    "meshtasticd-a-peer": ["meshtasticd-a"],
    "meshtasticd-b": ["meshtasticd-b-peer"],
    "meshtasticd-b-peer": ["meshtasticd-b"],
}

if os.environ.get("LAB_MEDRE_PROFILE", "") == "1":
    NODE_PEERS["meshtasticd-a-medre"] = ["meshtasticd-a", "meshtasticd-a-peer"]
    NODE_PEERS["meshtasticd-a"] = ["meshtasticd-a-peer", "meshtasticd-a-medre"]
    NODE_PEERS["meshtasticd-a-peer"] = ["meshtasticd-a", "meshtasticd-a-medre"]
    NODE_PEERS["meshtasticd-b-medre"] = ["meshtasticd-b", "meshtasticd-b-peer"]
    NODE_PEERS["meshtasticd-b"] = ["meshtasticd-b-peer", "meshtasticd-b-medre"]
    NODE_PEERS["meshtasticd-b-peer"] = ["meshtasticd-b", "meshtasticd-b-medre"]

# NORMAL profile: MeshC/GammaLab is medre-normal's OWN meshnet (the a/b-medre
# stress nodes are DOWN under --normal, so their entries must not be added).
if os.environ.get("LAB_NORMAL_PROFILE", "") == "1":
    NODE_PEERS["meshtasticd-c-medre"] = ["meshtasticd-c-peer"]
    NODE_PEERS["meshtasticd-c-peer"] = ["meshtasticd-c-medre"]

VISIBILITY_WAIT_SECONDS = 90


def snapshot_host(host: str) -> tuple[str | None, list[str]]:
    from meshtastic.tcp_interface import TCPInterface

    iface = TCPInterface(hostname=host, portNumber=4403)
    try:
        time.sleep(4)
        node_db = iface.nodes or {}
        node_ids = set(node_db.keys())
        my_info = iface.getMyNodeInfo() or {}
        my_id = my_info.get("user", {}).get("id") if "user" in my_info else None
        if my_id is None:
            my_id = my_info.get("id")
        if my_id is None:
            my_num = iface.myNodeNum
            my_id = f"!{my_num:08x}" if my_num else None
        return my_id, sorted(node_ids)
    finally:
        iface.close()


def main() -> int:
    hosts = list(NODE_PEERS)
    node_ids: dict[str, str] = {}
    node_db_cache: dict[str, list[str]] = {}
    for host in hosts:
        try:
            my_id, db_ids = snapshot_host(host)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  {host}: not reachable ({exc})")
            continue
        if my_id:
            node_ids[host] = my_id
            node_db_cache[host] = db_ids
            print(f"  {host}: id={my_id} nodedb_size={len(db_ids)}")

    if len(node_ids) < len(hosts):
        print("MESH SNAPSHOT FAIL: some nodes not reachable or without identity")
        return 1

    # Poll for over-air visibility (NodeDB propagation is periodic).
    deadline = time.monotonic() + VISIBILITY_WAIT_SECONDS
    visibility: dict[str, list[str]] = {}
    while True:
        for host, peer_hosts in NODE_PEERS.items():
            if host in visibility and visibility[host]:
                continue
            expected = [node_ids[p] for p in peer_hosts if p in node_ids]
            seen = [p for p, pid in zip(peer_hosts, expected, strict=False) if pid in node_db_cache.get(host, [])]
            visibility.setdefault(host, seen)
        if all(visibility.get(h) for h in NODE_PEERS) or time.monotonic() > deadline:
            break
        # refresh node DBs of hosts still missing their peer
        for host in NODE_PEERS:
            if not visibility.get(host):
                try:
                    _my, db_ids = snapshot_host(host)
                    node_db_cache[host] = db_ids
                except Exception:  # noqa: BLE001 - keep polling
                    pass
        time.sleep(10)

    snapshot: dict[str, dict] = {}
    for host, node_id in node_ids.items():
        snapshot[host] = {
            "node_id": node_id,
            "peers_visible": visibility.get(host, []),
        }
        print(f"  {host}: peers_visible={visibility.get(host, [])}")

    with open(runtime_path(), encoding="utf-8") as handle:
        runtime = json.load(handle)
    runtime["mesh_nodes"] = snapshot
    with open(runtime_path(), "w", encoding="utf-8") as handle:
        json.dump(runtime, handle, indent=2)
        handle.write("\n")

    print("MESH SNAPSHOT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
