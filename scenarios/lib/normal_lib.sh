# =============================================================================
# Shared helpers for the normal-topology scenarios (scenario-normal-*.sh).
# Sourced AFTER lib/common.sh AND lib/medre_lib.sh (it reuses their gates,
# corpus authority, collectors, and sniffer discipline) and specializes every
# MEDRE-NORMAL-side contract default so rig-naming drift is a one-line env
# fix. Order of strength: environment > lab/state/scenario.env (already
# sourced by common.sh) > the defaults below.
#
# Normal-topology rig contract (compose profile `normal`):
#   * mmrelay-a (MeshA) + mmrelay-b (MeshB) map ONLY #lab-bridge (ch0) and
#     #lab-bridge-ch2 (ch2); one shared Matrix surface, no per-meshnet rooms.
#   * medre-normal = ONE medre instance: matrix adapter (@medre:lab.test),
#     meshtastic adapter on NEW MeshC via meshtasticd-c-medre (medre-OWNED -
#     never inject or sniff there; tooling uses meshtasticd-c-peer), and an
#     LXMF loopback adapter.
#   * Trust: each mmrelay's trusted_relays = the peer bot + @medre. The
#     stress (medre-relay radio-a/radio-b) profile is DOWN in normal mode.
#
# Env keys published by the rig in lab/state/scenario.env (all optional; the
# defaults match the contract):
#   MEDRE_NORMAL_SERVICE      compose service of the normal medre instance
#   MEDRE_NORMAL_MAP_DB       medre-normal SQLite path inside lab-tools
#   NORMAL_MMRELAY_SERVICES   the two mmrelay services (space separated)
#   NORMAL_PEER_SERVICES      tooling peer node services (space separated)
#   NODE_C_PEER_HOST/PORT     MeshC tooling peer (inject/sniff surface)
#   NODE_C_MEDRE_HOST         MeshC medre-owned node (documentation only)
#   MESHNET_C                 MeshC meshnet label (empty = unasserted)
#   ROOM_BRIDGE_CH0_ALIAS / ROOM_BRIDGE_CH2_ALIAS
#   MEDRE_NORMAL_LXMF         "1" when the LXMF counterpart is live
#   MEDRE_NORMAL_LXMF_ADAPTER lxmf adapter id in envelopes/refs
#   MEDRE_QUEUE_LOG_A/B       mmrelay queue logs inside lab-tools
# =============================================================================
# shellcheck shell=bash

MEDRE_NORMAL_SERVICE="${MEDRE_NORMAL_SERVICE:-medre-normal}"
MEDRE_NORMAL_DB="${MEDRE_NORMAL_DB:-${MEDRE_NORMAL_MAP_DB:-/state/medre-normal/state/medre.sqlite}}"
MEDRE_NORMAL_BOT_MXID="${MEDRE_NORMAL_BOT_MXID:-@medre:lab.test}"
# medre-normal's MeshC radio adapter id as it appears in envelope
# source_adapter / native ref adapter. Empty = do not assert a value.
MEDRE_NORMAL_RADIO_ADAPTER="${MEDRE_NORMAL_RADIO_ADAPTER:-}"
NORMAL_MMRELAY_SERVICES="${NORMAL_MMRELAY_SERVICES:-mmrelay-a mmrelay-b}"
NORMAL_PEER_SERVICES="${NORMAL_PEER_SERVICES:-meshtasticd-a-peer meshtasticd-b-peer meshtasticd-c-peer}"
NODE_C_MEDRE_HOST="${NODE_C_MEDRE_HOST:-meshtasticd-c-medre}"
# The builder publishes the MeshC tooling peer as NODE_C_INJECT_HOST /
# NODE_C_WITNESS_HOST (lab/state/scenario.env); honor those over the
# scenario-internal NODE_C_PEER_HOST name.
NODE_C_PEER_HOST="${NODE_C_INJECT_HOST:-${NODE_C_WITNESS_HOST:-${NODE_C_PEER_HOST:-meshtasticd-c-peer}}}"
NODE_C_PEER_PORT="${NODE_C_PEER_PORT:-4403}"
MESHNET_C="${MESHNET_C:-}"
ROOM_BRIDGE_CH0_ALIAS="${ROOM_BRIDGE_CH0_ALIAS:-#lab-bridge:lab.test}"
ROOM_BRIDGE_CH2_ALIAS="${ROOM_BRIDGE_CH2_ALIAS:-#lab-bridge-ch2:lab.test}"
MEDRE_NORMAL_LXMF="${MEDRE_NORMAL_LXMF:-}"
MEDRE_NORMAL_LXMF_ADAPTER="${MEDRE_NORMAL_LXMF_ADAPTER:-lxmf0}"
MEDRE_QUEUE_LOG_A="${MEDRE_QUEUE_LOG_A:-/state/mmrelay-a/logs/mmrelay.log}"
MEDRE_QUEUE_LOG_B="${MEDRE_QUEUE_LOG_B:-/state/mmrelay-b/logs/mmrelay.log}"
# Containers owning the queue logs (for generation-bounded snapshots).
MEDRE_QUEUE_CONTAINER_A="${MEDRE_QUEUE_CONTAINER_A:-medre-lab-mmrelay-a}"
MEDRE_QUEUE_CONTAINER_B="${MEDRE_QUEUE_CONTAINER_B:-medre-lab-mmrelay-b}"

# --- Redirect the medre_lib surfaces onto the NORMAL instance -----------------
# The normal scenarios share medre_lib.sh's corpus authority, state reader,
# and collectors, but EVERY medre surface is the medre-normal instance: one
# bot, one state DB, one schema-authority container.
MEDRE_DB="${MEDRE_NORMAL_DB}"
MEDRE_DB_A="${MEDRE_NORMAL_DB}"
MEDRE_DB_B="${MEDRE_NORMAL_DB}"
MEDRE_RELAY_SERVICES="${MEDRE_NORMAL_SERVICE}"
MEDRE_SCHEMA_CONTAINER="${MEDRE_NORMAL_SERVICE}"
export MEDRE_NORMAL_SERVICE MEDRE_NORMAL_DB MEDRE_NORMAL_BOT_MXID \
	MEDRE_NORMAL_RADIO_ADAPTER NORMAL_MMRELAY_SERVICES NORMAL_PEER_SERVICES \
	NODE_C_MEDRE_HOST NODE_C_PEER_HOST NODE_C_PEER_PORT MESHNET_C \
	ROOM_BRIDGE_CH0_ALIAS ROOM_BRIDGE_CH2_ALIAS MEDRE_NORMAL_LXMF \
	MEDRE_NORMAL_LXMF_ADAPTER MEDRE_QUEUE_LOG_A MEDRE_QUEUE_LOG_B \
	MEDRE_QUEUE_CONTAINER_A MEDRE_QUEUE_CONTAINER_B \
	MEDRE_DB MEDRE_DB_A MEDRE_DB_B MEDRE_RELAY_SERVICES MEDRE_SCHEMA_CONTAINER

# medre_lib.sh captured MEDRE_DB / MEDRELAY_SERVICES values into its exec-env
# array at SOURCE time; rebuild that array with the redirected values so
# medre_lib_py / medre_state_py / start_collector carry the NORMAL contract.
_MEDRE_ENV_ARGS=()
for _medre_var in MEDRE_BOT_MXID MEDRE_RADIO_ADAPTER MEDRE_DB MEDRE_DB_A MEDRE_DB_B \
	MEDRE_RELAY_SERVICES MEDRE_SCHEMA_CONTAINER MEDRE_TRUNCATE_BYTES \
	MEDRE_CORPUS_REQUIRE_SCHEMA; do
	_MEDRE_ENV_ARGS+=("${_medre_var}=${!_medre_var:-}")
done
unset _medre_var

# --- Generation-consistency gate ----------------------------------------------
# require_generation_consistency CONTAINER CONFIG [CONTAINER CONFIG...]
#   Every running bridge container's StartedAt must POSTDATE its mounted
#   generated config's mtime. Closes the aborted-bring-up mixed-generation
#   window (config regenerated, relays not restarted) that the 2026-11-11
#   fanout incident ran through: a relay from the old generation keeps the
#   OLD room mapping while lab/state/config already carries the new one, so
#   every verdict it produces is evidence about a topology nobody configured.
#   StartedAt parsing per the generation-bounded queue snapshots (c9acb3c):
#   docker's RFC3339 stamp truncated to whole UTC seconds vs the config
#   mtime's epoch seconds. A pair is skipped when the container is absent or
#   the config does not exist (feature not brought up); a PRESENT pair that
#   fails the ordering is a FAIL, not a skip - the mixed generation poisons
#   every assertion downstream of it.
require_generation_consistency() {
	local container config started started_epoch config_epoch
	local mixed="" evidence=""
	while (($# >= 2)); do
		container="$1"
		config="$2"
		shift 2
		docker inspect --format '{{.State.StartedAt}}' "${container}" >/dev/null 2>&1 || continue
		[[ -f ${config} ]] || continue
		started="$(docker inspect --format '{{.State.StartedAt}}' "${container}" 2>/dev/null | cut -c1-19 | tr 'T' ' ')"
		started_epoch="$(date -u -d "${started}" +%s 2>/dev/null)" || started_epoch=""
		config_epoch="$(stat -c %Y "${config}" 2>/dev/null)" || config_epoch=""
		if [[ -z ${started_epoch} || -z ${config_epoch} ]]; then
			SC_FAIL "generation-consistency gate: cannot compare generation inputs for ${container}" \
				"container=${container} started=${started:-unreadable} config=${config} (mtime unreadable)"
		fi
		if ((config_epoch >= started_epoch)); then
			mixed=1
			evidence+="container=${container} started ${started}Z but ${config} was written $(date -u -d "@${config_epoch}" '+%Y-%m-%dT%H:%M:%SZ') (config is same-age or newer: the process may run stale mapping)"$'\n'
		fi
	done
	if ((mixed == 1)); then
		SC_FAIL "generation-consistency gate FAILED: a running bridge predates its generated config (mixed generation)" \
			"${evidence}" $'\n'"REQUIREMENT FOR THE RIG: regenerate the configs and restart the bridge containers on the same generation (rerun lab/up.sh with the rig's mode flag) before judging any scenario verdict"
	fi
	SC_LOG "generation gate: every running bridge container postdates its generated config"
}

# --- Normal-profile gate --------------------------------------------------------
# SKIP when the normal profile is absent entirely; FAIL when it is
# half-delivered (medre-normal runs but the shared rig pieces are missing);
# SKIP with the contract reason when the STRESS bridge is still up (its
# radio-a/radio-b nodes would pollute the exactly-once fanout evidence).
require_normal_rig() {
	require_rig
	local service db_ok=0 svc_ok=0
	for service in ${NORMAL_PEER_SERVICES} ${NORMAL_MMRELAY_SERVICES}; do
		if ! compose ps --status running --services 2>/dev/null | grep -Fxq "${service}"; then
			SC_SKIP "normal-profile rig incomplete: ${service} is not running" \
				"running services: $(compose ps --status running --services 2>/dev/null | tr '\n' ' ')" $'\n'"REQUIREMENT FOR THE RIG: compose profile 'normal' (mmrelay-a/b on MeshA/B mapping #lab-bridge rooms, medre-normal on MeshC, the three peer tooling nodes)"
		fi
	done
	if compose ps --status running --services 2>/dev/null | grep -Fxq "${MEDRE_NORMAL_SERVICE}"; then
		svc_ok=1
	fi
	if tools_exec env "${_MEDRE_ENV_ARGS[@]}" test -f "${MEDRE_NORMAL_DB}" >/dev/null 2>&1; then
		db_ok=1
	fi
	if ((svc_ok == 0 && db_ok == 0)); then
		SC_SKIP "normal-profile medre instance absent" \
			"no running service ${MEDRE_NORMAL_SERVICE} and no state DB at ${MEDRE_NORMAL_DB}; REQUIREMENT FOR THE RIG: compose profile 'normal' with the medre-normal state path published (MEDRE_NORMAL_MAP_DB in lab/state/scenario.env)"
	fi
	if ((svc_ok == 0 && db_ok == 1)); then
		SC_SKIP "medre-normal is not running (state DB present from a previous bring-up)" \
			"MEDRE_NORMAL_DB=${MEDRE_NORMAL_DB} is readable but the live instance is down - rerun once the rig builder has ${MEDRE_NORMAL_SERVICE} up"
	fi
	if ((svc_ok == 1 && db_ok == 0)); then
		SC_FAIL "normal rig half-delivered: ${MEDRE_NORMAL_SERVICE} runs but its state DB is unreadable" \
			"MEDRE_NORMAL_DB=${MEDRE_NORMAL_DB} (builder must publish the medre-normal-state volume path via MEDRE_NORMAL_MAP_DB in lab/state/scenario.env)"
	fi
	if compose ps --status running --services 2>/dev/null | grep -Fxq "medre-relay"; then
		SC_SKIP "stress medre bridge (medre-relay) is still running" \
			"the normal-topology contract requires the stress profile DOWN: its radio-a/radio-b nodes share MeshA/MeshB and would pollute the exactly-once fanout evidence; bring the stress profile down and rerun"
	fi
	require_generation_consistency \
		"${MEDRE_QUEUE_CONTAINER_A}" "${LAB_ROOT}/lab/state/config/mmrelay-a.yaml" \
		"${MEDRE_QUEUE_CONTAINER_B}" "${LAB_ROOT}/lab/state/config/mmrelay-b.yaml" \
		"medre-lab-${MEDRE_NORMAL_SERVICE}" "${LAB_ROOT}/lab/state/config/medre-normal.yaml"
	SC_LOG "normal rig verified: ${MEDRE_NORMAL_SERVICE} up, state DB=${MEDRE_NORMAL_DB}, peers+relays up, generation consistent"
}

# --- Room resolution (bridge rooms) ----------------------------------------------
# Sets ROOM_BRIDGE_CH0_ID (required), ROOM_BRIDGE_CH2_ID and ROOM_OTHER_ID
# (optional; empty when unresolvable - per-variant SKIPs handle them).
resolve_bridge_rooms() {
	local _al
	local alias_localparts=()
	for _al in "${ROOM_BRIDGE_CH0_ALIAS:-}" "${ROOM_BRIDGE_CH2_ALIAS:-}" "${ROOM_OTHER_ALIAS:-}"; do
		[[ -n ${_al} ]] || continue
		_al="${_al#\#}"
		alias_localparts+=("${_al%%:*}")
	done
	unset _al
	local resolved
	if ! resolved=$(lib_py resolve.py "${alias_localparts[@]}" 2>&1); then
		SC_SKIP "cannot resolve rooms from runtime.json" "${resolved}"
	fi
	local key_ch0="${ROOM_BRIDGE_CH0_ALIAS#\#}" key_ch2="${ROOM_BRIDGE_CH2_ALIAS#\#}" key_ot="${ROOM_OTHER_ALIAS#\#}"
	key_ch0="${key_ch0%%:*}" key_ch2="${key_ch2%%:*}" key_ot="${key_ot%%:*}"
	ROOM_BRIDGE_CH0_ID=""
	ROOM_BRIDGE_CH2_ID=""
	ROOM_OTHER_ID=""
	local line
	while IFS= read -r line; do
		case "${line}" in
		"${key_ch0}"=*) ROOM_BRIDGE_CH0_ID="${line#*=}" ;;
		"${key_ch2}"=*) ROOM_BRIDGE_CH2_ID="${line#*=}" ;;
		"${key_ot}"=*) ROOM_OTHER_ID="${line#*=}" ;;
		esac
	done <<<"${resolved}"
	[[ -n ${ROOM_BRIDGE_CH0_ID} ]] ||
		SC_SKIP "#lab-bridge unresolved (the normal profile must provision and alias it)" \
			"resolution output: ${resolved}" $'\n'"publish ROOM_BRIDGE_CH0_ALIAS in lab/state/scenario.env if the rig names the room differently"
	[[ -n ${ROOM_BRIDGE_CH2_ID} ]] ||
		SC_LOG "NOTE: #lab-bridge-ch2 unresolved - the ch2 variants will SKIP (publish ROOM_BRIDGE_CH2_ALIAS)"
	[[ -n ${ROOM_OTHER_ID} ]] ||
		SC_LOG "NOTE: unmapped room unresolved - scenarios using it will SKIP (publish ROOM_OTHER_ALIAS)"
}

# --- Queue-log snapshot -------------------------------------------------------------
# queue_snapshot VAR LOG_PATH CONTAINER: captures {"count","max_pos"} of the
# relay's "Queued message (N/500)" lines from the CURRENT container
# generation only. The state volume's log file spans container generations
# (it is never truncated on recreation), so an unbounded read attributes a
# dead generation's cascade backlog (max_pos 74+ in one observed case) to
# the live relay. The cutoff is the container's StartedAt, matched against
# each line's leading "YYYY-MM-DD HH:MM:SS +0000" timestamp.
queue_snapshot() {
	local __var="$1" log="$2" container="$3" out cutoff
	if tools_exec test -f "${log}" >/dev/null 2>&1; then
		cutoff="$(docker inspect --format '{{.State.StartedAt}}' "${container}" 2>/dev/null |
			cut -c1-19 | tr 'T' ' ')"
		if [[ -n ${cutoff} ]]; then
			# Every tools_exec stage is || true'd: under pipefail a failing
			# exec/awk stage (f5d9424's class) or an empty grep match would
			# kill the whole snapshot; a zero in-generation count is a valid
			# result and the last stage (queue-parse) never fails on it.
			# shellcheck disable=SC2016 # Child-language syntax or named output, not a host-shell read.
			out="$({ tools_exec awk -v cutoff="${cutoff}" '$1 " " $2 >= cutoff' "${log}" 2>/dev/null || true; } |
				{ tools_exec grep "Queued message (" 2>/dev/null || true; } |
				medre_lib_py normal_checks.py queue-parse)"
		else
			out="$({ tools_exec grep "Queued message (" "${log}" 2>/dev/null || true; } |
				medre_lib_py normal_checks.py queue-parse)"
		fi
	else
		out='{"count": 0, "max_pos": 0}'
	fi
	printf -v "${__var}" '%s' "${out}"
}

# queue_assert_flat LABEL BEFORE_A BEFORE_B AFTER_A AFTER_B MAX_NEW MAX_POS
#   The fanout loop signature on the mmrelay send queues: no cascade growth,
#   no backlog depth.
queue_assert_flat() {
	local label="$1" before_a="$2" before_b="$3" after_a="$4" after_b="$5"
	local max_new="$6" max_pos="$7"
	local delta_a delta_b pos_a pos_b
	delta_a=$(($(jq_get "${after_a}" count | tr -d '"') - $(jq_get "${before_a}" count | tr -d '"')))
	delta_b=$(($(jq_get "${after_b}" count | tr -d '"') - $(jq_get "${before_b}" count | tr -d '"')))
	pos_a="$(jq_get "${after_a}" max_pos | tr -d '"')"
	pos_b="$(jq_get "${after_b}" max_pos | tr -d '"')"
	if ((delta_a > max_new || delta_b > max_new)); then
		SC_FAIL "${label}: mmrelay send queues grew beyond the direct-bridge tolerance (cascade signature)" \
			"relay-a queued+${delta_a} (max_pos=${pos_a}), relay-b queued+${delta_b} (max_pos=${pos_b})" $'\n'"before_a=${before_a} after_a=${after_a}" $'\n'"before_b=${before_b} after_b=${after_b}"
	fi
	if ((pos_a > max_pos || pos_b > max_pos)); then
		SC_FAIL "${label}: mmrelay send-queue backlog (depth ${pos_a}/${pos_b} exceeds ${max_pos})" \
			"after_a=${after_a} after_b=${after_b}"
	fi
	SC_PASS "${label}: send queues flat within tolerance" \
		"relay-a queued+${delta_a} max_pos=${pos_a}; relay-b queued+${delta_b} max_pos=${pos_b}"
}

# --- Map-table snapshot ---------------------------------------------------------
# table_snapshot VAR: one line summarizing every state table the normal
# scenarios watch, for the settle-window flatness assertions.
table_snapshot() {
	local __var="$1" a b mce
	capture a lib_py maps.py count "${MAP_DB_A}"
	capture b lib_py maps.py count "${MAP_DB_B}"
	capture mce medre_state_py count "${MEDRE_NORMAL_DB}"
	printf -v "${__var}" '%s' \
		"A=$(jq_get "${a}" count | tr -d '"')" \
		" B=$(jq_get "${b}" count | tr -d '"')" \
		" MEDRE_EVENTS=$(jq_get "${mce}" canonical_events | tr -d '"')" \
		" MEDRE_REFS=$(jq_get "${mce}" native_message_refs | tr -d '"')"
}

# table_assert_flat LABEL BEFORE AFTER
table_assert_flat() {
	local label="$1" before="$2" after="$3"
	[[ ${before} == "${after}" ]] ||
		SC_FAIL "${label}: state tables grew across the settle window (re-bridge cascade)" \
			"before: ${before}" $'\n'"after: ${after}"
	SC_PASS "${label}: all map/state tables flat across the settle window" \
		"before: ${before}" $'\n'"after: ${after}"
}

# --- Mesh metadata -----------------------------------------------------------------
# peer_node_id VAR HOST PORT: "!hex" node id of a tooling peer node (for
# sender-attribution assertions). Call BEFORE arming sniffers on that node.
peer_node_id() {
	local __var="$1" host="$2" port="$3" out
	capture out medre_lib_py medre_radio.py info "${host}" "${port}"
	((SC_STATUS == 0)) || SC_FAIL "cannot read peer node identity (${host}:${port})" "${out}"
	printf -v "${__var}" '%s' "$(jq_get "${out}" node_id | tr -d '"')"
	[[ ${!__var} =~ ^![0-9a-f]{8}$ ]] ||
		SC_FAIL "peer node identity unusable (${host}:${port})" "${out}"
}

# --- Checked arming (lab-infra transient retry) -------------------------------------
# start_collector_checked OUT_VAR HOST PORT [args...]
# start_sniffer_checked OUT_VAR HOST PORT [args...]
#   Arm via the shared helpers, then VERIFY the process is alive and reported
#   readiness; retry ONCE on a failed arm. Observed live: docker's embedded
#   resolver blipped ("gaierror: Temporary failure in name resolution") at a
#   re-arm seconds after a previous collector generation stopped, and rapid
#   node-API churn killed another arm. A dead arm must not masquerade as a
#   silent empty window.
#
#   Indirection discipline (2026-11-11 review): OUT_VAR holds a TMPFILE path
#   and OUT_VAR_PID the arm's host-side pid, so the pid needs a SECOND level
#   of indirection (${!out}_PID would test a filename). The RETRY arm is
#   verified too: a dead retry must FAIL the scenario, not fall through to
#   an unverified window.
_arm_is_ready() { # TMPFILE PID_VAR_NAME -> 0 when alive and reported ready
	local tmp="$1" pid_var="$2" pid
	pid="${!pid_var:-}"
	[[ -n ${pid} ]] && kill -0 "${pid}" 2>/dev/null && grep -q '"listening"' "${tmp}" 2>/dev/null
}

start_collector_checked() {
	local out="$1" tmp pid_var
	shift
	start_collector "${out}" "$@"
	tmp="${!out}"
	pid_var="${out}_PID"
	if _arm_is_ready "${tmp}" "${pid_var}"; then
		return 0
	fi
	stop_collector "${!pid_var:-}" "${tmp:-}" 2>/dev/null || true
	SC_LOG "collector arm failed ($(head -c 160 "${tmp:-/dev/null}" 2>/dev/null | tr '\n' ' ')); retrying once"
	start_collector "${out}" "$@"
	tmp="${!out}"
	if ! _arm_is_ready "${tmp}" "${pid_var}"; then
		SC_FAIL "collector arm failed twice (${out}): the window would be unverified" \
			"retry arm is dead or never reported readiness; last output: $(head -c 160 "${tmp:-/dev/null}" 2>/dev/null | tr '\n' ' ')"
	fi
}

start_sniffer_checked() {
	local out="$1" tmp pid_var
	shift
	start_sniffer "${out}" "$@"
	tmp="${!out}"
	pid_var="${out}_PID"
	if _arm_is_ready "${tmp}" "${pid_var}"; then
		return 0
	fi
	stop_sniffer "${!pid_var:-}" "${tmp:-}" 2>/dev/null || true
	SC_LOG "sniffer arm failed ($(head -c 160 "${tmp:-/dev/null}" 2>/dev/null | tr '\n' ' ')); retrying once"
	start_sniffer "${out}" "$@"
	tmp="${!out}"
	if ! _arm_is_ready "${tmp}" "${pid_var}"; then
		SC_FAIL "sniffer arm failed twice (${out}): the window would be unverified" \
			"retry arm is dead or never reported readiness; last output: $(head -c 160 "${tmp:-/dev/null}" 2>/dev/null | tr '\n' ' ')"
	fi
}

# --- Radio-ingress readiness gate ---------------------------------------------------
# radio_ingest_ready HOST PORT OWNER_BOT ROOM_ID LABEL
#   Deterministic readiness gate for the radio->Matrix direction: inject a
#   probe text and wait for the owner bridge's canonical event. mmrelay
#   DROPS inbound radio packets during its startup drain window ("Dropping
#   inbound packet during startup drain window") by design, so a probe that
#   never yields an event means the bridge is not yet accepting radio
#   traffic - SKIP with the steady-state requirement instead of failing.
radio_ingest_ready() {
	local host="$1" port="$2" owner_bot="$3" room_id="$4" label="$5"
	local probe since out
	probe="NRDY-${label}-$(SC_UID)"
	since="$(date +%s%3N)"
	capture out lib_py radio.py send-text "${host}" "${port}" "${probe}"
	((SC_STATUS == 0)) || SC_SKIP "cannot inject the ${label} readiness probe" "${out}"
	capture out lib_py mx_events.py wait "${room_id}" \
		--sender "${owner_bot}" --body-contains "${probe}" \
		--require-envelope --since-ms "${since}" --timeout 60
	# capture() as a FUNCTION always returns 0 (its last command is an
	# assignment), so the verdict MUST come from SC_STATUS - an
	# `if capture ...; then` would treat a timeout as success.
	if ((SC_STATUS == 0)); then
		SC_LOG "[${label}] radio ingress ready (probe event $(jq_get "${out}" event.event_id | tr -d '"'))"
		return 0
	fi
	SC_SKIP "${label} bridge is not accepting radio packets yet (startup drain drops them by design)" \
		"probe=${probe} produced no canonical event within 60s" $'\n'"REQUIREMENT: steady-state rig (services up and past their startup drain window, ~90s after start) - the suite tests steady-state interop, not the drain semantics"
}
