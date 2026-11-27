#!/usr/bin/env bash
# =============================================================================
# hardware-lab bring-up: synapse -> provision -> relay auth/cross-signing ->
# meshtasticd node pairs + channels -> relays -> smoke test.
#
# Topology: meshnet AlphaLab = meshtasticd-a + meshtasticd-a-peer (mmrelay-a);
# meshnet BetaLab = meshtasticd-b + meshtasticd-b-peer (mmrelay-b). The relay
# connects only to its primary node; lab-tools injects/witnesses via peers.
#
# Idempotent: safe to re-run; existing state (credentials, rooms, volumes) is
# reused. Use lab/reset.sh for a clean slate.
#
# Usage: lab/up.sh [--reset] [--skip-build] [--skip-smoke] [--with-medre] [--normal]
# =============================================================================
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${LAB_ROOT}"

DO_RESET=0
SKIP_BUILD=0
SKIP_SMOKE=0
WITH_MEDRE=0
WITH_NORMAL=0
WITH_OPENHOP=0
WITH_OPENHOP_RADIO=0
for arg in "$@"; do
	case "${arg}" in
	--reset) DO_RESET=1 ;;
	--skip-build) SKIP_BUILD=1 ;;
	--skip-smoke) SKIP_SMOKE=1 ;;
	--with-medre) WITH_MEDRE=1 ;;
	--normal) WITH_NORMAL=1 ;;
	--openhop) WITH_OPENHOP=1 ;;
	--openhop-radio) WITH_OPENHOP_RADIO=1 ;;
	*)
		echo "Unknown option: ${arg}" >&2
		echo "Usage: lab/up.sh [--reset] [--skip-build] [--skip-smoke] [--with-medre] [--normal] [--openhop] [--openhop-radio]" >&2
		exit 2
		;;
	esac
done

if ((WITH_MEDRE)) && ((WITH_NORMAL)); then
	echo "ERROR: --with-medre and --normal are mutually exclusive" >&2
	exit 2
fi

# The openHop profile is exclusive with the other MEDRE topologies:
# medre-openhop owns the meshtasticd-c-medre node (one API client per
# node), and the openHop repeater's companion accepts exactly one client
# at a time. Running two MEDRE instances against the same rig would
# double-book that node and make the companion's single-client rule
# ambiguous.
if ((WITH_OPENHOP)) && ((WITH_MEDRE || WITH_NORMAL)); then
	echo "ERROR: --openhop is mutually exclusive with --with-medre and --normal" >&2
	exit 2
fi

# The virtual-radio profile is a STANDALONE rig of its own: the lab relay
# holds EXACTLY two client slots (the two repeaters' radios), and each
# repeater's companion holds exactly one client. It therefore cannot share
# a rig with any other profile.
if ((WITH_OPENHOP_RADIO)) && ((WITH_OPENHOP || WITH_MEDRE || WITH_NORMAL)); then
	echo "ERROR: --openhop-radio is mutually exclusive with --openhop, --with-medre and --normal" >&2
	exit 2
fi

EXPECTED_BRANCH=""
MMRELAY_SRC="${MMRELAY_SRC:-${LAB_ROOT}/sources/mmrelay}"
SYNAPSE_CONTAINER="medre-lab-synapse"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="lab/logs"

log() { printf '\n=== %s ===\n' "$*"; }
die() {
	echo "ERROR: $*" >&2
	exit 1
}

# Topology modes are mutually exclusive: BOTH run a medre instance as
# @medre:lab.test, and a simultaneous pair would double-book the bot account
# (two nio sync clients on one device set) and violate the one-api-client-
# per-node rule (the stress relay owns a/b-medre nodes while the normal
# relay owns the c-medre node... and both would fight over room mapping).
if ((WITH_MEDRE)) && ((WITH_NORMAL)); then
	die "--with-medre (stress topology) and --normal (normal-deployment topology) are mutually exclusive"
fi

# --- Environment ------------------------------------------------------------
if [[ ! -f .env ]]; then
	die "Run medre-lab docker prepare before bring-up"
fi
# shellcheck disable=SC1091
source .env
: "${LAB_SOURCE_MANIFEST:?Run medre-lab docker prepare first}"
python3 lab/check_source_mounts.py "${LAB_SOURCE_MANIFEST}" --host-only
export MMRELAY_SRC="${MMRELAY_SRC:-${LAB_ROOT}/sources/mmrelay}"

if [[ ! -d "${MMRELAY_SRC}/src/mmrelay" ]]; then
	die "MMRELAY_SRC=${MMRELAY_SRC} does not look like an mmrelay checkout"
fi

BRANCH="$(git -C "${MMRELAY_SRC}" branch --show-current || true)"
HEAD_SHA="$(git -C "${MMRELAY_SRC}" rev-parse HEAD || true)"
echo "mmrelay checkout: ${MMRELAY_SRC}"
echo "  branch: ${BRANCH}"
echo "  head:   ${HEAD_SHA}"

# --- medre-adapter-matrix override (every path) --------------------------------
# The mmrelay images bake medre-adapter-matrix from the MEDRE_MATRIX_SRC
# checkout (compose additional context medre-matrix-src; see lab/mmrelay/
# Dockerfile) instead of mmrelay's pyproject pin, and the medre profiles
# mount/shadow the SAME checkout. One source, one revision, everywhere the
# adapter appears. The exact HEAD SHA is exported as the MEDRE_MATRIX_REF
# build arg so a detached checkout is honored verbatim; the ref is resolved
# again inside the build (git+file install) and recorded in the baked
# direct_url.json (vcs_info.commit_id) for in-container verification.
MEDRE_MATRIX_SRC="${MEDRE_MATRIX_SRC:-${LAB_ROOT}/sources/medre-adapter-matrix}"
if [[ ! -d "${MEDRE_MATRIX_SRC}/src/medre" ]]; then
	die "MEDRE_MATRIX_SRC=${MEDRE_MATRIX_SRC} does not look like a medre-adapter-matrix checkout"
fi
MEDRE_MATRIX_REF="$(git -C "${MEDRE_MATRIX_SRC}" rev-parse HEAD)"
export MEDRE_MATRIX_SRC MEDRE_MATRIX_REF
echo "medre-adapter-matrix override: ${MEDRE_MATRIX_SRC}"
echo "  ref:    ${MEDRE_MATRIX_REF} (baked into mmrelay images; mounted for medre)"

# --- medre core override (every path) ------------------------------------------
# The selected core source backs the MEDRE image. Rig-level MMRelay bake
# overrides are separate from qualification of MMRelay's declared dependencies.
MEDRE_CORE_SRC="${MEDRE_CORE_SRC:-${LAB_ROOT}/sources/medre}"
if [[ ! -d "${MEDRE_CORE_SRC}/src/medre" ]]; then
	die "MEDRE_CORE_SRC=${MEDRE_CORE_SRC} does not look like a medre core checkout"
fi
MEDRE_CORE_REF="$(git -C "${MEDRE_CORE_SRC}" rev-parse HEAD)"
export MEDRE_CORE_SRC MEDRE_CORE_REF
echo "medre core override: ${MEDRE_CORE_SRC}"
echo "  ref:    ${MEDRE_CORE_REF} (mounted for medre)"

# --- mmrelay-bake core override (every path) ------------------------------------
# The MMRelay images may bake a different core than the MEDRE mount. Defaults
# to the selected MEDRE core; compatibility with MMRelay's legacy imports is
# a property of the actual selected pair, not a guarantee of this override.
# The bake-pin stamp records its exact ref for in-container provenance.
MMRELAY_BAKE_CORE_SRC="${MMRELAY_BAKE_CORE_SRC:-${MEDRE_CORE_SRC}}"
if [[ ! -d "${MMRELAY_BAKE_CORE_SRC}/src/medre" ]]; then
	die "MMRELAY_BAKE_CORE_SRC=${MMRELAY_BAKE_CORE_SRC} does not look like a medre core checkout"
fi
MMRELAY_BAKE_CORE_REF="$(git -C "${MMRELAY_BAKE_CORE_SRC}" rev-parse HEAD)"
export MMRELAY_BAKE_CORE_SRC MMRELAY_BAKE_CORE_REF
echo "mmrelay-bake core override: ${MMRELAY_BAKE_CORE_SRC}"
echo "  ref:    ${MMRELAY_BAKE_CORE_REF} (baked into mmrelay images only)"

COMPOSE=(docker compose)
RELAYS=(a b)

# Derived images are local build artifacts, not registry images. Fail before
# stopping running services or resetting durable state when reuse is impossible.
if ((SKIP_BUILD)); then
	for image in medre-lab/synapse-lab:latest medre-lab/meshtasticd-lab:latest; do
		docker image inspect "${image}" >/dev/null 2>&1 ||
			die "Missing local wrapper ${image}; rerun without --skip-build"
	done
fi

# --- MEDRE bridge profile -----------------------------------------------------
# Adds: dedicated meshtasticd-a-medre (AlphaLab) + meshtasticd-b-medre
# (BetaLab) nodes and one medre-relay runtime (bot @medre:lab.test, matrix
# adapter + two meshtastic adapters). Every extra step below is gated on
# WITH_MEDRE so a without-profile bring-up is byte-identical to the
# 2-relay topology. Candidate revisions (clones under
# ${LAB_ROOT}/sources/ — see sources/README.md,
# lab/README.md and lab/medre/Dockerfile).
MEDRE_NODES=(meshtasticd-a-medre meshtasticd-b-medre)
MEDRE_BOT_MXID="@medre:lab.test"
NORMAL_NODES=(meshtasticd-c-medre meshtasticd-c-peer)
LXMF_PEER_IDENTITY="lab/state/lxmf/peer.identity"
LXMF_PEER_HASH=""
if ((WITH_MEDRE)); then
	COMPOSE=(docker compose --profile medre)
	MEDRE_CORE_SRC="${MEDRE_CORE_SRC:-${LAB_ROOT}/sources/medre}"
	MEDRE_MATRIX_SRC="${MEDRE_MATRIX_SRC:-${LAB_ROOT}/sources/medre-adapter-matrix}"
	MEDRE_MESHTASTIC_SRC="${MEDRE_MESHTASTIC_SRC:-${LAB_ROOT}/sources/medre-adapter-meshtastic}"
	MEDRE_LXMF_SRC="${MEDRE_LXMF_SRC:-${LAB_ROOT}/sources/medre-adapter-lxmf}"
	for src in "${MEDRE_CORE_SRC}" "${MEDRE_MATRIX_SRC}" "${MEDRE_MESHTASTIC_SRC}"; do
		if [[ ! -d ${src}/src/medre ]]; then
			die "MEDRE source ${src} does not look like a medre checkout/worktree"
		fi
	done
	echo "medre selected source checkouts:"
	echo "  core:       ${MEDRE_CORE_SRC} ($(git -C "${MEDRE_CORE_SRC}" rev-parse --short HEAD))"
	echo "  matrix:     ${MEDRE_MATRIX_SRC} ($(git -C "${MEDRE_MATRIX_SRC}" rev-parse --short HEAD))"
	echo "  meshtastic: ${MEDRE_MESHTASTIC_SRC} ($(git -C "${MEDRE_MESHTASTIC_SRC}" rev-parse --short HEAD))"
	echo "  lxmf:       ${MEDRE_LXMF_SRC} ($(git -C "${MEDRE_LXMF_SRC}" rev-parse --short HEAD)) (image deps only; stress relay never mounts it)"
fi

# --- NORMAL-deployment profile -----------------------------------------------
# The realistic topology: medre-normal gets its OWN meshnet (MeshC/GammaLab
# on docker bridge mesh-c) instead of sharing Alpha/Beta with the mmrelay
# relays, plus a loopback-only LXMF/Reticulum leg inside the container. The
# shared surface is #lab-bridge (ch0) / #lab-bridge-ch2 (ch2); mmrelay-a/b
# map ONLY those two rooms (their four per-meshnet rooms are mapped nowhere).
# Every medre instance in the rig is @medre:lab.test, so this mode ALSO stops
# the stress-profile medre containers (and the stress mode stops this one) —
# a simultaneous stress+normal medre would double-book the bot account and
# the one-api-client-per-node rule.
if ((WITH_NORMAL)); then
	COMPOSE=(docker compose --profile normal)
	MEDRE_CORE_SRC="${MEDRE_CORE_SRC:-${LAB_ROOT}/sources/medre}"
	MEDRE_MATRIX_SRC="${MEDRE_MATRIX_SRC:-${LAB_ROOT}/sources/medre-adapter-matrix}"
	MEDRE_MESHTASTIC_SRC="${MEDRE_MESHTASTIC_SRC:-${LAB_ROOT}/sources/medre-adapter-meshtastic}"
	MEDRE_LXMF_SRC="${MEDRE_LXMF_SRC:-${LAB_ROOT}/sources/medre-adapter-lxmf}"
	for src in "${MEDRE_CORE_SRC}" "${MEDRE_MATRIX_SRC}" "${MEDRE_MESHTASTIC_SRC}"; do
		if [[ ! -d ${src}/src/medre ]]; then
			die "MEDRE source ${src} does not look like a medre checkout/worktree"
		fi
	done
	# LXMF adapter worktree: consumed READ-ONLY (mounted at /src/lxmf for the
	# runtime shadowing; never modified by the lab).
	if [[ ! -d "${MEDRE_LXMF_SRC}/src/medre/adapters/lxmf" ]]; then
		die "MEDRE_LXMF_SRC=${MEDRE_LXMF_SRC} does not look like the lxmf adapter worktree"
	fi
	echo "medre selected source checkouts (normal profile):"
	echo "  core:       ${MEDRE_CORE_SRC} ($(git -C "${MEDRE_CORE_SRC}" rev-parse --short HEAD))"
	echo "  matrix:     ${MEDRE_MATRIX_SRC} ($(git -C "${MEDRE_MATRIX_SRC}" rev-parse --short HEAD))"
	echo "  meshtastic: ${MEDRE_MESHTASTIC_SRC} ($(git -C "${MEDRE_MESHTASTIC_SRC}" rev-parse --short HEAD))"
	echo "  lxmf:       ${MEDRE_LXMF_SRC} ($(git -C "${MEDRE_LXMF_SRC}" rev-parse --short HEAD)) READ-ONLY"
fi

# --- Single medre-instance enforcement ----------------------------------------
# EVERY up.sh-orchestrated path (plain, --with-medre, --normal) stops BOTH
# modes' medre containers before anything else: both modes run @medre:lab.test
# (one bot account, one api client per node). Plain bring-ups enforce this too
# - a mode switch through the plain path must not leave the other mode's
# medre runtime fighting the relays for room mapping. Compose stop is a no-op
# on absent containers, so this is idempotent. (Raw `docker compose` with both
# profiles active is NOT guarded here - this enforcement is an up.sh
# orchestration guarantee, not a compose-level one.)
log "Ensuring both medre modes' containers are DOWN (single-instance rule)"
docker compose --profile medre stop medre-relay >/dev/null 2>&1 || true
docker compose --profile medre stop meshtasticd-a-medre meshtasticd-b-medre >/dev/null 2>&1 || true
docker compose --profile normal stop medre-normal >/dev/null 2>&1 || true
docker compose --profile normal stop meshtasticd-c-medre meshtasticd-c-peer >/dev/null 2>&1 || true

mkdir -p lab/state/config "${LOG_DIR}/scenarios"

if ((DO_RESET)); then
	log "Reset requested: wiping volumes and lab state"
	bash lab/reset.sh
fi

# --- openHop profile (OPT-IN, self-contained) --------------------------------
# The openHop profile is a STANDALONE rig: it needs neither Synapse nor the
# mmrelay relays nor any Matrix room. It is the hardware-free
# MEDRE<->openHop Docker qualifier:
#   openhop-repeater  the openHop repeater (radio_type: null) serving the
#                     MeshCore companion over TCP 5000 on the private
#                     `openhop` compose network (never published);
#   medre-openhop     the MEDRE runtime whose MeshCore adapter talks to
#                     that companion, plus the existing MeshC sim node.
#
# Everything below is gated on WITH_OPENHOP, so a default bring-up stays
# byte-identical to today's 2-relay topology.
if ((WITH_OPENHOP)); then
	COMPOSE=(docker compose --profile openhop)

	# --- Pinned source checkouts ------------------------------------------
	# The openHop profile carries its OWN pins (see sources.lock.json):
	# the default rig's medre 0.1.0 pin cannot satisfy the meshcore
	# adapter's `medre>=0.2.0,<0.3.0` requirement.
	OPENHOP_CORE_SRC="${OPENHOP_CORE_SRC:-${LAB_ROOT}/sources/openhop-core}"
	OPENHOP_REPEATER_SRC="${OPENHOP_REPEATER_SRC:-${LAB_ROOT}/sources/openhop-repeater}"
	MEDRE_OPENHOP_SRC="${MEDRE_OPENHOP_SRC:-${LAB_ROOT}/sources/medre-openhop}"
	MEDRE_MESHCORE_SRC="${MEDRE_MESHCORE_SRC:-${LAB_ROOT}/sources/medre-adapter-meshcore}"
	MEDRE_MESHTASTIC_OPENHOP_SRC="${MEDRE_MESHTASTIC_OPENHOP_SRC:-${LAB_ROOT}/sources/medre-adapter-meshtastic-openhop}"
	for src in \
		"${OPENHOP_CORE_SRC}:openhop_core" \
		"${OPENHOP_REPEATER_SRC}:openhop_repeater" \
		"${MEDRE_OPENHOP_SRC}:medre" \
		"${MEDRE_MESHCORE_SRC}:medre-adapter-meshcore" \
		"${MEDRE_MESHTASTIC_OPENHOP_SRC}:medre-adapter-meshtastic"; do
		dir="${src%%:*}"
		label="${src##*:}"
		[[ -d ${dir} ]] ||
			die "openHop source ${dir} (${label}) is missing; run 'medre-lab sources fetch'"
	done
	export OPENHOP_CORE_SRC OPENHOP_REPEATER_SRC MEDRE_OPENHOP_SRC \
		MEDRE_MESHCORE_SRC MEDRE_MESHTASTIC_OPENHOP_SRC
	# The exact HEAD SHAs, so the images bake from the pinned commits.
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
	echo "openHop selected source checkouts:"
	echo "  openhop_core:         ${OPENHOP_CORE_SRC} (${OPENHOP_CORE_REF})"
	echo "  openhop_repeater:     ${OPENHOP_REPEATER_SRC} (${OPENHOP_REPEATER_REF})"
	echo "  medre (openhop):      ${MEDRE_OPENHOP_SRC} (${MEDRE_OPENHOP_REF})"
	echo "  adapter_meshcore:     ${MEDRE_MESHCORE_SRC} (${MEDRE_MESHCORE_REF})"
	echo "  adapter_meshtastic:   ${MEDRE_MESHTASTIC_OPENHOP_SRC} (${MEDRE_MESHTASTIC_OPENHOP_REF})"

	# --- Source provenance guard -------------------------------------------
	# The openHop manifest names the openHop containers only; it is checked
	# with live container inspection once the profile is up.
	if [[ -n ${LAB_SOURCE_MANIFEST_OPENHOP:-} ]]; then
		python3 lab/check_source_mounts.py "${LAB_SOURCE_MANIFEST_OPENHOP}" --host-only ||
			die "openHop source provenance check failed"
	fi

	# --- Build -------------------------------------------------------------
	if ((! SKIP_BUILD)); then
		log "Building images (openhop repeater + medre openhop)"
		"${COMPOSE[@]}" build openhop-repeater medre-openhop
	else
		log "Skipping image build"
	fi

	# --- Generated openHop configs (identities + credentials) -------------
	# Identities and credentials are generated into the GITIGNORED
	# lab/state/ tree and substituted into the committed contract configs.
	# They never enter git, and they persist across bring-ups so the
	# companion keeps the SAME identity across restarts (persistence
	# evidence).
	log "Generating openHop configs into lab/state/config/"
	mkdir -p lab/state/openhop
	OPENHOP_SECRETS="lab/state/openhop/secrets.env"
	if [[ ! -f ${OPENHOP_SECRETS} ]]; then
		python3 - "${OPENHOP_SECRETS}" <<'PYSECRETS'
import os
import secrets
import sys

path = sys.argv[1]
if os.path.exists(path):
    raise SystemExit(f"refusing to overwrite {path}")
values = {
    "OPENHOP_COMPANION_IDENTITY_KEY": secrets.token_bytes(32).hex(),
    "OPENHOP_ADMIN_PASSWORD": secrets.token_urlsafe(24),
    "OPENHOP_GUEST_PASSWORD": secrets.token_urlsafe(24),
    "OPENHOP_JWT_SECRET": secrets.token_hex(32),
}
with open(path, "w", encoding="utf-8") as handle:
    for key, value in values.items():
        handle.write(f"{key}={value}\n")
os.chmod(path, 0o600)
print(f"  generated {path} (mode 0600, gitignored)")
PYSECRETS
	else
		echo "  ${OPENHOP_SECRETS} present, reusing it (identity persists across restarts)"
	fi
	# shellcheck disable=SC1090
	source "${OPENHOP_SECRETS}"
	export OPENHOP_COMPANION_IDENTITY_KEY OPENHOP_ADMIN_PASSWORD \
		OPENHOP_GUEST_PASSWORD OPENHOP_JWT_SECRET
	python3 - <<'PYGEN'
import os
import sys

pairs = (
    ("__OPENHOP_COMPANION_IDENTITY_KEY__", "OPENHOP_COMPANION_IDENTITY_KEY"),
    ("__OPENHOP_ADMIN_PASSWORD__", "OPENHOP_ADMIN_PASSWORD"),
    ("__OPENHOP_GUEST_PASSWORD__", "OPENHOP_GUEST_PASSWORD"),
    ("__OPENHOP_JWT_SECRET__", "OPENHOP_JWT_SECRET"),
)
for template, target, placeholders in (
    (
        "lab/config/openhop-repeater.yaml",
        "lab/state/config/openhop-repeater.yaml",
        pairs,
    ),
    (
        "lab/config/medre-openhop.yaml",
        "lab/state/config/medre-openhop.yaml",
        (),
    ),
):
    with open(template, encoding="utf-8") as handle:
        text = handle.read()
    for placeholder, env_key in placeholders:
        text = text.replace(placeholder, os.environ[env_key])
    for placeholder, _ in placeholders:
        if placeholder in text:
            raise SystemExit(f"unsubstituted placeholder remains in {template}: {placeholder}")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"  {target} generated")
PYGEN
	chmod 0600 lab/state/config/openhop-repeater.yaml
	# The repeater container reads this as uid 1000; grant it a POSIX ACL
	# entry when the invoking user has another uid (refuses otherwise).
	python3 lab/tools/openhop_config_access.py grant \
		lab/state/config/openhop-repeater.yaml

	# --- Bring-up ----------------------------------------------------------
	log "Starting the MeshC sim node (meshtasticd-c-medre)"
	"${COMPOSE[@]}" up -d meshtasticd-c-medre
	log "Starting the openHop repeater (radio_type: null)"
	"${COMPOSE[@]}" up -d openhop-repeater
	log "Starting medre-openhop (meshcore adapter -> openhop-repeater:5000)"
	"${COMPOSE[@]}" up -d medre-openhop

	openhop_wait_healthy() {
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
	openhop_wait_healthy medre-lab-mesh-c-medre 240
	openhop_wait_healthy medre-lab-openhop-repeater 240
	openhop_wait_healthy medre-lab-medre-openhop 240

	# --- Live source provenance -------------------------------------------
	# Now that the containers are up, the openHop manifest is checked with
	# LIVE container inspection: each pinned source must be mounted
	# read-only at its target and its live import must resolve there.
	if [[ -n ${LAB_SOURCE_MANIFEST_OPENHOP:-} ]]; then
		log "Verifying the live openHop source mounts"
		python3 lab/check_source_mounts.py "${LAB_SOURCE_MANIFEST_OPENHOP}" ||
			die "openHop source provenance check failed"
	fi

	log "Capturing openHop logs"
	docker logs medre-lab-openhop-repeater >"${LOG_DIR}/openhop-repeater-${TS}.log" 2>&1 || true
	docker logs medre-lab-medre-openhop >"${LOG_DIR}/medre-openhop-${TS}.log" 2>&1 || true

	# The openHop family's pins live in the shared "openhop_sources"
	# section; this section describes the Phase 2 rig. Both REPLACE any
	# earlier section of the same name (lab/version_record.py), so the
	# record never carries two openHop sections with different pins.
	python3 lab/openhop_pins.py "${LAB_SOURCE_MANIFEST_OPENHOP}" \
		--record lab/VERSION >/dev/null ||
		die "could not record the openHop pins"
	python3 lab/version_record.py set openhop_profile <<EOF
  source_pins: see the openhop_sources section (the SAME checkouts this rig builds and runs from)
  image_openhop: medre-lab/openhop-repeater:latest (lab/openhop/Dockerfile; local checkouts installed in order: openhop_core, then openhop_repeater)
  image_medre: medre-lab/medre-openhop-dev:latest (lab/medre-openhop/Dockerfile; medre 0.2.3 + meshcore 0.2.0 + meshtastic 0.2.1)
  topology: openhop-repeater (radio_type null, companion TCP 5000 on the private medre-lab-openhop network, never published) + medre-openhop
  radio: NONE (hardware-free qualifier; NullRadio raises on send, so no delivery is possible)
EOF

	log "openHop profile is UP"
	cat <<EOF
  openhop-repeater: medre-lab-openhop-repeater (companion TCP 5000, private network only)
  medre-openhop:     medre-lab-medre-openhop (meshcore -> openhop-repeater:5000, meshtastic -> meshtasticd-c-medre)
  MeshC sim node:    medre-lab-mesh-c-medre
  Logs:              ${LOG_DIR}/openhop-repeater-${TS}.log, ${LOG_DIR}/medre-openhop-${TS}.log
  Lifecycle check:   lab/openhop/lifecycle_check.sh
  Teardown:          lab/down.sh
EOF
	exit 0
fi

# --- openHop VIRTUAL RADIO profile (OPT-IN, self-contained) -----------------
# Two openHop repeaters wired to the LAB's virtual radio relay instead of to
# hardware. The RF MEDIUM is simulated; the MeshCore packets that cross it
# are real (encrypted by one repeater, decrypted by the other).
#
# This rig is standalone: the relay holds exactly two client slots (the two
# repeaters' radios) and each companion holds exactly one client, so it can
# never share a rig with another profile.
if ((WITH_OPENHOP_RADIO)); then
	COMPOSE=(docker compose --profile openhop-radio)

	# --- Pinned source checkouts ------------------------------------------
	OPENHOP_CORE_SRC="${OPENHOP_CORE_SRC:-${LAB_ROOT}/sources/openhop-core}"
	OPENHOP_REPEATER_SRC="${OPENHOP_REPEATER_SRC:-${LAB_ROOT}/sources/openhop-repeater}"
	MEDRE_MESHCORE_SRC="${MEDRE_MESHCORE_SRC:-${LAB_ROOT}/sources/medre-adapter-meshcore}"
	for src in \
		"${OPENHOP_CORE_SRC}:openhop_core" \
		"${OPENHOP_REPEATER_SRC}:openhop_repeater" \
		"${MEDRE_MESHCORE_SRC}:medre-adapter-meshcore"; do
		dir="${src%%:*}"
		label="${src##*:}"
		[[ -d ${dir} ]] ||
			die "openHop radio source ${dir} (${label}) is missing; run 'medre-lab sources fetch'"
	done
	export OPENHOP_CORE_SRC OPENHOP_REPEATER_SRC MEDRE_MESHCORE_SRC
	OPENHOP_CORE_REF_HEAD="$(git -C "${OPENHOP_CORE_SRC}" rev-parse HEAD)"
	export OPENHOP_CORE_REF="${OPENHOP_CORE_REF_HEAD}"
	OPENHOP_REPEATER_REF_HEAD="$(git -C "${OPENHOP_REPEATER_SRC}" rev-parse HEAD)"
	export OPENHOP_REPEATER_REF="${OPENHOP_REPEATER_REF_HEAD}"
	echo "openHop virtual radio source checkouts:"
	echo "  openhop_core:         ${OPENHOP_CORE_SRC} @ ${OPENHOP_CORE_REF}"
	echo "  openhop_repeater:     ${OPENHOP_REPEATER_SRC} @ ${OPENHOP_REPEATER_REF}"

	# --- Build -------------------------------------------------------------
	if ((! SKIP_BUILD)); then
		log "Building images (virtual radio relay + openhop repeater + medre openhop)"
		"${COMPOSE[@]}" build openhop-radio-relay openhop-repeater-a openhop-repeater-b openhop-radio-probe
	else
		log "Skipping image build"
	fi

	# --- Generated repeater configs (identities + credentials) --------------
	# Each repeater gets its OWN companion identity and credentials: the two
	# nodes must never share an identity. Everything is generated into the
	# GITIGNORED lab/state/ tree and substituted into the committed template.
	log "Generating openHop radio configs into lab/state/config/"
	mkdir -p lab/state/openhop-radio
	RADIO_SECRETS="lab/state/openhop-radio/secrets.env"
	if [[ ! -f ${RADIO_SECRETS} ]]; then
		python3 - "${RADIO_SECRETS}" <<'PYSECRETS'
import os
import secrets
import sys

path = sys.argv[1]
if os.path.exists(path):
    raise SystemExit(f"refusing to overwrite {path}")
values = {}
for instance in ("a", "b"):
    values[f"RADIO_{instance.upper()}_COMPANION_IDENTITY_KEY"] = secrets.token_bytes(32).hex()
    values[f"RADIO_{instance.upper()}_ADMIN_PASSWORD"] = secrets.token_urlsafe(24)
    values[f"RADIO_{instance.upper()}_GUEST_PASSWORD"] = secrets.token_urlsafe(24)
    values[f"RADIO_{instance.upper()}_JWT_SECRET"] = secrets.token_hex(32)
with open(path, "w", encoding="utf-8") as handle:
    for key, value in values.items():
        handle.write(f"{key}={value}\n")
os.chmod(path, 0o600)
print(f"  generated {path} (mode 0600, gitignored)")
PYSECRETS
	else
		echo "  ${RADIO_SECRETS} present, reusing it (identities persist across restarts)"
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
	# The repeater containers read these as uid 1000; grant them a POSIX
	# ACL entry when the invoking user has another uid (refuses otherwise).
	python3 lab/tools/openhop_config_access.py grant \
		lab/state/config/openhop-radio-repeater-a.yaml
	python3 lab/tools/openhop_config_access.py grant \
		lab/state/config/openhop-radio-repeater-b.yaml

	# --- Bring-up ----------------------------------------------------------
	log "Starting the virtual radio relay"
	"${COMPOSE[@]}" up -d openhop-radio-relay
	log "Starting the two openHop repeaters (radio_type: modem_tcp -> the relay)"
	"${COMPOSE[@]}" up -d openhop-repeater-a openhop-repeater-b

	openhop_radio_wait_healthy() {
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
	openhop_radio_wait_healthy medre-lab-openhop-radio-relay 120
	openhop_radio_wait_healthy medre-lab-openhop-repeater-a 240
	openhop_radio_wait_healthy medre-lab-openhop-repeater-b 240

	log "Capturing openHop radio logs"
	docker logs medre-lab-openhop-radio-relay >"${LOG_DIR}/openhop-radio-relay-${TS}.log" 2>&1 || true
	docker logs medre-lab-openhop-repeater-a >"${LOG_DIR}/openhop-radio-repeater-a-${TS}.log" 2>&1 || true
	docker logs medre-lab-openhop-repeater-b >"${LOG_DIR}/openhop-radio-repeater-b-${TS}.log" 2>&1 || true

	python3 lab/openhop_pins.py "${LAB_SOURCE_MANIFEST_OPENHOP}" \
		--record lab/VERSION >/dev/null ||
		die "could not record the openHop pins"
	python3 lab/version_record.py set openhop_radio_profile <<EOF
  source_pins: see the openhop_sources section (the SAME checkouts this rig builds and runs from)
  image_relay: medre-lab/openhop-radio-relay:latest (lab/openhop-radio/Dockerfile; lab-owned asyncio service speaking the openHop Modem protocol)
  topology: openhop-radio-relay + openhop-repeater-a + openhop-repeater-b on the private medre-lab-openhop-radio network (no host ports)
  radio: VIRTUAL (the lab relay simulates the RF medium; the MeshCore packets crossing it are real)
EOF

	log "openHop virtual radio profile is UP"
	cat <<EOF
  relay:        medre-lab-openhop-radio-relay (openHop Modem protocol, TCP 5055, private network only)
  repeater A:   medre-lab-openhop-repeater-a (companion TCP 5000, private network only)
  repeater B:   medre-lab-openhop-repeater-b (companion TCP 5000, private network only)
  Logs:         ${LOG_DIR}/openhop-radio-*-${TS}.log
  Packet proof: lab/openhop-radio/packet_proof.sh
  Teardown:     lab/down.sh
EOF
	exit 0
fi

# --- Build -------------------------------------------------------------------
if ((! SKIP_BUILD)); then
	log "Building images (mmrelay dev + lab-tools + unprivileged synapse/meshtasticd wrappers)"
	"${COMPOSE[@]}" build mmrelay-a mmrelay-b lab-tools synapse meshtasticd-a
	if ((WITH_MEDRE)); then
		log "Building image (medre dev)"
		"${COMPOSE[@]}" build medre-relay
	fi
	if ((WITH_NORMAL)); then
		log "Building image (medre dev, shared by medre-normal)"
		"${COMPOSE[@]}" build medre-normal
	fi
else
	log "Skipping image build"
fi

# --- Generated relay configs -------------------------------------------------
# Contract configs in lab/config/ are copied verbatim into lab/state/config/
# (the bind the relay containers mount). No patching: the committed configs
# are the canonical 2-relay topology. The ONLY patches are profile-gated and
# land in the generated copies only (committed configs stay byte-identical):
#   - trust injection: @medre:lab.test added to trusted_relays (both medre
#     modes; the relays must treat the medre bot's canonical envelopes as
#     trusted peer relay claims, exactly like the other relay bot's).
#   - normal-profile room mapping: under --normal each relay maps ONLY the
#     two shared bridge rooms (ch0/ch2); under the stress/default profiles
#     the four per-meshnet rooms are kept.
log "Generating relay configs into lab/state/config/"
for relay in "${RELAYS[@]}"; do
	cp "lab/config/mmrelay-${relay}.yaml" "lab/state/config/mmrelay-${relay}.yaml"
done

# Normal-profile room mapping (generated copies only): replace the four-room
# matrix_rooms block with the single shared surface.
if ((WITH_NORMAL)); then
	log "Restricting mmrelay room mapping to the shared bridge rooms (normal profile)"
	python3 - <<'PYROOMS'
import re

BRIDGE_BLOCK = (
    "matrix_rooms:\n"
    '  - id: "#lab-bridge:lab.test"\n'
    "    meshtastic_channel: 0\n"
    '  - id: "#lab-bridge-ch2:lab.test"\n'
    "    meshtastic_channel: 2\n"
)
pattern = re.compile(
    r"matrix_rooms:\n(?:  - id:.*\n    meshtastic_channel: \d+\n)+"
)
for relay in ("a", "b"):
    path = f"lab/state/config/mmrelay-{relay}.yaml"
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    new_text, count = pattern.subn(BRIDGE_BLOCK, text)
    if count != 1:
        raise SystemExit(
            f"mmrelay-{relay}: expected exactly one matrix_rooms block, "
            f"matched {count} in {path}"
        )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(new_text)
    print(f"  mmrelay-{relay}: matrix_rooms -> #lab-bridge (ch0) + #lab-bridge-ch2 (ch2)")
PYROOMS
fi

# Trust wiring (both medre modes): the relays must treat the medre bot's
# canonical envelopes as trusted peer relay claims, exactly like the other
# relay bot's. Committed mmrelay configs stay byte-identical; the injected
# MXID lands only in the generated copies the containers mount.
if ((WITH_MEDRE)) || ((WITH_NORMAL)); then
	log "Injecting ${MEDRE_BOT_MXID} into mmrelay trusted_relays (generated configs)"
	for relay in "${RELAYS[@]}"; do
		file="lab/state/config/mmrelay-${relay}.yaml"
		other="@relay-b:lab.test"
		[[ ${relay} == "b" ]] && other="@relay-a:lab.test"
		if grep -q "${MEDRE_BOT_MXID}" "${file}"; then
			echo "  mmrelay-${relay}: ${MEDRE_BOT_MXID} already trusted"
		elif grep -q "trusted_relays: \[\"${other}\"\]" "${file}"; then
			sed -i "s|trusted_relays: \[\"${other}\"\]|trusted_relays: [\"${other}\", \"${MEDRE_BOT_MXID}\"]|" "${file}"
			echo "  mmrelay-${relay}: trusted_relays=[${other}, ${MEDRE_BOT_MXID}]"
		else
			die "mmrelay-${relay}: expected trusted_relays [\"${other}\"] not found in ${file}"
		fi
	done
fi

wait_healthy() {
	local name=$1
	local timeout=${2:-240}
	local deadline=$((SECONDS + timeout))
	while true; do
		local status
		status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' "${name}" 2>/dev/null || echo absent)"
		case "${status}" in
		healthy)
			echo "  ${name} healthy"
			return 0
			;;
		absent) die "${name} container is absent (crashed?)" ;;
		esac
		if ((SECONDS >= deadline)); then
			die "${name} not healthy after ${timeout}s (status=${status})"
		fi
		sleep 3
	done
}

# require_config_fresh CONTAINER CONFIG
#   Generation-consistency gate: the running container's StartedAt must
#   POSTDATE its generated config's mtime. Closes the aborted-bring-up
#   mixed-generation window (config regenerated, bridge never restarted)
#   that ran a fanout pass against relays still mapping the OLD rooms
#   (triage 2026-11-11). StartedAt parsing per the scenario suite's
#   generation-bounded queue snapshots (RFC3339 truncated to whole UTC
#   seconds vs the config mtime's epoch seconds; up.sh writes configs well
#   before starting the containers, so second granularity is unambiguous).
require_config_fresh() {
	local container=$1 config=$2
	local started config_epoch started_epoch
	started="$(docker inspect --format '{{.State.StartedAt}}' "${container}" 2>/dev/null | cut -c1-19 | tr 'T' ' ')"
	[[ -n ${started} ]] || die "generation gate: cannot read StartedAt of ${container} (is it running?)"
	[[ -f ${config} ]] || die "generation gate: generated config missing: ${config}"
	config_epoch="$(stat -c %Y "${config}")"
	started_epoch="$(date -u -d "${started}" +%s)"
	if ((started_epoch > config_epoch)); then
		echo "  ${container}: started $(date -u -d "${started}" '+%H:%M:%SZ') postdates ${config} (generation consistent)"
	else
		die "${container} (started ${started}Z) does NOT postdate its generated config ${config} (mtime $(date -u -d "@${config_epoch}" '+%Y-%m-%dT%H:%M:%SZ')) - mixed generation: the process may run stale room mapping against regenerated state; rerun lab/up.sh to regenerate and restart"
	fi
}

# --- Synapse -------------------------------------------------------------------
log "Synapse: generating config if needed"
if ! "${COMPOSE[@]}" run --rm --entrypoint sh synapse -c 'test -f /data/homeserver.yaml' >/dev/null 2>&1; then
	"${COMPOSE[@]}" run --rm synapse generate
	echo "  generated homeserver.yaml"
fi
log "Synapse: applying lab overrides (idempotent, version-aware)"
OVERRIDES_OUT="$("${COMPOSE[@]}" run --rm --entrypoint sh \
	--volume "${PWD}/lab/config/synapse-overrides.yaml:/overrides.yaml:ro" \
	--volume "${PWD}/lab/tools/apply-synapse-overrides.sh:/apply-overrides.sh:ro" \
	synapse /apply-overrides.sh /data/homeserver.yaml /overrides.yaml)"
echo "  ${OVERRIDES_OUT:-overrides unchanged}"
if grep -q "reapplied" <<<"${OVERRIDES_OUT}"; then
	log "Synapse: recreating container to load new overrides"
	"${COMPOSE[@]}" up -d --force-recreate synapse
fi

log "Starting synapse"
"${COMPOSE[@]}" up -d synapse
wait_healthy "${SYNAPSE_CONTAINER}" 240

# --- Provisioning ---------------------------------------------------------------
log "Starting lab-tools (long-running utility container)"
"${COMPOSE[@]}" up -d lab-tools

log "Provisioning users and rooms"
if ((WITH_NORMAL)); then
	# Registers @medre:lab.test and creates/joins the two shared bridge
	# rooms (both gated inside provision.py; without the flags nothing
	# changes vs the contract provisioning).
	"${COMPOSE[@]}" run --rm -e LAB_MEDRE_PROFILE=1 -e LAB_NORMAL_PROFILE=1 lab-tools python /opt/lab-tools/provision.py
elif ((WITH_MEDRE)); then
	# Registers @medre:lab.test and joins it to the four bridged rooms
	# (gated inside provision.py; without the flag nothing changes).
	"${COMPOSE[@]}" run --rm -e LAB_MEDRE_PROFILE=1 lab-tools python /opt/lab-tools/provision.py
else
	"${COMPOSE[@]}" run --rm lab-tools python /opt/lab-tools/provision.py
fi

# --- Relay auth (cross-signing chain publication) ---------------------------------
log "Relay bot auth (mmrelay auth login; skipped when credentials exist)"
for relay in "${RELAYS[@]}"; do
	if "${COMPOSE[@]}" run --rm mmrelay-"${relay}" \
		sh -c 'test -f /data/matrix/credentials.json' >/dev/null 2>&1; then
		echo "  mmrelay-${relay}: credentials present, skipping auth login"
	else
		"${COMPOSE[@]}" run --rm mmrelay-"${relay}" \
			python -m mmrelay.cli --config "/config/mmrelay-${relay}.yaml" --home /data \
			auth login \
			--homeserver "http://synapse:8008" \
			--username "@relay-${relay}:lab.test" \
			--password "lab-relay-${relay}-pass"
		echo "  mmrelay-${relay}: auth login complete"
	fi
done

log "Verifying bot self-cross-signing chains"
"${COMPOSE[@]}" run --rm lab-tools \
	python /opt/lab-tools/crosssign_check.py "${#RELAYS[@]}"

# --- Generated medre config (profile only) -------------------------------------
# lab/config/medre-relay.yaml is the committed contract template. The
# generated copy (the bind the medre-relay container mounts) substitutes the
# bot access token and provisioned room IDs from lab/state/runtime.json.
if ((WITH_MEDRE)); then
	log "Generating medre relay config into lab/state/config/medre-relay.yaml"
	MEDRE_BOT_TOKEN="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["users"]["medre"]["access_token"])')"
	R_ALPHA_CH0="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["rooms"]["lab-alpha-ch0"]["room_id"])')"
	R_ALPHA_CH2="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["rooms"]["lab-alpha-ch2"]["room_id"])')"
	R_BETA_CH0="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["rooms"]["lab-beta-ch0"]["room_id"])')"
	R_BETA_CH2="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["rooms"]["lab-beta-ch2"]["room_id"])')"
	python3 - "${MEDRE_BOT_TOKEN}" "${R_ALPHA_CH0}" "${R_ALPHA_CH2}" "${R_BETA_CH0}" "${R_BETA_CH2}" <<'PYGEN'
import sys

token, r_a0, r_a2, r_b0, r_b2 = sys.argv[1:6]
with open("lab/config/medre-relay.yaml", encoding="utf-8") as handle:
    text = handle.read()
for placeholder, value in (
    ("__MEDRE_ACCESS_TOKEN__", token),
    ("__ROOM_ALPHA_CH0__", r_a0),
    ("__ROOM_ALPHA_CH2__", r_a2),
    ("__ROOM_BETA_CH0__", r_b0),
    ("__ROOM_BETA_CH2__", r_b2),
):
    text = text.replace(placeholder, value)
for placeholder in (
    "__MEDRE_ACCESS_TOKEN__",
    "__ROOM_ALPHA_CH0__",
    "__ROOM_ALPHA_CH2__",
    "__ROOM_BETA_CH0__",
    "__ROOM_BETA_CH2__",
):
    if placeholder in text:
        raise SystemExit(f"unsubstituted placeholder remains in medre config: {placeholder}")
with open("lab/state/config/medre-relay.yaml", "w", encoding="utf-8") as handle:
    handle.write(text)
print("  medre-relay.yaml generated (token + room IDs substituted)")
PYGEN
fi

# --- LXMF counterpart bootstrap (normal profile) --------------------------------
# Seeds the host-side LXMF material under lab/state/lxmf/ (gitignored):
#   peer.identity                 persistent counterpart identity (created
#                                 once; its delivery-destination hash is what
#                                 the medre route config addresses)
#   reticulum/config              medre-normal's loopback RNS config
#   reticulum-peer/config         the counterpart's loopback RNS config
# The bootstrap runs INSIDE the medre-normal image (it bakes the pinned RNS)
# with --no-deps so no rig service is started as a side effect.
if ((WITH_NORMAL)); then
	log "Seeding LXMF loopback material into lab/state/lxmf/"
	mkdir -p lab/state/lxmf/reticulum lab/state/lxmf/reticulum-peer
	cp lab/config/reticulum/medre.config lab/state/lxmf/reticulum/config
	cp lab/config/reticulum/peer.config lab/state/lxmf/reticulum-peer/config
	# Seed medre's embedded Reticulum config into the state volume (idempotent;
	# only the config file is replaced, the RNS destination cache survives).
	# Must happen before medre-normal starts: an empty config dir makes RNS
	# write its DEFAULT config, silently dropping the loopback interface.
	"${COMPOSE[@]}" run --rm --no-deps medre-normal \
		sh -c 'mkdir -p /data/reticulum && cp /config-reticulum/medre.config /data/reticulum/config'
	echo "  /data/reticulum/config seeded (loopback UDPInterface 4242<->4243)"
	BOOTSTRAP_OUT="$("${COMPOSE[@]}" run --rm --no-deps medre-normal \
		python /opt/lab-tools/lxmf_peer_bootstrap.py /data-lxmf/peer.identity)"
	echo "${BOOTSTRAP_OUT}" | grep -q "LXMF_PEER_BOOTSTRAP_RESULT=" ||
		die "lxmf peer bootstrap failed: ${BOOTSTRAP_OUT}"
	LXMF_PEER_HASH="$(echo "${BOOTSTRAP_OUT}" | sed -n 's/.*LXMF_PEER_BOOTSTRAP_RESULT=//p' | python3 -c 'import json,sys;print(json.load(sys.stdin)["destination_hash"])')"
	if [[ ${#LXMF_PEER_HASH} -ne 32 ]]; then
		die "lxmf peer destination hash malformed: '${LXMF_PEER_HASH}'"
	fi
	echo "  lxmf peer identity ready; delivery destination hash: ${LXMF_PEER_HASH}"
fi

# --- Generated medre-normal config (normal profile only) ------------------------
# lab/config/medre-normal.yaml is the committed contract template. The
# generated copy substitutes the bot access token, the two shared bridge room
# IDs, and the LXMF counterpart destination hash.
if ((WITH_NORMAL)); then
	log "Generating medre-normal config into lab/state/config/medre-normal.yaml"
	MEDRE_BOT_TOKEN="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["users"]["medre"]["access_token"])')"
	R_BRIDGE="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["rooms"]["lab-bridge"]["room_id"])')"
	R_BRIDGE_CH2="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["rooms"]["lab-bridge-ch2"]["room_id"])')"
	python3 - "${MEDRE_BOT_TOKEN}" "${R_BRIDGE}" "${R_BRIDGE_CH2}" "${LXMF_PEER_HASH}" <<'PYGEN'
import sys

token, r_bridge, r_bridge_ch2, peer_hash = sys.argv[1:5]
with open("lab/config/medre-normal.yaml", encoding="utf-8") as handle:
    text = handle.read()
for placeholder, value in (
    ("__MEDRE_ACCESS_TOKEN__", token),
    ("__ROOM_BRIDGE__", r_bridge),
    ("__ROOM_BRIDGE_CH2__", r_bridge_ch2),
    ("__LXMF_PEER_DEST__", peer_hash),
):
    text = text.replace(placeholder, value)
for placeholder in (
    "__MEDRE_ACCESS_TOKEN__",
    "__ROOM_BRIDGE__",
    "__ROOM_BRIDGE_CH2__",
    "__LXMF_PEER_DEST__",
):
    if placeholder in text:
        raise SystemExit(f"unsubstituted placeholder remains in medre-normal config: {placeholder}")
with open("lab/state/config/medre-normal.yaml", "w", encoding="utf-8") as handle:
    handle.write(text)
print("  medre-normal.yaml generated (token + bridge rooms + lxmf peer hash substituted)")
PYGEN
fi

# --- meshtasticd node pairs ---------------------------------------------------------
log "Starting meshtasticd nodes"
NODES=(meshtasticd-a meshtasticd-a-peer meshtasticd-b meshtasticd-b-peer)
if ((WITH_MEDRE)); then
	# Dedicated medre nodes (compose profile `medre`): meshtasticd-a-medre on
	# AlphaLab, meshtasticd-b-medre on BetaLab. Owned exclusively by
	# medre-relay once it starts (one API client per node).
	NODES+=("${MEDRE_NODES[@]}")
fi
if ((WITH_NORMAL)); then
	# Normal-profile MeshC pair (compose profile `normal`): meshtasticd-c-medre
	# is owned exclusively by medre-normal once it starts; the peer node is the
	# tooling injection/witness surface.
	NODES+=("${NORMAL_NODES[@]}")
fi
"${COMPOSE[@]}" up -d "${NODES[@]}"
for node in "${NODES[@]}"; do
	wait_healthy "medre-lab-mesh-${node#meshtasticd-}" 240
done

# Channel writes need the nodes' API exclusively: a reconnecting relay force-
# closes the CLI's admin-session handshake mid-flight. On re-runs the relays
# (and the medre relays) may already be up, so stop them for the channel phase.
"${COMPOSE[@]}" stop mmrelay-a mmrelay-b >/dev/null 2>&1 || true
if ((WITH_MEDRE)); then
	"${COMPOSE[@]}" stop medre-relay >/dev/null 2>&1 || true
fi
if ((WITH_NORMAL)); then
	# medre-normal owns meshtasticd-c-medre's only API client slot.
	"${COMPOSE[@]}" stop medre-normal >/dev/null 2>&1 || true
fi

# shellcheck disable=SC1091
source lab/config/meshnet.env

configure_channel() {
	local host=$1 ch_index=$2 key=$3 value=$4
	local attempt
	for attempt in 1 2 3; do
		if docker compose exec -T lab-tools \
			python -m meshtastic --timeout 25 --host "${host}" --ch-set "${key}" "${value}" --ch-index "${ch_index}" >/dev/null 2>&1; then
			return 0
		fi
		echo "  [retry ${attempt}] ch-set ${key} on ${host} idx ${ch_index} failed"
		sleep 5
	done
	die "channel configuration failed for ${host} (index ${ch_index}, ${key})"
}

log "Configuring mesh channels on the meshnet members (primary idx 0 + secondary idx 2)"
CHANNEL_ENTRIES=(
	"meshtasticd-a ${MESH_A_PRIMARY_NAME} ${MESH_A_PRIMARY_PSK} ${MESH_A_SECONDARY_INDEX} ${MESH_A_SECONDARY_NAME} ${MESH_A_SECONDARY_PSK}"
	"meshtasticd-a-peer ${MESH_A_PRIMARY_NAME} ${MESH_A_PRIMARY_PSK} ${MESH_A_SECONDARY_INDEX} ${MESH_A_SECONDARY_NAME} ${MESH_A_SECONDARY_PSK}"
	"meshtasticd-b ${MESH_B_PRIMARY_NAME} ${MESH_B_PRIMARY_PSK} ${MESH_B_SECONDARY_INDEX} ${MESH_B_SECONDARY_NAME} ${MESH_B_SECONDARY_PSK}"
	"meshtasticd-b-peer ${MESH_B_PRIMARY_NAME} ${MESH_B_PRIMARY_PSK} ${MESH_B_SECONDARY_INDEX} ${MESH_B_SECONDARY_NAME} ${MESH_B_SECONDARY_PSK}"
)
if ((WITH_MEDRE)); then
	# The medre nodes share their meshnet's channel identity (same names and
	# PSKs) so they mesh over the simulated air with the existing pair.
	CHANNEL_ENTRIES+=(
		"meshtasticd-a-medre ${MESH_A_PRIMARY_NAME} ${MESH_A_PRIMARY_PSK} ${MESH_A_SECONDARY_INDEX} ${MESH_A_SECONDARY_NAME} ${MESH_A_SECONDARY_PSK}"
		"meshtasticd-b-medre ${MESH_B_PRIMARY_NAME} ${MESH_B_PRIMARY_PSK} ${MESH_B_SECONDARY_INDEX} ${MESH_B_SECONDARY_NAME} ${MESH_B_SECONDARY_PSK}"
	)
fi
if ((WITH_NORMAL)); then
	# MeshC/GammaLab (medre-normal's OWN meshnet): LabPrimC/LabSecC, distinct
	# PSKs from every other meshnet (lab/config/meshnet.env).
	CHANNEL_ENTRIES+=(
		"meshtasticd-c-medre ${MESH_C_PRIMARY_NAME} ${MESH_C_PRIMARY_PSK} ${MESH_C_SECONDARY_INDEX} ${MESH_C_SECONDARY_NAME} ${MESH_C_SECONDARY_PSK}"
		"meshtasticd-c-peer ${MESH_C_PRIMARY_NAME} ${MESH_C_PRIMARY_PSK} ${MESH_C_SECONDARY_INDEX} ${MESH_C_SECONDARY_NAME} ${MESH_C_SECONDARY_PSK}"
	)
fi
for entry in "${CHANNEL_ENTRIES[@]}"; do
	# shellcheck disable=SC2086
	read -r host prim_name prim_psk sec_idx sec_name sec_psk <<<"${entry}"
	configure_channel "${host}" 0 name "${prim_name}"
	configure_channel "${host}" 0 psk "${prim_psk}"
	configure_channel "${host}" "${sec_idx}" name "${sec_name}"
	configure_channel "${host}" "${sec_idx}" psk "${sec_psk}"
	echo "  ${host}: primary=${prim_name} secondary[${sec_idx}]=${sec_name}"
done
# Re-check readiness after channel writes (CI-lifted wait-after-config pattern)
for node in "${NODES[@]}"; do
	wait_healthy "medre-lab-mesh-${node#meshtasticd-}" 240
done

log "Snapshotting node identities + over-air mesh visibility"
if ((WITH_MEDRE)); then
	# Includes the medre nodes as third meshnet members (script gates the
	# NODE_PEERS map on the same env var). Runs before any relay/medre
	# container starts, so node API exclusivity holds.
	"${COMPOSE[@]}" run --rm -e LAB_MEDRE_PROFILE=1 lab-tools python /opt/lab-tools/mesh_snapshot.py
elif ((WITH_NORMAL)); then
	# Includes the MeshC pair (script gates on LAB_NORMAL_PROFILE). Runs
	# before medre-normal starts, so the c-medre node's API is still free.
	"${COMPOSE[@]}" run --rm -e LAB_NORMAL_PROFILE=1 lab-tools python /opt/lab-tools/mesh_snapshot.py
else
	"${COMPOSE[@]}" run --rm lab-tools python /opt/lab-tools/mesh_snapshot.py
fi

# --- Relays -------------------------------------------------------------------------
log "Starting MMRelay instances"
RELAY_SERVICES=()
for relay in "${RELAYS[@]}"; do
	RELAY_SERVICES+=("mmrelay-${relay}")
done
"${COMPOSE[@]}" up -d "${RELAY_SERVICES[@]}"
for relay in "${RELAYS[@]}"; do
	wait_healthy "medre-lab-mmrelay-${relay}" 240
done
# The mmrelay ready file is written only after the Matrix sync loop is running
# (see mmrelay main.py), so healthy == synced Matrix session.

# --- MEDRE relay ---------------------------------------------------------------------
if ((WITH_MEDRE)); then
	log "Starting medre relay (matrix + radio-a + radio-b in one runtime)"
	"${COMPOSE[@]}" up -d medre-relay
	wait_healthy "medre-lab-medre-relay" 240
	log "Verifying medre bot joined the four bridged rooms"
	"${COMPOSE[@]}" run --rm \
		-e "ROOM_KEYS=lab-alpha-ch0 lab-alpha-ch2 lab-beta-ch0 lab-beta-ch2" \
		lab-tools python - <<'PYJOIN'
import os
import sys
import time

from lab_lib import load_runtime, matrix_request

runtime = load_runtime()
admin_token = runtime["users"]["admin"]["access_token"]
bot = runtime["users"]["medre"]["user_id"]
rooms = [
    (key, runtime["rooms"][key]["room_id"])
    for key in os.environ["ROOM_KEYS"].split()
]
deadline = 60
joined_all = False

while deadline > 0:
    joined_all = True
    for alias, room_id in rooms:
        quoted = room_id.replace("!", "%21").replace(":", "%3A")
        response = matrix_request(
            "GET", f"/_matrix/client/v3/rooms/{quoted}/joined_members", admin_token
        )
        members = (
            response.json().get("joined", {}) if response.status_code == 200 else {}
        )
        if bot not in members:
            joined_all = False
            print(f"  {alias}: {bot} not joined yet")
    if joined_all:
        break
    time.sleep(3)
    deadline -= 3
if not joined_all:
    print(f"MEDRE JOIN FAIL: {bot} did not join all mapped rooms")
    sys.exit(1)
print(f"  {bot} joined all mapped rooms: {', '.join(alias for alias, _ in rooms)}")
PYJOIN
fi

# --- MEDRE normal relay -------------------------------------------------------------
if ((WITH_NORMAL)); then
	log "Starting medre-normal (matrix + radio-c + lxmf loopback in one runtime)"
	"${COMPOSE[@]}" up -d medre-normal
	wait_healthy "medre-lab-medre-normal" 240
	log "Verifying medre bot joined the shared bridge rooms"
	"${COMPOSE[@]}" run --rm \
		-e "ROOM_KEYS=lab-bridge lab-bridge-ch2" \
		lab-tools python - <<'PYJOIN'
import os
import sys
import time

from lab_lib import load_runtime, matrix_request

runtime = load_runtime()
admin_token = runtime["users"]["admin"]["access_token"]
bot = runtime["users"]["medre"]["user_id"]
rooms = [
    (key, runtime["rooms"][key]["room_id"])
    for key in os.environ["ROOM_KEYS"].split()
]
deadline = 60
joined_all = False

while deadline > 0:
    joined_all = True
    for alias, room_id in rooms:
        quoted = room_id.replace("!", "%21").replace(":", "%3A")
        response = matrix_request(
            "GET", f"/_matrix/client/v3/rooms/{quoted}/joined_members", admin_token
        )
        members = (
            response.json().get("joined", {}) if response.status_code == 200 else {}
        )
        if bot not in members:
            joined_all = False
            print(f"  {alias}: {bot} not joined yet")
    if joined_all:
        break
    time.sleep(3)
    deadline -= 3
if not joined_all:
    print(f"MEDRE JOIN FAIL: {bot} did not join all mapped rooms")
    sys.exit(1)
print(f"  {bot} joined all mapped rooms: {', '.join(alias for alias, _ in rooms)}")
PYJOIN
fi

# --- Scenario env (contract interface for the scenario layer) ------------------------
log "Publishing lab/state/scenario.env"
UNTRUSTED_TOKEN="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["users"]["untrusted"]["access_token"])')"
ALICE_TOKEN="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["users"]["alice"]["access_token"])')"
BOB_TOKEN="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["users"]["bob"]["access_token"])')"
cat >lab/state/scenario.env <<EOF
# Written by lab/up.sh at bring-up. Sourced AFTER scenarios/config.env;
# overrides the contract defaults. Tokens rotate on every provision run.
ROOM_ALPHA_CH0_ALIAS="#lab-alpha-ch0:lab.test"
ROOM_ALPHA_CH2_ALIAS="#lab-alpha-ch2:lab.test"
ROOM_BETA_CH0_ALIAS="#lab-beta-ch0:lab.test"
ROOM_BETA_CH2_ALIAS="#lab-beta-ch2:lab.test"
# Single-bridge-era compat: smoke path is Alpha ch0.
ROOM_BRIDGE_ALIAS="\${ROOM_ALPHA_CH0_ALIAS}"
ROOM_CH2_ALIAS="\${ROOM_ALPHA_CH2_ALIAS}"
ROOM_OTHER_ALIAS="#lab-other:lab.test"
UNTRUSTED_TOKEN="${UNTRUSTED_TOKEN}"
ALICE_TOKEN="${ALICE_TOKEN}"
BOB_TOKEN="${BOB_TOKEN}"
MAP_DB_A="/state/mmrelay-a/database/meshtastic.sqlite"
MAP_DB_B="/state/mmrelay-b/database/meshtastic.sqlite"
PLUGIN_OBS_A="/lab-logs/scenarios/echo-observed-a.jsonl"
PLUGIN_OBS_B="/lab-logs/scenarios/echo-observed-b.jsonl"
# Threenode profile is parked by owner directive; no relay-C surfaces.
PLUGIN_OBS_C=""
# NOTE: radio injection must go through the PEER nodes (meshtasticd-a-peer /
# meshtasticd-b-peer). The relay-connected primary nodes force-close their
# previous TCP API client on every new connection.
NODE_A_INJECT_HOST="meshtasticd-a-peer"
NODE_B_INJECT_HOST="meshtasticd-b-peer"
NODE_A_WITNESS_HOST="meshtasticd-a-peer"
NODE_B_WITNESS_HOST="meshtasticd-b-peer"
EOF
if ((WITH_MEDRE)); then
	MEDRE_TOKEN="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["users"]["medre"]["access_token"])')"
	cat >>lab/state/scenario.env <<EOF
# --- MEDRE bridge profile (written only on --with-medre bring-ups) ---------
MEDRE_BOT_MXID="\${MEDRE_BOT_MXID:-${MEDRE_BOT_MXID}}"
MEDRE_BOT_TOKEN="${MEDRE_TOKEN}"
MEDRE_MAP_DB="/state/medre/state/medre.sqlite"
# Dedicated medre-owned nodes (relay-connected primaries: NEVER inject or
# listen through these; use the peer hosts above).
NODE_A_MEDRE_HOST="meshtasticd-a-medre"
NODE_B_MEDRE_HOST="meshtasticd-b-medre"
EOF
fi
if ((WITH_NORMAL)); then
	MEDRE_TOKEN="$(python3 -c 'import json;print(json.load(open("lab/state/runtime.json"))["users"]["medre"]["access_token"])')"
	cat >>lab/state/scenario.env <<EOF
# --- NORMAL profile (written only on --normal bring-ups) -------------------
# The shared surface is the bridge room pair; the per-meshnet rooms above
# are mapped NOWHERE by the relays in this mode.
MEDRE_BOT_MXID="\${MEDRE_BOT_MXID:-${MEDRE_BOT_MXID}}"
MEDRE_BOT_TOKEN="${MEDRE_TOKEN}"
MEDRE_NORMAL_MAP_DB="/state/medre-normal/state/medre.sqlite"
MEDRE_NORMAL_ROOM_BRIDGE_ALIAS="#lab-bridge:lab.test"
MEDRE_NORMAL_ROOM_BRIDGE_CH2_ALIAS="#lab-bridge-ch2:lab.test"
ROOM_BRIDGE_CH0_ALIAS="#lab-bridge:lab.test"
ROOM_BRIDGE_CH2_ALIAS="#lab-bridge-ch2:lab.test"
# The lxmf0 loopback adapter is part of the normal contract's medre-normal
# runtime, so it is ALWAYS live in this mode: publish the declaration the
# scenario suite keys its conditional lxmf leg on (regenerated on every
# --normal bring-up, so the leg is not silently dropped after a rebuild).
MEDRE_NORMAL_LXMF="1"
# MeshC = GammaLab = meshtasticd-c-medre (medre-normal-owned primary: NEVER
# inject or listen through it) + meshtasticd-c-peer (tooling surface).
NODE_C_INJECT_HOST="meshtasticd-c-peer"
NODE_C_WITNESS_HOST="meshtasticd-c-peer"
NODE_C_MEDRE_HOST="meshtasticd-c-medre"
# LXMF loopback leg: counterpart driver runs INSIDE medre-normal
#   docker compose --profile normal exec -T medre-normal \\
#     python /opt/lab-tools/lxmf_counterpart.py --identity /data-lxmf/peer.identity \\
#       --reticulum-config /data-lxmf/reticulum-peer --storage /data-lxmf/peer-router ...
LXMF_PEER_IDENTITY="/data-lxmf/peer.identity"
LXMF_PEER_DESTINATION_HASH="${LXMF_PEER_HASH}"
LXMF_COUNTERPART_LOG="/lab-logs/lxmf-counterpart-$(date +%Y%m%d-%H%M%S).log"
EOF
fi
echo "  lab/state/scenario.env written"

# --- Smoke ----------------------------------------------------------------------------
if ((! SKIP_SMOKE)); then
	if ((WITH_NORMAL)); then
		# The base 2-relay smoke exercises the four per-meshnet room mapping,
		# which the normal profile deliberately maps NOWHERE — the normal
		# contract has its own smoke instead.
		log "Generation-consistency gate (bridge containers must postdate their generated configs)"
		require_config_fresh medre-lab-mmrelay-a lab/state/config/mmrelay-a.yaml
		require_config_fresh medre-lab-mmrelay-b lab/state/config/mmrelay-b.yaml
		require_config_fresh medre-lab-medre-normal lab/state/config/medre-normal.yaml
		log "Running normal-profile smoke (fan-out x3 meshes, loop-freedom settle, radio->room)"
		"${COMPOSE[@]}" exec -T lab-tools python /opt/lab-tools/normal_smoke.py
		log "LXMF counterpart exchange (loopback Reticulum leg, inside medre-normal)"
		LXMF_LOG="${LOG_DIR}/lxmf-counterpart-${TS}.log"
		LXMF_RX_MARKER="LXMF_RX_${TS}"
		LXMF_TX_MARKER="LXMF_TX_${TS}"
		"${COMPOSE[@]}" exec -T medre-normal python /opt/lab-tools/lxmf_counterpart.py \
			--identity /data-lxmf/peer.identity \
			--reticulum-config /data-lxmf/reticulum-peer \
			--storage /data-lxmf/peer-router \
			--display-name lab-counterpart \
			--expect-text "${LXMF_RX_MARKER}" \
			--send-text "${LXMF_TX_MARKER}" \
			--timeout 240 >"${LXMF_LOG}" 2>&1 &
		LXMF_PID=$!
		LXMF_READY=0
		for _attempt in $(seq 1 30); do
			if grep -q "LXMF_COUNTERPART_READY=" "${LXMF_LOG}" 2>/dev/null; then
				LXMF_READY=1
				break
			fi
			if ! kill -0 "${LXMF_PID}" 2>/dev/null; then
				break
			fi
			sleep 2
		done
		if ((LXMF_READY)); then
			# Room -> LXMF leg: the counterpart must receive a copy of a human
			# text sent into #lab-bridge while it is armed.
			"${COMPOSE[@]}" exec -T lab-tools python - "${LXMF_RX_MARKER}" <<'PYLXMFSEND'
import sys
import time

from lab_lib import load_runtime, matrix_request

marker = sys.argv[1]
runtime = load_runtime()
token = runtime["users"]["alice"]["access_token"]
room = runtime["rooms"]["lab-bridge"]["room_id"]
response = matrix_request(
    "PUT",
    f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{int(time.time() * 1000)}",
    token,
    {"msgtype": "m.text", "body": marker},
)
if response.status_code >= 400:
    raise SystemExit(f"alice send failed: {response.status_code} {response.text}")
print(f"  alice sent {marker} into #lab-bridge")
PYLXMFSEND
			LXMF_RC=0
			wait "${LXMF_PID}" || LXMF_RC=$?
			RESULT_LINE="$(sed -n 's/.*LXMF_COUNTERPART_RESULT=//p' "${LXMF_LOG}" | tail -1)"
			if [[ -z ${RESULT_LINE} ]]; then
				die "lxmf counterpart produced no RESULT line (rc=${LXMF_RC}); log: ${LXMF_LOG}"
			fi
			export LXMF_RC LXMF_RX_MARKER LXMF_TX_MARKER LXMF_LOG RESULT_LINE
			python3 - <<'PYLXMFCHECK'
import json
import os
import sys

result = json.loads(os.environ["RESULT_LINE"])
problems = []
if not result.get("medre_announce_seen"):
    problems.append("no medre announce seen")
if not any(
    os.environ["LXMF_RX_MARKER"] in str(message.get("content", ""))
    for message in result.get("received", [])
):
    problems.append(f"expected inbound {os.environ['LXMF_RX_MARKER']} not received")
if not result.get("sent_delivered"):
    problems.append("counterpart -> medre delivery not confirmed")
if problems:
    print("LXMF COUNTERPART FAIL:", "; ".join(problems))
    print(json.dumps(result, indent=2))
    sys.exit(1)
print(
    "  counterpart received the room fan-out copy (matrix -> medre lxmf0 -> "
    "counterpart) and its own send was DELIVERED to medre"
)
PYLXMFCHECK
			# LXMF -> room leg: medre must surface the counterpart's message in
			# #lab-bridge as a canonical @medre event.
			"${COMPOSE[@]}" exec -T lab-tools python - "${LXMF_TX_MARKER}" <<'PYLXMFROOM'
import json
import sys
import time

from lab_lib import load_runtime, matrix_request

marker = sys.argv[1]
runtime = load_runtime()
admin_token = runtime["users"]["admin"]["access_token"]
medre_user = runtime["users"]["medre"]["user_id"]
room_id = runtime["rooms"]["lab-bridge"]["room_id"]
deadline = time.monotonic() + 120
found = None
while time.monotonic() < deadline:
    quoted = room_id.replace("!", "%21").replace(":", "%3A")
    response = matrix_request(
        "GET",
        f"/_matrix/client/v3/rooms/{quoted}/messages",
        admin_token,
        params={"dir": "b", "limit": "50"},
    )
    if response.status_code == 200:
        for event in response.json().get("chunk", []):
            if (
                event.get("sender") == medre_user
                and marker in str(event.get("content", {}).get("body", ""))
            ):
                found = event
                break
    if found:
        break
    time.sleep(2)
if found is None:
    print("LXMF ROOM LEG FAIL: no @medre event for the counterpart message")
    sys.exit(1)
envelope = (
    found.get("content", {}).get("medre", {}).get("envelope", {})
)
record = {
    "verdict": "PASS",
    "room_event_id": found.get("event_id"),
    "room_event_body": found.get("content", {}).get("body", ""),
    "envelope": envelope,
}
with open("/lab-logs/lxmf-counterpart-result.json", "w", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2)
    handle.write("\n")
print(f"  medre surfaced the counterpart message: {found.get('event_id')}")
print("LXMF COUNTERPART PASS (both legs)")
PYLXMFROOM
		else
			die "lxmf counterpart never became READY (log: ${LXMF_LOG}) — run the counterpart manually with:
  docker compose --profile normal exec -T medre-normal python /opt/lab-tools/lxmf_counterpart.py --identity /data-lxmf/peer.identity --reticulum-config /data-lxmf/reticulum-peer --storage /data-lxmf/peer-router --timeout 240"
		fi
	else
		log "Running interop smoke (peer-a radio -> #lab-alpha-ch0 -> relay B -> Beta radio)"
		"${COMPOSE[@]}" exec -T lab-tools python /opt/lab-tools/smoke.py
		if ((WITH_MEDRE)); then
			log "Running medre bridge smoke (dual envelopes + dual radio sends, no loops)"
			"${COMPOSE[@]}" exec -T lab-tools python /opt/lab-tools/medre_smoke.py
		fi
	fi
else
	log "Skipping smoke (--skip-smoke)"
fi
# NOTE (cross-bridge loop hazard): applies to the STRESS `medre` profile
# only. With medre-relay running there, room <-> radio cycling between the
# mmrelay relays' committed cross-mesh mapping and medre's radio->Matrix
# echoes can flood both radios (see lab/README.md, MEDRE bridge profile,
# KNOWN LOOP HAZARD). Bring-up leaves the relay RUNNING per the medre-profile
# contract; stop it with `docker compose --profile medre stop medre-relay`
# when mmrelay scenario throughput degrades, and start it again on demand.
# The NORMAL profile is the loop-free star: every bridge owns its own
# meshnet, so no radio TX is ever heard by another bridge.

# --- Records ----------------------------------------------------------------------------
log "Capturing bring-up logs"
"${COMPOSE[@]}" logs --no-color --timestamps >"${LOG_DIR}/bringup-${TS}.log"
for relay in "${RELAYS[@]}"; do
	docker logs "medre-lab-mmrelay-${relay}" >"${LOG_DIR}/mmrelay-${relay}-${TS}.log" 2>&1
done
if ((WITH_MEDRE)); then
	docker logs "medre-lab-medre-relay" >"${LOG_DIR}/medre-relay-${TS}.log" 2>&1
fi
if ((WITH_NORMAL)); then
	docker logs "medre-lab-medre-normal" >"${LOG_DIR}/medre-normal-${TS}.log" 2>&1
fi
echo "  logs: ${LOG_DIR}/bringup-${TS}.log (+ per-relay logs)"

MESHTASTICD_REF="$(awk '$1 == "FROM" {print $2; exit}' lab/meshtasticd/Dockerfile)"
SYNAPSE_REF="$(awk '$1 == "FROM" {print $2; exit}' lab/synapse/Dockerfile)"
# Record the running containers' image IDs as well as the declared bases. With
# --skip-build a previously built wrapper need not match the current FROM.
MESHTASTICD_IMAGE_ID="$(docker inspect --type container --format '{{.Image}}' medre-lab-mesh-a)"
SYNAPSE_IMAGE_ID="$(docker inspect --type container --format '{{.Image}}' medre-lab-synapse)"
cat >lab/VERSION <<EOF
# hardware-lab version record (rewritten by lab/up.sh at each bring-up)
recorded_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
mmrelay:
  checkout: ${MMRELAY_SRC}
  branch: ${BRANCH}
  head: ${HEAD_SHA}
images:
  meshtasticd_declared_base: ${MESHTASTICD_REF}
  meshtasticd_local_image_id: ${MESHTASTICD_IMAGE_ID}
  meshtasticd_firmware: $(docker exec medre-lab-mesh-a meshtasticd -V 2>/dev/null | tail -1)
  meshtasticd_fallback: meshtastic/meshtasticd@sha256:23e92b1331a3a471eaef0c63cbca4365ca40b3111a9781cfdbe5a5114e5773d4 # 2.7.26, last stable that passed the mesh probe; pin here temporarily if a daily breaks bring-up
  synapse_declared_base: ${SYNAPSE_REF}
  synapse_local_image_id: ${SYNAPSE_IMAGE_ID}
  meshtasticd_runtime: medre-lab/meshtasticd-lab:latest (runtime user 1000:1000; declared base above, actual local image ID recorded)
  mmrelay-dev: medre-lab/mmrelay-dev:latest (built from lab/mmrelay/Dockerfile; deps baked from mmrelay pyproject ".[e2e]")
  lab-tools: medre-lab/lab-tools:latest (built from lab/tools/Dockerfile)
topology: AlphaLab = meshtasticd-a + meshtasticd-a-peer (mmrelay-a); BetaLab = meshtasticd-b + meshtasticd-b-peer (mmrelay-b)
threenode_profile: parked by owner directive (removed from compose)
EOF
if ((WITH_MEDRE)); then
	cat >>lab/VERSION <<EOF
medre_profile:
  bot: ${MEDRE_BOT_MXID}
  core: ${MEDRE_CORE_SRC} @ $(git -C "${MEDRE_CORE_SRC}" rev-parse HEAD)
  adapter_matrix: ${MEDRE_MATRIX_SRC} @ $(git -C "${MEDRE_MATRIX_SRC}" rev-parse HEAD)
  adapter_meshtastic: ${MEDRE_MESHTASTIC_SRC} @ $(git -C "${MEDRE_MESHTASTIC_SRC}" rev-parse HEAD)
  image: medre-lab/medre-dev:latest (built from lab/medre/Dockerfile; deps baked from the selected checkouts, worktrees mounted read-only)
  medre_topology: AlphaLab += meshtasticd-a-medre, BetaLab += meshtasticd-b-medre (dedicated nodes, one API client each: medre-relay)
EOF
fi
if ((WITH_NORMAL)); then
	cat >>lab/VERSION <<EOF
normal_profile:
  bot: ${MEDRE_BOT_MXID}
  core: ${MEDRE_CORE_SRC} @ $(git -C "${MEDRE_CORE_SRC}" rev-parse HEAD)
  adapter_matrix: ${MEDRE_MATRIX_SRC} @ $(git -C "${MEDRE_MATRIX_SRC}" rev-parse HEAD)
  adapter_meshtastic: ${MEDRE_MESHTASTIC_SRC} @ $(git -C "${MEDRE_MESHTASTIC_SRC}" rev-parse HEAD)
  adapter_lxmf: ${MEDRE_LXMF_SRC} @ $(git -C "${MEDRE_LXMF_SRC}" rev-parse HEAD) (READ-ONLY)
  image: medre-lab/medre-dev:latest (built from lab/medre/Dockerfile; lxmf deps baked, worktrees mounted read-only)
  normal_topology: star, loop-free: mmrelay-a on AlphaLab, mmrelay-b on BetaLab, medre-normal on its OWN MeshC/GammaLab (meshtasticd-c-medre + meshtasticd-c-peer, one API client each) + lxmf loopback leg
  shared_rooms: #lab-bridge (ch0) / #lab-bridge-ch2 (ch2); mmrelay-a/b map ONLY these two rooms in this mode
EOF
fi

log "Interop lab is UP"
cat <<'EOF'
  Synapse:         http://127.0.0.1:8008 (server_name lab.test)
  meshtasticd-a:   127.0.0.1:4403   meshtasticd-a-peer: 127.0.0.1:4405
  meshtasticd-b:   127.0.0.1:4404   meshtasticd-b-peer: 127.0.0.1:4406
  Runtime JSON:    lab/state/runtime.json (tokens, room IDs, node IDs)
  Scenario env:    lab/state/scenario.env (aliases, tokens, map DB paths)
  Smoke proof:     lab/logs/smoke-result.json
EOF
if ((WITH_MEDRE)); then
	cat <<'EOF'
  medre nodes:     meshtasticd-a-medre 127.0.0.1:4407, meshtasticd-b-medre 127.0.0.1:4408
  medre-relay:     @medre:lab.test (radio-a: meshtasticd-a-medre, radio-b: meshtasticd-b-medre)
  Medre map db:    medre-state volume -> /data/state/medre.sqlite (lab-tools: /state/medre/state/medre.sqlite)
  Medre smoke:     lab/logs/medre-smoke-result.json
  Loop hazard:     see lab/README.md (MEDRE bridge profile, KNOWN LOOP HAZARD)
EOF
fi
if ((WITH_NORMAL)); then
	cat <<EOF
  normal profile:  mmrelay-a on AlphaLab, mmrelay-b on BetaLab (bridge rooms only)
  medre nodes:     meshtasticd-c-medre 127.0.0.1:4409, meshtasticd-c-peer 127.0.0.1:4410
  medre-normal:    @medre:lab.test (radio-c: meshtasticd-c-medre [MeshC/GammaLab], lxmf0: loopback Reticulum)
  Medre map db:    medre-normal-state volume -> /data/state/medre.sqlite (lab-tools: /state/medre-normal/state/medre.sqlite)
  LXMF peer:       lab/state/lxmf/peer.identity -> dest ${LXMF_PEER_HASH} (counterpart: lab/tools/lxmf_counterpart.py)
  Normal smoke:    lab/logs/normal-smoke-result.json
  LXMF exchange:   lab/logs/lxmf-counterpart-${TS}.log + lab/logs/lxmf-counterpart-result.json
EOF
fi
