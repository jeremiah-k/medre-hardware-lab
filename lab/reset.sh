#!/usr/bin/env bash
# =============================================================================
# Wipe all interop-lab state for a clean bring-up: stops containers, removes
# named volumes (synapse data, relay state, node DBs) and clears lab/state +
# lab/logs. Called by lab/up.sh --reset.
# =============================================================================
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${LAB_ROOT}"

docker compose down --volumes --remove-orphans

rm -rf lab/state/config lab/state/runtime.json lab/state/scenario.env
mkdir -p lab/state/config
find lab/logs -mindepth 1 ! -name .gitkeep -delete

echo "Lab state wiped (volumes, lab/state, lab/logs)."
