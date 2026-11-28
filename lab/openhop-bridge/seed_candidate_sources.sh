#!/usr/bin/env bash
# =============================================================================
# Seed the openHop-profile source checkouts with the revisions under
# qualification.
#
# WHY THIS EXISTS
# ---------------
# The revisions this lab qualifies live on NAMED BRANCHES in the developer's
# local clones and are NOT on GitHub yet. `medre-lab sources fetch` clones
# from GitHub and checks the pinned SHA out, so it cannot materialise these
# revisions on its own. This script does what fetch() would do for those
# keys:
#
#   * the gitignored sources/<key> checkout is created from the lock's URL
#     when it does not exist yet (no checkout, so the pinned SHA need not be
#     on the remote),
#   * the exact lock revision is fetched from an explicit local repository
#     or bundle, never from whatever the source repository has checked out;
#   * no remote-tracking refs are created for local candidates.
#
# A bundle may be used instead of a local clone: set SEED_BUNDLE_<KEY>
# (hyphens replaced with underscores) to its path. Its advertised branch
# must point at the exact lock revision. The lock is the sole pin authority.
#
# These are QUALIFICATION pins: the evidence produced with them proves the
# revision under qualification, not an upstream release.
#
# Usage: lab/openhop-bridge/seed_candidate_sources.sh [--check]
#   --check  verify only; do not fetch, create or move any checkout.
# Exit: 0 = every pinned checkout is at its pinned SHA.
# =============================================================================
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${LAB_ROOT}"

CHECK_ONLY=0
if [[ ${1:-} == "--check" ]]; then
	CHECK_ONLY=1
fi

# key | local candidate repository
CANDIDATES=(
	"medre-adapter-meshcore|${MEDRE_MESHCORE_LOCAL_REPO:-/home/jeremiah/dev/medre-adapter-meshcore}"
	"openhop-core|${OPENHOP_CORE_LOCAL_REPO:-/home/jeremiah/dev/openhop/openhop_core}"
	"openhop-repeater|${OPENHOP_REPEATER_LOCAL_REPO:-/home/jeremiah/dev/openhop/openhop_repeater}"
	"medre-openhop|${MEDRE_LOCAL_REPO:-/home/jeremiah/dev/medre}"
)

# bundle_override <key>: the bundle file to take the candidate from, if any.
bundle_override() {
	local key="$1" name
	name="SEED_BUNDLE_${key}"
	name="${name//[^A-Za-z0-9_]/_}"
	echo "${!name:-}"
}

if ((CHECK_ONLY)); then
	CANDIDATES+=("medre-adapter-meshtastic-openhop|")
fi

staging=""
# shellcheck disable=SC2329 # Runs only from the EXIT trap.
cleanup_stage() {
	if [[ -n ${staging} ]]; then
		rm -rf -- "${staging}"
	fi
}
trap cleanup_stage EXIT

status=0
for entry in "${CANDIDATES[@]}"; do
	IFS='|' read -r key repo <<<"${entry}"
	# Validate the exact lock pin before touching a checkout.
	lock_entry="$(python3 -c 'import json, re, sys; e = json.load(open("sources.lock.json"))[sys.argv[1]]; assert re.fullmatch("[0-9a-f]{40}", e["ref"]); print(e["url"] + "|" + e["ref"])' "${key}")"
	IFS='|' read -r url sha <<<"${lock_entry}"
	target="sources/${key}"
	bundle="$(bundle_override "${key}")"
	created=0

	if [[ ! -e "${target}/.git" ]]; then
		if ((CHECK_ONLY)); then
			echo "ERROR: ${target} is not a git checkout" >&2
			status=1
			continue
		fi
		if [[ -e ${target} || -L ${target} ]]; then
			echo "ERROR: ${target} exists but is not a checkout" >&2
			exit 1
		fi
		mkdir -p sources
		staging="$(mktemp -d "sources/.${key}.XXXXXX")"
		echo "${key}: preparing ${target} from ${url}"
		destination="${target}"
		target="${staging}/checkout"
		git clone --no-checkout --filter=blob:none "${url}" "${target}"
		created=1
	fi

	# A no-checkout clone has no worktree yet; all other checkouts must be
	# clean even when HEAD equals the pin. Never preserve local edits.
	if [[ "$(git -C "${target}" rev-parse --show-toplevel)" != "$(cd "${target}" && pwd -P)" ]]; then
		echo "ERROR: ${target} is not a checkout root" >&2
		exit 1
	fi
	if ((! created)); then
		dirty="$(git -C "${target}" status --porcelain)"
	else
		dirty=""
	fi
	if [[ -n ${dirty} ]]; then
		echo "ERROR: ${target} has uncommitted changes" >&2
		exit 1
	fi
	head_sha="$(git -C "${target}" rev-parse HEAD 2>/dev/null || true)"
	if ((! created)) && [[ ${head_sha} == "${sha}" ]]; then
		echo "${key}: already at ${sha}"
		continue
	fi
	if ((CHECK_ONLY)); then
		echo "ERROR: ${key} is at ${head_sha:-<none>}, expected ${sha}" >&2
		status=1
		continue
	fi

	if [[ -n ${bundle} ]]; then
		[[ ${bundle} == /* ]] || bundle="${LAB_ROOT}/${bundle}"
		[[ -f ${bundle} ]] || {
			echo "ERROR: bundle ${bundle} for ${key} does not exist" >&2
			status=1
			cleanup_stage
			staging=""
			continue
		}
		echo "${key}: seeding ${sha} from bundle ${bundle}"
		ref="$(git bundle list-heads "${bundle}" | awk -v sha="${sha}" '$1 == sha && $2 ~ /^refs\/heads\// { print $2; exit }')"
		if [[ -z ${ref} ]]; then
			echo "ERROR: bundle does not advertise the pinned commit" >&2
			exit 1
		fi
		git -C "${target}" fetch --no-tags "${bundle}" "${ref}"
	else
		[[ ${repo} == /* ]] || repo="${LAB_ROOT}/${repo}"
		[[ -e "${repo}/.git" ]] || {
			echo "ERROR: local candidate repository ${repo} is missing" >&2
			status=1
			cleanup_stage
			staging=""
			continue
		}
		echo "${key}: seeding exact ${sha} from ${repo}"
		git -C "${repo}" cat-file -e "${sha}^{commit}"
		git -C "${target}" fetch --no-tags "${repo}" "${sha}"
	fi
	if ((created)); then
		git -C "${target}" checkout --detach --force "${sha}"
	else
		git -C "${target}" checkout --detach "${sha}"
	fi
	if [[ "$(git -C "${target}" rev-parse HEAD)" != "${sha}" || -n "$(git -C "${target}" status --porcelain)" ]]; then
		echo "ERROR: ${key} did not land on ${sha}" >&2
		status=1
	elif ((created)); then
		if [[ -e ${destination} || -L ${destination} ]]; then
			echo "ERROR: ${destination} appeared during seeding" >&2
			exit 1
		fi
		mv -T -- "${target}" "${destination}"
	fi
	cleanup_stage
	staging=""
done

exit "${status}"
