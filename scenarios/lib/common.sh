# =============================================================================
# Shared bash helpers for scenario scripts. Sourced by every scenario-*.sh.
# Provides config loading, lab-tools execution wrappers, assertion helpers
# with printed evidence, and bounded polling.
#
# Contract: a scenario script exits 0 = PASS, 2 = SKIP (neutral), anything
# else = FAIL. Every assertion prints the evidence it checked.
# =============================================================================
# shellcheck shell=bash
# SC_EVIDENCE accepts either stdin or optional arguments.
# shellcheck disable=SC2119,SC2120

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB_ROOT="$(cd "${SCENARIO_DIR}/.." && pwd)"
LIB_DIR="${SCENARIO_DIR}/lib"

# --- Config -----------------------------------------------------------------
# 1) scenarios/config.env (contract defaults), 2) environment, 3) the rig's
# lab/state/scenario.env overrides (sourced last). No config file is written.
_lab_source_config() {
	local file
	for file in "${SCENARIO_DIR}/config.env" "${LAB_ROOT}/lab/state/scenario.env"; do
		if [[ -f ${file} ]]; then
			# shellcheck disable=SC1090
			source "${file}"
		fi
	done
}
_lab_source_config

LAB_COMPOSE_FILE="${LAB_COMPOSE_FILE:-${LAB_ROOT}/compose.yaml}"
SCENARIOS_LIB_DIR="${SCENARIOS_LIB_DIR:-/scenarios/lib}"
export SYNAPSE_URL LAB_SERVER_NAME LAB_RUNTIME_JSON \
	BOT_A_MXID BOT_B_MXID BOT_C_MXID UNTRUSTED_LOCALPART DRIVER_LOCALPART \
	HUMAN_ALICE_LOCALPART HUMAN_BOB_LOCALPART \
	MESHNET_A MESHNET_B MAP_DB_A MAP_DB_B MAP_DB_C \
	PLUGIN_OBS_A PLUGIN_OBS_B PLUGIN_OBS_C \
	NODE_A_HOST NODE_A_PORT NODE_A_PEER_HOST NODE_A_PEER_PORT \
	NODE_B_HOST NODE_B_PORT NODE_B_PEER_HOST NODE_B_PEER_PORT \
	NODE_C_HOST NODE_C_PORT \
	MATRIX_WAIT_SECONDS RADIO_WAIT_SECONDS SNIFF_WAIT_SECONDS \
	ROOM_ALPHA_CH0_ALIAS ROOM_ALPHA_CH2_ALIAS \
	ROOM_BETA_CH0_ALIAS ROOM_BETA_CH2_ALIAS ROOM_OTHER_ALIAS
# Human tokens may be published by the rig under either naming.
export LAB_TOKEN_ALICE LAB_TOKEN_BOB

# --- Logging / verdicts -------------------------------------------------------
SC_TS() { date +%s%3N; }
SC_LOG() { echo "[scenario] $*"; }
SC_EVIDENCE() { # indented evidence block from stdin or args
	if [[ $# -gt 0 ]]; then
		echo "            $*"
	else
		sed 's/^/            /'
	fi
}

SC_PASS() {
	echo "PASS: $1"
	if [[ $# -gt 1 ]]; then
		shift
		printf '%s\n' "$*" | SC_EVIDENCE
	fi
}

SC_FAIL() {
	echo "FAIL: $1" >&2
	if [[ $# -gt 1 ]]; then
		shift
		printf '%s\n' "$*" | SC_EVIDENCE >&2
	fi
	exit 1
}

SC_SKIP() {
	echo "SKIP: $1"
	[[ $# -gt 1 ]] && printf '%s\n' "$2" | SC_EVIDENCE
	exit 2
}

# --- lab-tools execution ------------------------------------------------------
# Runs a command inside lab-tools; $@ after '--' is the argv.
compose() {
	docker compose -f "${LAB_COMPOSE_FILE}" ${LAB_COMPOSE_PROFILES:+${LAB_COMPOSE_PROFILES}} "$@"
}

# docker compose exec does NOT inherit host env, so the scenario contract is
# passed explicitly with -e flags on every exec. Helpers treat empty values
# as unset, so rig-published overrides (lab/state/scenario.env) and contract
# defaults both flow through.
SCENARIO_ENV_VARS=(
	SYNAPSE_URL LAB_SERVER_NAME LAB_RUNTIME_JSON
	BOT_A_MXID BOT_B_MXID BOT_C_MXID UNTRUSTED_LOCALPART DRIVER_LOCALPART
	HUMAN_ALICE_LOCALPART HUMAN_BOB_LOCALPART LAB_TOKEN_ALICE LAB_TOKEN_BOB
	MESHNET_A MESHNET_B
	MAP_DB_A MAP_DB_B MAP_DB_C
	PLUGIN_OBS_A PLUGIN_OBS_B PLUGIN_OBS_C
	NODE_A_HOST NODE_A_PORT NODE_A_PEER_HOST NODE_A_PEER_PORT
	NODE_B_HOST NODE_B_PORT NODE_B_PEER_HOST NODE_B_PEER_PORT
	NODE_C_HOST NODE_C_PORT
	MATRIX_WAIT_SECONDS RADIO_WAIT_SECONDS SNIFF_WAIT_SECONDS
)
SCENARIO_ENV_ARGS=()
for _sc_var in "${SCENARIO_ENV_VARS[@]}"; do
	SCENARIO_ENV_ARGS+=(-e "${_sc_var}=${!_sc_var:-}")
done
unset _sc_var

tools_exec() {
	compose exec -T "${SCENARIO_ENV_ARGS[@]}" lab-tools "$@"
}

# Run one of the scenario helper python scripts inside lab-tools.
# Usage: lib_py <script-name.py> [args...]
lib_py() {
	local script="$1"
	shift
	tools_exec python3 "${SCENARIOS_LIB_DIR}/${script}" "$@"
}

# Preflight: rig must be up and scenarios mounted into lab-tools.
require_rig() {
	if ! docker compose -f "${LAB_COMPOSE_FILE}" ps --status running --services 2>/dev/null | grep -Fxq lab-tools; then
		SC_SKIP "rig not running (lab-tools container is down)" \
			"start the rig with lab/up.sh, then rerun; scenarios never manage containers"
	fi
	if ! tools_exec test -d "${SCENARIOS_LIB_DIR}" >/dev/null 2>&1; then
		SC_SKIP "scenarios/lib is not mounted into lab-tools at ${SCENARIOS_LIB_DIR}" \
			"REQUIREMENT FOR THE RIG: bind-mount ./scenarios into lab-tools (see scenarios/README.md)"
	fi
}

# --- Markers / uid ------------------------------------------------------------
SC_UID() { printf '%s-%s-%s' "$(date +%s)" "$$" "${RANDOM}"; }

# --- Room resolution ------------------------------------------------------------
# Sets ROOM_ALPHA_CH0_ID / ROOM_ALPHA_CH2_ID / ROOM_BETA_CH0_ID /
# ROOM_BETA_CH2_ID / ROOM_OTHER_ID from the rig's runtime.json (alias
# fallbacks from config.env). resolve.py tolerates unresolvable optional
# rooms (empty value).
resolve_rooms() {
	# Alias env values may be "#localpart", "localpart", or "localpart:homeserver";
	# resolve.py looks runtime.json up by BARE localpart and echoes it as the key.
	local _al
	local alias_localparts=()
	for _al in \
		"${ROOM_ALPHA_CH0_ALIAS:-}" \
		"${ROOM_ALPHA_CH2_ALIAS:-}" \
		"${ROOM_BETA_CH0_ALIAS:-}" \
		"${ROOM_BETA_CH2_ALIAS:-}" \
		"${ROOM_OTHER_ALIAS:-}"; do
		[[ -n ${_al} ]] || continue
		_al="${_al#\#}"
		alias_localparts+=("${_al%%:*}")
	done
	unset _al
	local resolved
	if ! resolved=$(lib_py resolve.py "${alias_localparts[@]}" 2>&1); then
		SC_SKIP "cannot resolve rooms from runtime.json" "${resolved}"
	fi
	ROOM_ALPHA_CH0_ID=""
	ROOM_ALPHA_CH2_ID=""
	ROOM_BETA_CH0_ID=""
	ROOM_BETA_CH2_ID=""
	ROOM_OTHER_ID=""
	# Alias env values may be "#localpart", "localpart", or "localpart:homeserver";
	# resolve.py always reports bare localparts. Normalize to the bare localpart.
	local key_a0="${ROOM_ALPHA_CH0_ALIAS#\#}" key_a2="${ROOM_ALPHA_CH2_ALIAS#\#}"
	local key_b0="${ROOM_BETA_CH0_ALIAS#\#}" key_b2="${ROOM_BETA_CH2_ALIAS#\#}"
	local key_ot="${ROOM_OTHER_ALIAS#\#}"
	key_a0="${key_a0%%:*}" key_a2="${key_a2%%:*}" key_b0="${key_b0%%:*}"
	key_b2="${key_b2%%:*}" key_ot="${key_ot%%:*}"
	local line
	while IFS= read -r line; do
		case "${line}" in
		"${key_a0}"=*) ROOM_ALPHA_CH0_ID="${line#*=}" ;;
		"${key_a2}"=*) ROOM_ALPHA_CH2_ID="${line#*=}" ;;
		"${key_b0}"=*) ROOM_BETA_CH0_ID="${line#*=}" ;;
		"${key_b2}"=*) ROOM_BETA_CH2_ID="${line#*=}" ;;
		"${key_ot}"=*) ROOM_OTHER_ID="${line#*=}" ;;
		esac
	done <<<"${resolved}"
	[[ -n ${ROOM_ALPHA_CH0_ID} && -n ${ROOM_BETA_CH0_ID} ]] ||
		SC_SKIP "per-meshnet rooms unresolved (provision the rig first: lab/tools/provision.py)"
}

# --- Peer sniffing ----------------------------------------------------------------
# A node serves ONE api client at a time: a new client force-closes the
# standing one, and the meshtastic transport auto-reconnects (up to 8
# attempts), so a leaked listener will keep evicting whoever holds the
# slot. Sniffers therefore attach ONLY to peer nodes, never run alongside
# an injection on the same node, stop any previous generation on arm, and
# are killed inside lab-tools by tag on stop (a host-side kill of the
# docker-exec client leaves the container-side process alive).
#
# start_sniffer OUT_VAR HOST PORT [--timeout S] [--expect-text X] [--expect-reply-id P]
#   Starts radio.py listen in the background (stdout+stderr to a host temp
#   file), sets ${OUT_VAR} to that file and ${OUT_VAR}_PID to the job pid,
#   and blocks (bounded) until the listener reports readiness.
# stop_sniffer PID TMPFILE
#   Kills the sniffer job AND its container-side listener, then removes the
#   temp file.
start_sniffer() {
	local out="$1"
	shift
	local tmp pid old_pid_var old_pid
	old_pid_var="${out}_PID"
	old_pid="${!old_pid_var:-}"
	if [[ -n ${old_pid} ]]; then
		stop_sniffer "${old_pid}" "${!out:-}"
	fi
	tmp="$(mktemp "${LAB_ROOT}/lab/logs/scenarios/sniff-XXXXXX.log")"
	# shellcheck disable=SC2094 # Child-language syntax or named output, not a host-shell read.
	lib_py radio.py listen "$@" --tag "$(basename "${tmp}" .log)" >"${tmp}" 2>&1 &
	pid=$!
	printf -v "${out}" '%s' "${tmp}"
	printf -v "${out}_PID" '%s' "${pid}"
	# Block until the listener prints its readiness marker (bounded): a
	# relay send right after arming can otherwise slip through the
	# TCP-connect + waitForConfig subscribe window and be missed silently.
	local deadline=$((SECONDS + 20))
	while ((SECONDS < deadline)); do
		grep -q '"listening"' "${tmp}" && return 0
		kill -0 "${pid}" 2>/dev/null || break
		sleep 0.5
	done
}

stop_sniffer_kill_by_tag() { # TAG - kill container-side radio.py listeners
	tools_exec python3 -c '
import os, signal, sys
tag = sys.argv[1]
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    try:
        with open(f"/proc/{entry}/cmdline", "rb") as fh:
            cmdline = fh.read().decode("utf-8", "replace")
    except OSError:
        continue
    if "radio.py" in cmdline and tag in cmdline:
        try:
            os.kill(int(entry), signal.SIGKILL)
        except OSError:
            pass
' "$1" >/dev/null 2>&1 || true
}

stop_sniffer() {
	local pid="$1" tmp="$2"
	[[ -n ${pid} ]] && kill "${pid}" >/dev/null 2>&1 || true
	wait "${pid}" 2>/dev/null || true
	if [[ -n ${tmp} && -e ${tmp} ]]; then
		stop_sniffer_kill_by_tag "$(basename "${tmp}" .log)"
	fi
	rm -f "${tmp}"
}

# --- Bounded wait ---------------------------------------------------------------
# wait_until <timeout-seconds> <command...>: retries the command until it
# exits 0; prints the command's last output on timeout (nonzero exit).
wait_until() {
	local timeout="$1"
	shift
	local deadline=$((SECONDS + timeout))
	local output=""
	while ((SECONDS < deadline)); do
		if output=$("$@" 2>&1); then
			return 0
		fi
		sleep 2
	done
	printf '%s\n' "${output}" >&2
	return 1
}

# Capture a value from a command into a variable without dying under set -e.
# capture VAR command...   (sets VAR and SC_STATUS)
capture() {
	local __var="$1"
	shift
	local __out
	if __out=$("$@" 2>&1); then
		printf -v "${__var}" '%s' "${__out}"
		SC_STATUS=0
	else
		printf -v "${__var}" '%s' "${__out}"
		SC_STATUS=1
	fi
}

# --- JSON field extraction (jq is installed in lab-tools) ----------------------
# jq_get JSON EXPR
jq_get() {
	tools_exec python3 -c "
import json, sys
payload = json.loads(sys.argv[1])
current = payload
for segment in sys.argv[2].split('.'):
    if not segment:
        continue
    if isinstance(current, list):
        current = current[int(segment)]
        continue
    if not isinstance(current, dict) or segment not in current:
        current = None
        break
    current = current[segment]
print('null' if current is None else json.dumps(current, ensure_ascii=False))
" "$1" "$2"
}
