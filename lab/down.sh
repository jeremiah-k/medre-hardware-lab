#!/usr/bin/env bash
# =============================================================================
# Stop the interop lab and guarantee a COMPLETE teardown. Standing rule (owner
# directive 2026-11-19): the rig comes fully down after every run — no running
# processes left.
#
# Plain `docker compose down` is not sufficient: a container that ignores
# SIGTERM (e.g. meshtasticd) past the stop timeout makes compose abort mid-down
# ("Network ... still in use") and leak containers/networks behind a zero exit
# code. So this script:
#   1. downs compose with a short timeout so docker escalates TERM->KILL;
#   2. force-removes any surviving project containers (bounded retry loop);
#   3. removes leftover project networks the same way;
#   4. verifies honestly: success is printed only when `docker ps -a` shows
#      zero project containers AND `docker network ls` zero project networks;
#      otherwise what remains is listed and the exit is nonzero.
# State volumes are KEPT (bring-up is resumable). To wipe state for a clean
# bring-up, use lab/reset.sh instead.
# =============================================================================
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${LAB_ROOT}"

# Compose project name (`name:` in compose.yaml); container_name: values and
# project networks (named "medre-lab-*" plus the default "medre-lab_default")
# all share this prefix. Volumes share it too but are deliberately untouched.
PROJECT_PREFIX="medre-lab"
SWEEP_ATTEMPTS=3

log() { printf '\n=== %s ===\n' "$*"; }

# Project containers (any state) / networks by name prefix. If docker itself
# fails, the function fails so `set -e` stops the script instead of letting an
# empty listing masquerade as a clean rig.
project_containers() {
	local names
	names="$(docker ps -a --format '{{.Names}}')" || return 1
	printf '%s\n' "${names}" | grep "^${PROJECT_PREFIX}" || true
}

project_networks() {
	local names
	names="$(docker network ls --format '{{.Name}}')" || return 1
	printf '%s\n' "${names}" | grep "^${PROJECT_PREFIX}" || true
}

log "Stopping lab (compose down; 5s stop timeout, then SIGKILL)"
# Compose may still abort on a stuck resource ("Network ... still in use") or
# on unprovisioned interpolation env; tolerate any failure here — the prefix
# sweep + verification below decide the outcome and never depend on compose.
compose_rc=0
docker compose down --remove-orphans --timeout 5 || compose_rc=$?
if ((compose_rc != 0)); then
	log "compose down exited ${compose_rc}; forcing the remaining teardown"
fi

# Sweep survivors: force-remove project containers until none are listed.
# `docker rm -f` is synchronous, so each pass either clears the rig or leaves
# something the final verification will report. Names never contain spaces.
for _ in $(seq "${SWEEP_ATTEMPTS}"); do
	survivors="$(project_containers)"
	if [ -z "${survivors}" ]; then break; fi
	log "Force-removing surviving containers"
	printf '%s\n' "${survivors}"
	printf '%s\n' "${survivors}" | xargs -r docker rm -f >/dev/null || true
	sleep 1
done

# Sweep leftover networks (containers are gone, so compose/lab networks should
# detach; an external holder keeps them alive and gets reported at the end).
for _ in $(seq "${SWEEP_ATTEMPTS}"); do
	leftover="$(project_networks)"
	if [ -z "${leftover}" ]; then break; fi
	log "Removing leftover lab networks"
	printf '%s\n' "${leftover}"
	printf '%s\n' "${leftover}" | xargs -r docker network rm >/dev/null || true
	sleep 1
done

# Honest verification: the success line is only printed for a fully down rig.
remaining_containers="$(project_containers)"
remaining_networks="$(project_networks)"
if [ -n "${remaining_containers}" ] || [ -n "${remaining_networks}" ]; then
	echo "ERROR: lab teardown incomplete; rig is NOT fully down:" >&2
	if [ -n "${remaining_containers}" ]; then
		echo "Remaining containers:" >&2
		printf '%s\n' "${remaining_containers}" >&2
	fi
	if [ -n "${remaining_networks}" ]; then
		echo "Remaining networks:" >&2
		printf '%s\n' "${remaining_networks}" >&2
	fi
	exit 1
fi

echo "Lab stopped and verified down (0 project containers, 0 project networks). Volumes kept; run lab/up.sh to resume, lab/reset.sh to wipe."
