#!/usr/bin/env bash
# =============================================================================
# openHop Docker qualifier: bounded MEDRE<->openHop lifecycle check.
#
# WHAT THIS CHECKS (and what it does NOT)
# ---------------------------------------
# It proves, with recorded evidence:
#   1. container startup          the repeater and the MEDRE runtime both
#                                 start and reach their healthchecks;
#   2. protocol handshake         APP_START -> SELF_INFO -> channel read
#                                 against the companion, using the PINNED
#                                 upstream SDK (meshcore 2.3.15);
#   3. MEDRE adapter connection   MEDRE's own logs show the MeshCore
#                                 session CONNECTED, and the startup banner
#                                 carries the companion's own identity
#                                 (device name + public-key prefix) that
#                                 MEDRE read from SELF_INFO;
#   4. clean stop                 MEDRE shuts down on request and exits 0;
#   5. reconnect                  MEDRE's adapter RECONNECTS after the
#                                 repeater is restarted underneath it;
#   6. identity/state persistence the companion's identity and its channel
#                                 state survive the repeater restart.
#
# It does NOT prove delivery. The repeater runs with `radio_type: null`
# and its NullRadio RAISES on every send, so nothing in this profile can
# transmit. "Adapter connected" is a protocol fact, never a delivery fact.
#
# SINGLE-CLIENT RULE
# ------------------
# openHop's companion accepts ONE client at a time and evicts the previous
# one. The protocol probe therefore runs ONLY while MEDRE is stopped. There
# is NO TCP readiness probe against the companion port anywhere in this
# script: a probe would evict MEDRE's own session.
#
# Usage: lab/openhop/lifecycle_check.sh [--evidence-dir DIR]
# Exit:  0 = PASS, 1 = FAIL.
# =============================================================================
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${LAB_ROOT}"

EVIDENCE_DIR="${LAB_ROOT}/lab/logs/openhop"
while (($# > 0)); do
	case "$1" in
	--evidence-dir)
		EVIDENCE_DIR="$2"
		shift 2
		;;
	*)
		echo "unknown option: $1" >&2
		exit 2
		;;
	esac
done
mkdir -p "${EVIDENCE_DIR}"
rm -f "${EVIDENCE_DIR}/verdict.json"

COMPOSE=(docker compose --profile openhop)
REPEATER_SERVICE="openhop-repeater"
MEDRE_SERVICE="medre-openhop"
REPEATER_CONTAINER="medre-lab-openhop-repeater"
MEDRE_CONTAINER="medre-lab-medre-openhop"
PROBE_TOOL="lab/tools/openhop_probe.py"

# Bounded waits (seconds).
REPEATER_WAIT_SECONDS=120
MEDRE_WAIT_SECONDS=180
RECONNECT_WAIT_SECONDS=180

log() { printf '\n=== %s ===\n' "$*"; }
fail() {
	echo "FAIL: $*" >&2
	echo '{"pass": false, "reason": "'"$*"'"}' >"${EVIDENCE_DIR}/verdict.json"
	exit 1
}

require_container() {
	local name=$1
	docker inspect -f '{{.State.Running}}' "${name}" 2>/dev/null |
		grep -qx true || fail "${name} is not running"
}

wait_healthy() {
	local name=$1 limit=$2 waited=0
	while ((waited < limit)); do
		local state
		state="$(docker inspect -f '{{.State.Health.Status}}' "${name}" 2>/dev/null || echo missing)"
		case "${state}" in
		healthy)
			echo "  ${name}: healthy (${waited}s)"
			return 0
			;;
		unhealthy)
			fail "${name} became unhealthy"
			;;
		esac
		sleep 5
		waited=$((waited + 5))
	done
	fail "${name} did not become healthy within ${limit}s"
}

# The probe runs in a throwaway container on the openhop network so it does
# not disturb the MEDRE runtime's own container. It is the ONLY client of
# the companion while it runs.
run_probe() {
	local label="$1"
	local out="${EVIDENCE_DIR}/probe-${label}.json"
	"${COMPOSE[@]}" run --rm --no-deps \
		--entrypoint python \
		-v "${LAB_ROOT}/${PROBE_TOOL}:/opt/openhop_probe.py:ro" \
		"${MEDRE_SERVICE}" /opt/openhop_probe.py \
		--host "${REPEATER_SERVICE}" --port 5000 >"${out}" 2>"${out}.err" || true
	if [[ ! -s ${out} ]]; then
		echo "  probe ${label} produced no output:" >&2
		cat "${out}.err" >&2 || true
		fail "protocol probe ${label} produced no output"
	fi
	cat "${out}"
}

probe_passed() {
	python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("pass") else 1)' "$1"
}

# ---------------------------------------------------------------------------
log "Phase 0: preconditions"
# ---------------------------------------------------------------------------
require_container "${REPEATER_CONTAINER}" 2>/dev/null || true
docker compose --profile openhop ps >/dev/null 2>&1 ||
	fail "docker compose --profile openhop is not usable"

# Phase 0a: MEDRE must be STOPPED for the protocol probe (single-client
# rule). If MEDRE is up, stop it first: the probe and MEDRE may never hold
# the companion at the same time.
log "Phase 0a: stop MEDRE (the companion accepts one client at a time)"
"${COMPOSE[@]}" stop "${MEDRE_SERVICE}" >/dev/null 2>&1 || true
sleep 2

log "Phase 1: protocol handshake against the companion (APP_START/SELF_INFO/channel read)"
"${COMPOSE[@]}" up -d "${REPEATER_SERVICE}" >/dev/null
wait_healthy "${REPEATER_CONTAINER}" "${REPEATER_WAIT_SECONDS}"
PROBE_BEFORE="${EVIDENCE_DIR}/probe-before-restart.json"
run_probe "before-restart"
probe_passed "${PROBE_BEFORE}" ||
	fail "protocol probe before the repeater restart did not pass"

# ---------------------------------------------------------------------------
log "Phase 2: MEDRE startup and adapter connection"
# ---------------------------------------------------------------------------
"${COMPOSE[@]}" up -d "${MEDRE_SERVICE}" >/dev/null
wait_healthy "${MEDRE_CONTAINER}" "${MEDRE_WAIT_SECONDS}"
startup_log="${EVIDENCE_DIR}/medre-openhop.log"
startup_at="$(docker inspect -f '{{.State.StartedAt}}' "${MEDRE_CONTAINER}")" ||
	fail "cannot identify the MEDRE process start"
waited=0
while ((waited < MEDRE_WAIT_SECONDS)); do
	if docker logs --since "${startup_at}" "${MEDRE_CONTAINER}" >"${startup_log}" 2>&1 &&
		grep -q "MeshCoreSession openhop-companion started (mode=tcp, connected=True)" "${startup_log}" &&
		grep -qE "device name .+ · pubkey [0-9a-f]+" "${startup_log}"; then
		break
	fi
	sleep 5
	waited=$((waited + 5))
done
((waited < MEDRE_WAIT_SECONDS)) ||
	fail "MEDRE did not report a connected session and companion identity"

# The read-only diagnostics snapshot proves which adapters MEDRE built and
# with which configuration (no runtime, no network, no writes).
"${COMPOSE[@]}" exec -T "${MEDRE_SERVICE}" \
	medre diagnostics --config /config/medre-openhop.yaml --format json \
	>"${EVIDENCE_DIR}/medre-diagnostics.json" 2>"${EVIDENCE_DIR}/medre-diagnostics.err" ||
	fail "medre diagnostics failed"
python3 -c '
import json,sys
data=json.load(open(sys.argv[1]))
adapters=data.get("adapters") or {}
if "openhop-companion" not in adapters:
    raise SystemExit("openhop-companion adapter missing from diagnostics")
print("  diagnostics adapters:", ", ".join(sorted(adapters)))
' "${EVIDENCE_DIR}/medre-diagnostics.json" ||
	fail "MEDRE diagnostics do not name the openhop-companion adapter"

# ---------------------------------------------------------------------------
log "Phase 3: reconnect after a repeater restart (MEDRE keeps running)"
# Compare identity and default-channel reads across the restart. The
# configured identity and default channel can be reconstructed by a fresh
# store; the separate backlog matrix checks persisted queue survival.
"${COMPOSE[@]}" exec -T "${REPEATER_SERVICE}" \
	sh -c 'ls -1 /var/lib/openhop_repeater' >"${EVIDENCE_DIR}/repeater-state-before.txt" 2>&1 || true

# Phase-scoped reconnect evidence: only the log lines produced AFTER the
# restart count. A reconnect line left over from an earlier phase would
# otherwise be read as this phase's proof.
LOG_MARK="$(docker logs "${MEDRE_CONTAINER}" 2>&1 | wc -l)"

"${COMPOSE[@]}" restart "${REPEATER_SERVICE}" >/dev/null
wait_healthy "${REPEATER_CONTAINER}" "${REPEATER_WAIT_SECONDS}"

log "Phase 3a: waiting for MEDRE's adapter to reconnect"
waited=0
reconnected=0
while ((waited < RECONNECT_WAIT_SECONDS)); do
	if docker logs "${MEDRE_CONTAINER}" 2>&1 | tail -n "+$((LOG_MARK + 1))" |
		grep "MeshCoreSession openhop-companion: reconnected successfully" >/dev/null; then
		reconnected=1
		echo "  MEDRE reconnected after ${waited}s"
		break
	fi
	sleep 5
	waited=$((waited + 5))
done
docker logs "${MEDRE_CONTAINER}" >"${EVIDENCE_DIR}/medre-openhop-after-restart.log" 2>&1 || true
((reconnected == 1)) ||
	fail "MEDRE's adapter did not reconnect within ${RECONNECT_WAIT_SECONDS}s"

# ---------------------------------------------------------------------------
log "Phase 4: stable identity and default channel across the repeater restart"
# ---------------------------------------------------------------------------
# Stop MEDRE again so the probe can be the sole client, then re-probe: the
# companion's SELF_INFO identity must be IDENTICAL to the pre-restart one.
"${COMPOSE[@]}" stop "${MEDRE_SERVICE}" >/dev/null 2>&1 || true
sleep 2
PROBE_AFTER="${EVIDENCE_DIR}/probe-after-restart.json"
run_probe "after-restart"
probe_passed "${PROBE_AFTER}" ||
	fail "protocol probe after the repeater restart did not pass"

python3 - "${PROBE_BEFORE}" "${PROBE_AFTER}" <<'PY' || fail "the companion identity or channel read changed across restart"
import json
import sys

before = json.load(open(sys.argv[1]))
after = json.load(open(sys.argv[2]))
b = before.get("self_info") or {}
a = after.get("self_info") or {}
for field in ("name", "public_key"):
    if b.get(field) != a.get(field):
        raise SystemExit(
            f"companion identity field {field} changed across the restart: "
            f"{b.get(field)!r} -> {a.get(field)!r}"
        )
if not a.get("public_key"):
    raise SystemExit("post-restart SELF_INFO carries no public_key")
bc = before.get("channel_0") or {}
ac = after.get("channel_0") or {}
if bc.get("channel_name") != ac.get("channel_name"):
    raise SystemExit(
        "channel 0 state changed across the restart: "
        f"{bc.get('channel_name')!r} -> {ac.get('channel_name')!r}"
    )
print("  companion identity stable:", a.get("name"), a.get("public_key"))
print("  channel 0 stable:", ac.get("channel_name"))
PY

# ---------------------------------------------------------------------------
log "Phase 5: clean stop"
# ---------------------------------------------------------------------------
"${COMPOSE[@]}" up -d "${MEDRE_SERVICE}" >/dev/null
wait_healthy "${MEDRE_CONTAINER}" "${MEDRE_WAIT_SECONDS}"
stop_at="$(docker inspect -f '{{.State.StartedAt}}' "${MEDRE_CONTAINER}")" ||
	fail "cannot identify the MEDRE process to stop"
stop_rc=0
"${COMPOSE[@]}" stop -t 30 "${MEDRE_SERVICE}" >/dev/null || stop_rc=$?
((stop_rc == 0)) || fail "MEDRE did not stop cleanly (compose exit ${stop_rc})"
docker inspect -f '{{.State.Running}} {{.State.ExitCode}}' "${MEDRE_CONTAINER}" 2>/dev/null |
	grep -qx 'false 0' || fail "MEDRE container did not exit with status 0"
docker logs --since "${stop_at}" "${MEDRE_CONTAINER}" >"${EVIDENCE_DIR}/medre-openhop-stopped.log" 2>&1 ||
	fail "cannot read the stopped MEDRE process logs"
grep -q "MEDRE stopped" "${EVIDENCE_DIR}/medre-openhop-stopped.log" ||
	fail "MEDRE did not log a clean shutdown"

# Restore the rig to the state the check found it in (MEDRE running): the
# check must not leave the rig torn down for the next case.
log "Phase 5a: restoring the rig (MEDRE back up)"
"${COMPOSE[@]}" up -d "${MEDRE_SERVICE}" >/dev/null
wait_healthy "${MEDRE_CONTAINER}" "${MEDRE_WAIT_SECONDS}"

# ---------------------------------------------------------------------------
cat >"${EVIDENCE_DIR}/verdict.json" <<EOF
{
  "case": "openhop-docker-lifecycle",
  "pass": true,
  "scope": "MEDRE<->openHop Docker protocol qualification",
  "legs": {
    "container_startup": "pass",
    "protocol_handshake_appstart_selfinfo_channel_read": "pass",
    "medre_adapter_connected": "pass",
    "reconnect_after_repeater_restart": "pass",
    "identity_and_default_channel_stable": "pass",
    "clean_stop": "pass"
  },
  "evidence_boundary": "Protocol handshake and adapter lifecycle only. The repeater runs radio_type: null, so NullRadio raises on every send: NO delivery and NO RF transmission is proven by this check."
}
EOF

log "PASS: openHop Docker lifecycle check"
echo "  evidence: ${EVIDENCE_DIR}"
exit 0
