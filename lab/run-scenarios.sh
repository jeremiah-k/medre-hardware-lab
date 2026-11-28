#!/usr/bin/env bash
# =============================================================================
# lab/run-scenarios.sh - cross-instance scenario suite runner.
#
# Usage: lab/run-scenarios.sh [--evidence-dir DIR] [name ...]
#   name      scenario name without the scenario- prefix / .sh suffix
#             (e.g. smoke, reply-correlation). No arguments = run all.
#
# Per-scenario setup/act/assert/teardown live inside each scenario script
# (scenarios/scenario-<name>.sh). The runner executes each one as a separate
# process, tees its output to lab/logs/scenarios/<name>-<ts>.log, records a
# PASS/FAIL/SKIP verdict, and exits nonzero if any scenario FAILED.
#
# The runner NEVER manages containers: it runs against an ALREADY-UP rig
# (lab/up.sh). Scenario scripts may SKIP themselves (exit 2) when an optional
# rig feature they need is absent; skips are reported but do not fail the run.
#
# Evidence fence: when at least one scenario will actually execute, the run
# first stamps a per-run evidence directory (default evidence/scenarios/<ts>,
# or --evidence-dir DIR) with the source-selection record (see
# lab/scenario_evidence.py), then writes one case file per executed scenario
# and a summary. Carry a recorded pass forward with:
#   python3 lab/tools/openhop_case_summary.py --verify-selection <dir> --profile scenarios
# =============================================================================
set -uo pipefail
umask 077

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCENARIO_DIR="${LAB_ROOT}/scenarios"
LOG_DIR="${LAB_ROOT}/lab/logs/scenarios"
RUN_TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p "${LOG_DIR}"

ALL_SCENARIOS=(
	smoke
	reply-correlation
	reaction-exact-key
	untargeted-emote
	untrusted-forged-envelope
	blank-reply-plugin
	cross-room-rejection
	channel-matrix
	human-interactions
	medre-parity
	medre-rendering
	medre-replies
	medre-reactions
	medre-loop-inertness
	medre-untrusted
	normal-fanout
	normal-parity
	normal-replies
	normal-reactions
	normal-lxmf
	normal-untrusted
)

# Scenarios deliberately NOT run (policy decisions recorded with reasons).
declare -A DEFERRED_SCENARIOS=(
	["threenode-same-mesh"]="DEFERRED by owner (2026-11-10): same-mesh multi-relay loop handling is a later work item; scenario kept in scenarios/deferred/"
)

EVIDENCE_DIR_ARG=""
SELECTED=()
while (($#)); do
	case "$1" in
	--evidence-dir)
		if (($# < 2)); then
			echo "ERROR: --evidence-dir requires a directory argument" >&2
			exit 2
		fi
		EVIDENCE_DIR_ARG="$2"
		shift 2
		;;
	--evidence-dir=*)
		EVIDENCE_DIR_ARG="${1#*=}"
		shift
		;;
	*)
		SELECTED+=("$1")
		shift
		;;
	esac
done
if ((${#SELECTED[@]} == 0)); then
	SELECTED=("${ALL_SCENARIOS[@]}")
fi
EVIDENCE_DIR="${LAB_ROOT}/evidence/scenarios/${RUN_TS}"
if [[ -n ${EVIDENCE_DIR_ARG} ]]; then
	EVIDENCE_DIR="${EVIDENCE_DIR_ARG}"
fi

# Normal qualification requires independently recorded full source pins. Missing
# mounts must fail before a scenario can accidentally use the baked distribution.
for name in "${SELECTED[@]}"; do
	if [[ ${name} == normal-* ]]; then
		if [[ -z ${LAB_SOURCE_MANIFEST:-} ]]; then
			echo "ERROR: set LAB_SOURCE_MANIFEST to the exact qualification source map" >&2
			exit 1
		fi
		python3 "${LAB_ROOT}/lab/check_source_mounts.py" "${LAB_SOURCE_MANIFEST}" || exit 1
		break
	fi
done

# --- Evidence fence ----------------------------------------------------------
# Only scenarios that will actually execute bind and write evidence: a
# deferred-only or unknown-only selection runs nothing and records nothing.
# The stamp precedes every case file, so a run cannot begin under content it
# will not be able to name (fail-closed: a bind refusal stops the run).
declare -a RUNNABLE=()
for name in "${SELECTED[@]}"; do
	[[ -n ${DEFERRED_SCENARIOS[${name}]-} ]] && continue
	if [[ ! ${name} =~ ^[a-zA-Z0-9_-]+$ ]]; then
		echo "ERROR: invalid scenario name ${name}" >&2
		exit 2
	fi
	if [[ -f ${SCENARIO_DIR}/scenario-${name}.sh ]]; then
		git -C "${LAB_ROOT}" ls-files --error-unmatch "scenarios/scenario-${name}.sh" >/dev/null || exit 1
		RUNNABLE+=("${name}")
	fi
done
BOUND=0
if ((${#RUNNABLE[@]} > 0)); then
	mkdir -p "${EVIDENCE_DIR}"
	if python3 "${LAB_ROOT}/lab/scenario_evidence.py" --bind "${EVIDENCE_DIR}" >/dev/null; then
		BOUND=1
		echo "Evidence directory: ${EVIDENCE_DIR}"
	else
		echo "ERROR: the scenario run is refused: no source-selection stamp in ${EVIDENCE_DIR}" >&2
		exit 1
	fi
fi

declare -a PASSED=()
declare -a RESULT_NAMES=()
declare -a RESULT_STATUS=()
declare -a RESULT_LOGS=()
FAILED=0
RAN=0

echo "==================================================================="
echo " hardware-lab scenario suite - $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo " rig: already-up containers (this runner never starts/stops them)"
echo "==================================================================="

for name in "${SELECTED[@]}"; do
	log_file="${LOG_DIR}/${name}-${RUN_TS}.log"
	if [[ -n ${DEFERRED_SCENARIOS[${name}]-} ]]; then
		echo ""
		echo "--- scenario: ${name} (deferred) ---"
		echo "SKIP: ${DEFERRED_SCENARIOS[${name}]}"
		RAN=$((RAN + 1))
		RESULT_NAMES+=("${name}")
		RESULT_STATUS+=("DEFERRED")
		RESULT_LOGS+=("-")
		continue
	fi
	script="${SCENARIO_DIR}/scenario-${name}.sh"
	if [[ ! -f ${script} ]]; then
		echo "ERROR: unknown scenario '${name}' (no ${script})"
		FAILED=$((FAILED + 1))
		continue
	fi
	echo ""
	echo "--- scenario: ${name} ---"
	set +e
	"${script}" 2>&1 | tee "${log_file}"
	pipeline_status=("${PIPESTATUS[@]}")
	status=${pipeline_status[0]}
	if ((pipeline_status[1] != 0)); then
		status=1
	fi
	set -e
	case "${status}" in
	0)
		verdict="PASS"
		PASSED+=("${name}")
		;;
	2)
		verdict="SKIP"
		;;
	*)
		verdict="FAIL"
		FAILED=$((FAILED + 1))
		;;
	esac
	RAN=$((RAN + 1))
	RESULT_NAMES+=("${name}")
	RESULT_STATUS+=("${verdict}")
	RESULT_LOGS+=("${log_file}")
	if ((BOUND)); then
		if ! python3 "${LAB_ROOT}/lab/scenario_evidence.py" --record-case "${EVIDENCE_DIR}" \
			"${name}" "${verdict}" "${log_file#"${LAB_ROOT}/"}" >/dev/null; then
			echo "ERROR: could not record the ${name} case file in ${EVIDENCE_DIR}" >&2
			FAILED=$((FAILED + 1))
		fi
	fi
done

if ((BOUND)); then
	if ! python3 "${LAB_ROOT}/lab/scenario_evidence.py" --write-summary "${EVIDENCE_DIR}" >/dev/null; then
		echo "ERROR: could not write the evidence summary in ${EVIDENCE_DIR}" >&2
		FAILED=$((FAILED + 1))
	fi
fi

echo ""
echo "==================================================================="
echo " Scenario summary"
echo "==================================================================="
for index in "${!RESULT_NAMES[@]}"; do
	printf ' %-9s %-32s log: %s\n' \
		"${RESULT_STATUS[${index}]}" \
		"${RESULT_NAMES[${index}]}" \
		"${RESULT_LOGS[${index}]}"
done
echo "-------------------------------------------------------------------"
echo " ${FAILED} failed / ${RAN} scenario(s) executed"
echo "==================================================================="

if ((FAILED > 0)); then
	exit 1
fi
if ((BOUND && ${#PASSED[@]} > 0)); then
	python3 "${LAB_ROOT}/lab/scenario_evidence.py" --complete "${EVIDENCE_DIR}" "${PASSED[@]}" >/dev/null || exit 1
fi
exit 0
