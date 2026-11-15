# =============================================================================
# Shared helpers for the medre interop scenarios (scenario-medre-*.sh).
# Sourced by medre scenarios AFTER lib/common.sh; owns every MEDRE-side
# contract default so late rig-naming differences are one-line fixes.
#
# Contents:
#   - MEDRE_* configuration defaults (env and lab/state/scenario.env win:
#     common.sh sourced those before this file, so only unset values fall
#     back to the defaults below)
#   - require_medre_bridge: SKIP/FAIL gates for the medre rig feature
#   - medre_lib_py / medre_state_py: lab-tools exec wrappers carrying the
#     MEDRE_* env (compose exec does not inherit host env, and common.sh's
#     SCENARIO_ENV_ARGS list does not include the MEDRE_* names)
#   - medre_validate: corpus validation with the medre installed-schema
#     authority (lab-tools first; container fallback when lab-tools lacks
#     the medre package)
#   - start_collector / stop_collector: tagged, exclusive radio collectors
#     (medre_radio.py) mirroring common.sh's sniffer lifecycle discipline
# =============================================================================
# shellcheck shell=bash

# --- MEDRE contract defaults (weakest; environment and scenario.env win) -----
MEDRE_BOT_MXID="${MEDRE_BOT_MXID:-@medre:lab.test}"
# medre bot's radio adapter id as it appears in envelope.source_adapter.
# Empty = do not assert a specific value (corpus validation still applies).
MEDRE_RADIO_ADAPTER="${MEDRE_RADIO_ADAPTER:-}"
# medre-state SQLite path INSIDE lab-tools. Builder contract (compose
# profile medre): medre-state volume at medre HOME /data, storage
# {state}/medre.sqlite -> /state/medre/state/medre.sqlite in lab-tools.
# The builder publishes its path as MEDRE_MAP_DB in lab/state/scenario.env;
# MEDRE_DB (the scenario contract name) wins if both are set.
MEDRE_DB="${MEDRE_DB:-${MEDRE_MAP_DB:-/state/medre/state/medre.sqlite}}"
MEDRE_DB_A="${MEDRE_DB_A:-${MEDRE_DB}}"
MEDRE_DB_B="${MEDRE_DB_B:-${MEDRE_DB}}"
# Compose services that carry the medre bridge (first match wins).
MEDRE_RELAY_SERVICES="${MEDRE_RELAY_SERVICES:-medre-relay-a medre-relay-b medre-relay}"
# Container used as the medre installed-schema authority; empty = auto-detect
# (medre services first, then the mmrelay relays, which pin the coordinated
# medre contract commits in their image).
MEDRE_SCHEMA_CONTAINER="${MEDRE_SCHEMA_CONTAINER:-}"
# Radio UTF-8 byte limit both bridges must respect (mmrelay
# DEFAULT_MESSAGE_TRUNCATE_BYTES == medre MeshtasticConfig.max_text_bytes).
MEDRE_TRUNCATE_BYTES="${MEDRE_TRUNCATE_BYTES:-227}"
# Require the medre installed-schema authority for corpus validation.
MEDRE_CORPUS_REQUIRE_SCHEMA="${MEDRE_CORPUS_REQUIRE_SCHEMA:-1}"
export MEDRE_BOT_MXID MEDRE_RADIO_ADAPTER MEDRE_DB MEDRE_DB_A MEDRE_DB_B \
	MEDRE_RELAY_SERVICES MEDRE_SCHEMA_CONTAINER MEDRE_TRUNCATE_BYTES \
	MEDRE_CORPUS_REQUIRE_SCHEMA

_MEDRE_ENV_ARGS=()
for _medre_var in MEDRE_BOT_MXID MEDRE_RADIO_ADAPTER MEDRE_DB MEDRE_DB_A MEDRE_DB_B \
	MEDRE_RELAY_SERVICES MEDRE_SCHEMA_CONTAINER MEDRE_TRUNCATE_BYTES \
	MEDRE_CORPUS_REQUIRE_SCHEMA; do
	_MEDRE_ENV_ARGS+=("${_medre_var}=${!_medre_var:-}")
done
unset _medre_var

# --- lab-tools exec wrappers carrying the MEDRE_* env -------------------------
# medre_lib_py <script.py> [args...]  (scripts needing MEDRE_* env)
medre_lib_py() {
	local script="$1"
	shift
	tools_exec env "${_MEDRE_ENV_ARGS[@]}" python3 "${SCENARIOS_LIB_DIR}/${script}" "$@"
}

# medre_state_py [args...]  (medre_maps.py shorthand with MEDRE_DB env)
medre_state_py() {
	medre_lib_py medre_maps.py "$@"
}

# --- Rig feature gates --------------------------------------------------------
# require_medre_bridge
#   SKIP when the medre rig feature is entirely absent (no service running
#   and no state DB mounted); FAIL when the feature is HALF-delivered (a
#   service runs but the state DB contract is missing).
require_medre_bridge() {
	local services service db_ok=0 svc_ok=0
	services="${MEDRE_RELAY_SERVICES}"
	for service in ${services}; do
		if compose ps --status running --services 2>/dev/null | grep -Fxq "${service}"; then
			svc_ok=1
			break
		fi
	done
	if tools_exec env "${_MEDRE_ENV_ARGS[@]}" test -f "${MEDRE_DB_A}" >/dev/null 2>&1; then
		db_ok=1
	fi
	if ((svc_ok == 0 && db_ok == 0)); then
		SC_SKIP "medre bridge rig feature absent" \
			"no running service among [${services}] and no medre state DB at ${MEDRE_DB_A}; REQUIREMENT FOR THE RIG: compose profile 'medre' (medre-relay service(s), meshtasticd-*-medre nodes, medre bot in the four lab rooms, mmrelay trusted_relays + medre peer-trust) and the medre-state volume path published as MEDRE_DB in lab/state/scenario.env"
	fi
	if ((svc_ok == 0 && db_ok == 1)); then
		SC_SKIP "medre bridge service is not running (state DB present from a previous bring-up)" \
			"no running service among [${services}]; MEDRE_DB_A=${MEDRE_DB_A} is readable but the live bridge is down - rerun once the rig builder has the medre-relay service up"
	fi
	if ((svc_ok == 1 && db_ok == 0)); then
		SC_FAIL "medre rig half-delivered: bridge service is up but its state DB is unreadable" \
			"services=[${services}] MEDRE_DB_A=${MEDRE_DB_A} (builder must publish the medre-state volume path via lab/state/scenario.env)"
	fi
}

# --- medre installed-schema authority ----------------------------------------
# Pick a running container that has medre importable; empty when none.
medre_schema_container() {
	local candidates=() service
	if [[ -n ${MEDRE_SCHEMA_CONTAINER} ]]; then
		candidates=("${MEDRE_SCHEMA_CONTAINER}")
	else
		read -r -a candidates <<<"${MEDRE_RELAY_SERVICES} mmrelay-a mmrelay-b"
	fi
	for service in "${candidates[@]}"; do
		compose ps --status running --services 2>/dev/null | grep -Fxq "${service}" || continue
		if compose exec -T "${service}" python3 -c "import medre" >/dev/null 2>&1; then
			printf '%s' "${service}"
			return 0
		fi
	done
	return 1
}

# medre_validate EVENT_JSON
#   Validates one event through scenarios/lib/corpus.py: contract checks in
#   lab-tools, upgraded with medre's installed-schema validation (container
#   fallback) when lab-tools lacks the medre package. The fallback container
#   has no /scenarios mount, so the corpus JSON travels inline via
#   --corpus-json. Sets MEDRE_VALIDATION (JSON verdict) and
#   MEDRE_VALIDATION_STATUS (0 ok, 1 violations). Fails the scenario when
#   MEDRE_CORPUS_REQUIRE_SCHEMA=1 and no authority could run.
medre_validate() {
	local event_json="$1" out status authority container fallback_json
	MEDRE_VALIDATION=""
	MEDRE_VALIDATION_STATUS=0
	capture out medre_lib_py corpus.py validate "${event_json}"
	status=${SC_STATUS}
	MEDRE_VALIDATION="${out}"
	authority="$(jq_get "${out}" schema_authority | tr -d '"')"
	if [[ "${authority}" == unavailable* ]]; then
		container="$(medre_schema_container || true)"
		if [[ -n ${container} ]]; then
			capture fallback_json compose exec -T "${container}" python3 - \
				validate "${event_json}" --corpus-json "$(cat "${SCENARIO_DIR}/corpus/canonical-events.json")" \
				<"${LIB_DIR}/corpus.py"
			if [[ ${SC_STATUS} -eq 0 || ${SC_STATUS} -eq 1 ]]; then
				MEDRE_VALIDATION="${fallback_json}"
				status=${SC_STATUS}
			fi
		fi
	fi
	if [[ "${authority}" == unavailable* ]]; then
		authority="$(jq_get "${MEDRE_VALIDATION}" schema_authority | tr -d '"')"
		if [[ "${authority}" == unavailable* && "${MEDRE_CORPUS_REQUIRE_SCHEMA}" == "1" ]]; then
			SC_FAIL "corpus validation ran WITHOUT the medre schema authority" \
				"contract verdict: ${MEDRE_VALIDATION}" $'\n'"no running container with medre installed" \
				"(looked for: ${MEDRE_RELAY_SERVICES} mmrelay-a mmrelay-b)"
		fi
	fi
	MEDRE_VALIDATION_STATUS=${status}
	if ((status != 0)); then
		SC_FAIL "event failed corpus validation (contract/medre-schema)" \
			"verdict: ${MEDRE_VALIDATION}"
	fi
}

# medre_assert_valid EVENT_JSON LABEL
#   medre_validate + PASS line with the matched corpus case as evidence.
medre_assert_valid() {
	local event_json="$1" label="$2"
	medre_validate "${event_json}"
	local case_name summary
	case_name="$(jq_get "${MEDRE_VALIDATION}" case | tr -d '"')"
	summary="$(jq_get "${MEDRE_VALIDATION}" summary)"
	SC_PASS "${label}: corpus-valid (medre schema authority)" \
		"corpus_case=${case_name} schema_authority=$(jq_get "${MEDRE_VALIDATION}" schema_authority | tr -d '"')" $'\n'"summary=${summary}"
}

# --- Collecting radio sniffers (full-population capture) ----------------------
# Same discipline as common.sh sniffers: PEER nodes only, tagged listeners,
# previous generation stopped on arm, container-side process killed on stop.
# start_collector OUT_VAR HOST PORT [--timeout S] [--contains X]
start_collector() {
	local out="$1"
	shift
	local tmp pid old_pid_var old_pid
	old_pid_var="${out}_PID"
	old_pid="${!old_pid_var:-}"
	if [[ -n ${old_pid} ]]; then
		stop_collector "${old_pid}" "${!out:-}"
	fi
	tmp="$(mktemp "${LAB_ROOT}/lab/logs/scenarios/medre-collect-XXXXXX.log")"
	medre_lib_py medre_radio.py collect "$@" --tag "medre-$(basename "${tmp}" .log)" \
		>"${tmp}" 2>&1 &
	pid=$!
	printf -v "${out}" '%s' "${tmp}"
	printf -v "${out}_PID" '%s' "${pid}"
	local deadline=$((SECONDS + 20))
	while ((SECONDS < deadline)); do
		grep -q '"listening"' "${tmp}" && return 0
		kill -0 "${pid}" 2>/dev/null || break
		sleep 0.5
	done
}

# stop_collector PID TMPFILE
stop_collector() {
	local pid="$1" tmp="$2"
	[[ -n ${pid} ]] && kill "${pid}" >/dev/null 2>&1 || true
	wait "${pid}" 2>/dev/null || true
	if [[ -n ${tmp} && -e ${tmp} ]]; then
		# medre_radio.py's cmdline contains "radio.py", so common.sh's
		# /proc walk kills the container-side listener by tag too.
		stop_sniffer_kill_by_tag "medre-$(basename "${tmp}" .log)"
	fi
	rm -f "${tmp}"
}

# collect_finished TMPFILE -> prints the final {"packets": ...} line
collector_result() {
	grep -o '{"packets".*}' "$1" | tail -1
}
