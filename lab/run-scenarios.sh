#!/usr/bin/env bash
# =============================================================================
# lab/run-scenarios.sh - cross-instance scenario suite runner.
#
# Usage: lab/run-scenarios.sh [name ...]
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
# =============================================================================
set -uo pipefail

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
	[threenode-same-mesh]="DEFERRED by owner (2026-11-10): same-mesh multi-relay loop handling is a later work item; scenario kept in scenarios/deferred/"
)

if (($# > 0)); then
	SELECTED=("$@")
else
	SELECTED=("${ALL_SCENARIOS[@]}")
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

declare -a RESULT_NAMES=()
declare -a RESULT_STATUS=()
declare -a RESULT_LOGS=()
FAILED=0
RAN=0

echo "==================================================================="
echo " interop-lab scenario suite - $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
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
	status=${PIPESTATUS[0]}
	set -e
	case "${status}" in
	0)
		verdict="PASS"
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
done

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
exit 0
