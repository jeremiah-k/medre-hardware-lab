#!/usr/bin/env bash
# =============================================================================
# Stop the interop lab. State volumes are KEPT (bring-up is resumable).
# To wipe state for a clean bring-up, use lab/reset.sh instead.
# =============================================================================
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${LAB_ROOT}"

docker compose down --remove-orphans
echo "Lab stopped. Volumes kept; run lab/up.sh to resume, lab/reset.sh to wipe."
