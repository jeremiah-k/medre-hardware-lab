#!/usr/bin/env bash
# =============================================================================
# Phase 3B bridge bring-up (openHop radio overlay + MEDRE + MeshC).
#
# Topology (all on private compose networks, no host ports):
#
#   virtual MC peer (SDK) --TCP 5000--> openhop-repeater-b --+
#                                                             | modem relay (lab)
#   MEDRE (meshcore) --TCP 5000--------> openhop-repeater-a --+
#                                                             | TCP 4403
#                             meshtasticd-c-medre -- MeshC -- meshtasticd-c-peer
#
# MEDRE is the ONLY client of companion A and the only API client of
# meshtasticd-c-medre; the virtual MC peer is the ONLY client of companion
# B and the observer the only API client of meshtasticd-c-peer.
#
# The repeater configs (identities, credentials) are the SAME ones the
# Phase 3A virtual-radio profile generates: they are per-instance
# credentials in the gitignored lab/state tree, reused so the bridge rig and
# the packet-proof rig describe the same two nodes.
#
# Usage: lab/openhop-bridge/bridge_up.sh [--skip-build]
# =============================================================================
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${LAB_ROOT}"

SKIP_BUILD=0
if [[ ${1:-} == "--skip-build" ]]; then
	SKIP_BUILD=1
fi

COMPOSE=(docker compose --profile openhop-bridge)
LOG_DIR="lab/logs/openhop-bridge"
TS="$(date +%Y%m%d-%H%M%S)"

log() { printf '\n=== %s ===\n' "$*"; }
die() {
	echo "ERROR: $*" >&2
	exit 1
}

# --- Environment ------------------------------------------------------------
[[ -f .env ]] || die "Run 'medre-lab docker prepare' first"
# shellcheck disable=SC1091
source .env
: "${LAB_SOURCE_MANIFEST:?Run 'medre-lab docker prepare' first}"

for pair in \
	"${OPENHOP_CORE_SRC}:openhop_core" \
	"${OPENHOP_REPEATER_SRC}:openhop_repeater" \
	"${MEDRE_OPENHOP_SRC}:medre" \
	"${MEDRE_MESHCORE_SRC}:medre-adapter-meshcore" \
	"${MEDRE_MESHTASTIC_OPENHOP_SRC}:medre-adapter-meshtastic"; do
	dir="${pair%%:*}"
	label="${pair##*:}"
	[[ -d ${dir} ]] || die "source checkout ${dir} (${label}) is missing"
done

OPENHOP_CORE_REF_HEAD="$(git -C "${OPENHOP_CORE_SRC}" rev-parse HEAD)"
export OPENHOP_CORE_REF="${OPENHOP_CORE_REF_HEAD}"
OPENHOP_REPEATER_REF_HEAD="$(git -C "${OPENHOP_REPEATER_SRC}" rev-parse HEAD)"
export OPENHOP_REPEATER_REF="${OPENHOP_REPEATER_REF_HEAD}"
MEDRE_OPENHOP_REF_HEAD="$(git -C "${MEDRE_OPENHOP_SRC}" rev-parse HEAD)"
export MEDRE_OPENHOP_REF="${MEDRE_OPENHOP_REF_HEAD}"
MEDRE_MESHCORE_REF_HEAD="$(git -C "${MEDRE_MESHCORE_SRC}" rev-parse HEAD)"
export MEDRE_MESHCORE_REF="${MEDRE_MESHCORE_REF_HEAD}"
MEDRE_MESHTASTIC_OPENHOP_REF_HEAD="$(git -C "${MEDRE_MESHTASTIC_OPENHOP_SRC}" rev-parse HEAD)"
export MEDRE_MESHTASTIC_OPENHOP_REF="${MEDRE_MESHTASTIC_OPENHOP_REF_HEAD}"
echo "pinned source checkouts:"
echo "  openhop_core:       ${OPENHOP_CORE_REF}"
echo "  openhop_repeater:   ${OPENHOP_REPEATER_REF}"
echo "  medre (openhop):    ${MEDRE_OPENHOP_REF}"
echo "  adapter_meshcore:   ${MEDRE_MESHCORE_REF}"
echo "  adapter_meshtastic: ${MEDRE_MESHTASTIC_OPENHOP_REF}"

python3 lab/check_source_mounts.py "${LAB_SOURCE_MANIFEST_OPENHOP}" --host-only ||
	die "openHop source provenance check failed"

# --- Build ------------------------------------------------------------------
if ((! SKIP_BUILD)); then
	log "Building images (openhop repeater + medre openhop)"
	"${COMPOSE[@]}" build openhop-repeater medre-openhop-bridge
else
	log "Verifying pinned packages in the existing repeater image"
	docker run --rm --network none --entrypoint python3 medre-lab/openhop-repeater:latest -I -c '
import json
import sys
from pathlib import Path
from sysconfig import get_path

stamp = json.loads((Path(get_path("purelib")) / "openhop-bake-pin.json").read_text())
if [stamp[key]["ref"] for key in ("openhop_core", "openhop_repeater")] != sys.argv[1:]:
    raise SystemExit("existing image does not match the selected openHop pins")
' "${OPENHOP_CORE_REF}" "${OPENHOP_REPEATER_REF}" ||
		die "rebuild the repeater image with the selected revisions"
fi

# --- Generated configs ------------------------------------------------------
log "Generating the bridge configs into lab/state/config/"
mkdir -p lab/state/config lab/state/openhop-radio

# The repeater identities/credentials are the Phase 3A ones (per-instance
# secrets in the gitignored tree); generate them the same way if absent.
RADIO_SECRETS="lab/state/openhop-radio/secrets.env"
if [[ ! -f ${RADIO_SECRETS} ]]; then
	python3 - "${RADIO_SECRETS}" <<'PYSECRETS'
import os
import secrets
import sys

os.umask(0o077)
path = sys.argv[1]
if os.path.exists(path):
    raise SystemExit(f"refusing to overwrite {path}")
values = {}
for instance in ("a", "b"):
    values[f"RADIO_{instance.upper()}_COMPANION_IDENTITY_KEY"] = secrets.token_bytes(32).hex()
    values[f"RADIO_{instance.upper()}_ADMIN_PASSWORD"] = secrets.token_urlsafe(24)
    values[f"RADIO_{instance.upper()}_GUEST_PASSWORD"] = secrets.token_urlsafe(24)
    values[f"RADIO_{instance.upper()}_JWT_SECRET"] = secrets.token_hex(32)
with open(path, "x", encoding="utf-8") as handle:
    for key, value in values.items():
        handle.write(f"{key}={value}\n")
os.chmod(path, 0o600)
print(f"  generated {path} (mode 0600, gitignored)")
PYSECRETS
else
	echo "  ${RADIO_SECRETS} present, reusing it"
fi
# shellcheck disable=SC1090
source "${RADIO_SECRETS}"
export RADIO_A_COMPANION_IDENTITY_KEY RADIO_A_ADMIN_PASSWORD \
	RADIO_A_GUEST_PASSWORD RADIO_A_JWT_SECRET
export RADIO_B_COMPANION_IDENTITY_KEY RADIO_B_ADMIN_PASSWORD \
	RADIO_B_GUEST_PASSWORD RADIO_B_JWT_SECRET

python3 - <<'PYGEN'
import os
import sys

os.umask(0o077)
template = "lab/config/openhop-radio-repeater.yaml"
for instance in ("a", "b"):
    prefix = f"RADIO_{instance.upper()}_"
    replacements = {
        "__RADIO_INSTANCE__": instance,
        "__RADIO_COMPANION_IDENTITY_KEY__": os.environ[prefix + "COMPANION_IDENTITY_KEY"],
        "__RADIO_ADMIN_PASSWORD__": os.environ[prefix + "ADMIN_PASSWORD"],
        "__RADIO_GUEST_PASSWORD__": os.environ[prefix + "GUEST_PASSWORD"],
        "__RADIO_JWT_SECRET__": os.environ[prefix + "JWT_SECRET"],
    }
    with open(template, encoding="utf-8") as handle:
        text = handle.read()
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    for placeholder in replacements:
        if placeholder in text:
            raise SystemExit(f"unsubstituted placeholder remains in {template}: {placeholder}")
    target = f"lab/state/config/openhop-radio-repeater-{instance}.yaml"
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(target, 0o600)
    print(f"  {target} generated")
PYGEN

# The MEDRE bridge config carries no secret: it is copied verbatim.
cp lab/config/medre-openhop-bridge.yaml lab/state/config/medre-openhop-bridge.yaml
echo "  lab/state/config/medre-openhop-bridge.yaml generated"

# The expected openHop wire prefix on the direction-2 arrival is repeater
# A's COMPANION node name (the sending side stamps it on the wire).
EXPECT_PREFIX="$(
	python3 - <<'PYPREFIX'
import yaml

with open("lab/state/config/openhop-radio-repeater-a.yaml", encoding="utf-8") as handle:
    config = yaml.safe_load(handle)
for companion in config["identities"]["companions"]:
    print(f"{companion['settings']['node_name']}: ")
PYPREFIX
)"
echo "  expected direction-2 wire prefix: ${EXPECT_PREFIX}"
export OPENHOP_BRIDGE_EXPECT_PREFIX="${EXPECT_PREFIX}"

# --- Bring-up ---------------------------------------------------------------
log "Starting the virtual radio relay and the two openHop repeaters"
"${COMPOSE[@]}" up -d openhop-radio-relay openhop-repeater-a openhop-repeater-b

log "Starting the MeshC sim nodes"
"${COMPOSE[@]}" up -d meshtasticd-c-medre meshtasticd-c-peer

# --- MeshC channel configuration --------------------------------------------
# The MeshC sim nodes must share a channel-0 secret so MEDRE's relayed text
# and the peer node's injection are mutually decryptable. Both nodes get the
# same lab values, exactly like the normal profile does.
log "Configuring the MeshC channels (primary + secondary)"
# shellcheck disable=SC1091
source lab/config/meshnet.env
for node in meshtasticd-c-medre meshtasticd-c-peer; do
	for entry in \
		"0 name ${MESH_C_PRIMARY_NAME}" \
		"0 psk ${MESH_C_PRIMARY_PSK}" \
		"${MESH_C_SECONDARY_INDEX} name ${MESH_C_SECONDARY_NAME}" \
		"${MESH_C_SECONDARY_INDEX} psk ${MESH_C_SECONDARY_PSK}"; do
		read -r idx key value <<<"${entry}"
		ok=0
		for attempt in 1 2 3; do
			if "${COMPOSE[@]}" run --rm --no-deps openhop-bridge-probe \
				python3 -m meshtastic --timeout 25 --host "${node}" \
				--ch-set "${key}" "${value}" --ch-index "${idx}" >/dev/null 2>&1; then
				ok=1
				break
			fi
			echo "  [retry ${attempt}] ch-set ${key} on ${node} idx ${idx} failed"
			sleep 5
		done
		((ok == 1)) || die "channel configuration failed for ${node} (index ${idx}, ${key})"
	done
done

log "Starting MEDRE (meshcore -> openhop-repeater-a, meshtastic -> meshtasticd-c-medre)"
"${COMPOSE[@]}" up -d medre-openhop-bridge

wait_healthy() {
	local container=$1 limit=$2 waited=0 state
	while ((waited < limit)); do
		state="$(docker inspect -f '{{.State.Health.Status}}' "${container}" 2>/dev/null || echo missing)"
		case "${state}" in
		healthy)
			echo "  ${container}: healthy (${waited}s)"
			return 0
			;;
		unhealthy)
			die "${container} became unhealthy"
			;;
		esac
		sleep 5
		waited=$((waited + 5))
	done
	die "${container} did not become healthy within ${limit}s"
}

wait_healthy medre-lab-openhop-radio-relay 120
wait_healthy medre-lab-openhop-repeater-a 240
wait_healthy medre-lab-openhop-repeater-b 240
wait_healthy medre-lab-mesh-c-medre 240
wait_healthy medre-lab-mesh-c-peer 240
wait_healthy medre-lab-medre-openhop-bridge 240

# Process liveness is weaker than adapter readiness. Use only log lines
# from the current process, and wait for both endpoints to connect.
log "Waiting for the MEDRE adapter sessions to attach"
bridge=medre-lab-medre-openhop-bridge
started_at="$(docker inspect -f '{{.State.StartedAt}}' "${bridge}")"
ready=0
for attempt in $(seq 1 240); do
	if startup="$(docker logs --since "${started_at}" "${bridge}" 2>&1)" &&
		grep -Eq 'MeshCoreSession openhop-a( started .*connected=True|: reconnected successfully)' <<<"${startup}" &&
		grep -q 'MeshtasticSession radio-c started (mode=tcp)' <<<"${startup}"; then
		ready=1
		break
	fi
	sleep 1
done
((ready)) || die "MEDRE did not connect both adapters within 240s"

mkdir -p "${LOG_DIR}"
docker logs medre-lab-openhop-radio-relay >"${LOG_DIR}/relay-${TS}.log" 2>&1 || true
docker logs medre-lab-openhop-repeater-a >"${LOG_DIR}/repeater-a-${TS}.log" 2>&1 || true
docker logs medre-lab-openhop-repeater-b >"${LOG_DIR}/repeater-b-${TS}.log" 2>&1 || true
docker logs medre-lab-medre-openhop-bridge >"${LOG_DIR}/medre-openhop-bridge-${TS}.log" 2>&1 || true

# --- Version record ----------------------------------------------------------
# The pins live in the shared "openhop_sources" section (all openHop rigs
# use the same checkouts); the bridge section describes THIS rig. Both
# REPLACE any earlier section of the same name (lab/version_record.py), so
# the record never carries a stale section from a previous bring-up.
# lab/openhop_pins.py derives the pins from the source manifest AND the
# live checkouts, and classifies each as upstream or local-unpushed.
python3 lab/openhop_pins.py "${LAB_SOURCE_MANIFEST_OPENHOP}" \
	--record lab/VERSION >/dev/null ||
	die "could not record the openHop pins"

# The image IDs are the ones that actually ran; the build args are the ones
# the images were built with.
OPENHOP_IMAGE_ID="$(docker image inspect --format '{{.Id}}' medre-lab/openhop-repeater:latest 2>/dev/null || echo unknown)"
MEDRE_IMAGE_ID="$(docker image inspect --format '{{.Id}}' medre-lab/medre-openhop-dev:latest 2>/dev/null || echo unknown)"
RELAY_IMAGE_ID="$(docker image inspect --format '{{.Id}}' medre-lab/openhop-radio-relay:latest 2>/dev/null || echo unknown)"

python3 lab/version_record.py set openhop_bridge <<EOF
  source_pins: see the openhop_sources section (the SAME checkouts this rig builds and runs from)
  image_openhop: medre-lab/openhop-repeater:latest (${OPENHOP_IMAGE_ID}; lab/openhop/Dockerfile; local checkouts installed in order: openhop_core, then openhop_repeater)
  image_medre: medre-lab/medre-openhop-dev:latest (${MEDRE_IMAGE_ID}; lab/medre-openhop/Dockerfile; medre 0.2.3 + meshcore 0.2.0 + meshtastic 0.2.1)
  image_relay: medre-lab/openhop-radio-relay:latest (${RELAY_IMAGE_ID}; lab/openhop-radio/Dockerfile; lab-owned asyncio service speaking the openHop Modem protocol)
  build_args: OPENHOP_CORE_REF=${OPENHOP_CORE_REF} OPENHOP_REPEATER_REF=${OPENHOP_REPEATER_REF} (the exact HEAD SHAs of the checkouts named in openhop_sources)
  topology: MEDRE (meshcore -> openhop-repeater-a:5000, meshtastic -> meshtasticd-c-medre:4403) on the openhop-radio + mesh-c networks (MeshC simulators also publish to host loopback)
  route: openhop-a <-> radio-c, bidirectional, enabled ONLY in lab/config/medre-openhop-bridge.yaml
  radio: VIRTUAL (the lab relay simulates the RF medium; the MeshCore packets crossing it are real)
  evidence: lab/logs/openhop-bridge/ (bridge-proof.json, evidence-mc-to-mesh.json, evidence-mesh-to-mc.json, verdict.json)
EOF
# Drop the legacy key name and any duplicated keys a previous bring-up left
# behind, so the record carries exactly one section per rig.
python3 lab/version_record.py remove openhop_bridge_profile lab/VERSION
python3 lab/version_record.py compact lab/VERSION

log "openHop bridge profile is UP"
cat <<EOF
  repeater A:   medre-lab-openhop-repeater-a (MEDRE is its only companion client)
  repeater B:   medre-lab-openhop-repeater-b (the virtual MC peer is its only companion client)
  relay:        medre-lab-openhop-radio-relay (openHop Modem protocol, private network only)
  MEDRE:        medre-lab-medre-openhop-bridge
  MeshC nodes:  medre-lab-mesh-c-medre (MEDRE's API client), medre-lab-mesh-c-peer (observer)
  Proof:        lab/openhop-bridge/bridge_proof.sh
  Teardown:     lab/down.sh
EOF
