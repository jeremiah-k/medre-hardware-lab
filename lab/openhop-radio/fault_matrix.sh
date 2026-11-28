#!/usr/bin/env bash
# =============================================================================
# Fault-injection matrix: the MeshCore receive pump under a lossy link.
#
# WHAT THIS MEASURES
# ------------------
# The MEDRE MeshCore session owns ONE connection-scoped receive pump: it
# wakes on MESSAGES_WAITING and drains GET_MSG until NO_MORE_MSGS.  The
# pump parks the drain when a fetch window elapses with no inbound
# traffic - the node's fetch is a destructive pop, so re-issuing would
# pop again into a link that is not returning what it pops - and probes
# a parked link so a silently dead one is retired through the idle-link
# liveness probe; one answered probe re-arms exactly one fetch for a
# drain the window interrupted.  The matrix measures how a slow, lossy,
# or silently dead companion behaves against that contract, and whether
# any wake is lost or duplicated.
#
# The fault is injected with a TRANSPARENT single-connection TCP splice
# (lab/openhop-radio/openhop_fault_proxy.py) between MEDRE and companion A:
# the splice forwards every byte unchanged and never speaks the companion
# protocol, so it cannot issue a GET_MSG and cannot stack one on another.
# The companion still sees exactly ONE client at a time.
#
# The matrix:
#   latency_under    response delayed BELOW the 2 s fetch timeout
#   latency_over     response delayed ABOVE the 2 s fetch timeout
#   repeated_waiting several MESSAGES_WAITING notifications in a row
#   reconnect        the link recovers and the client reconnects
#   reply_stall      TIME WINDOW: every server->client frame is swallowed
#                    for the window, on every connection inside it
#                    (handshake and liveness-probe replies included);
#                    when the window ends the backlog is delivered
#   mid_fetch_close  TIME WINDOW: the connection is closed the first time
#                    the client puts a request in flight (the next fetch
#                    or probe on a live link, the handshake on a fresh
#                    one) and the close re-arms with every connection for
#                    the rest of the window; afterwards the backlog is
#                    delivered
#   saturation       a deep backlog waits behind a reply-stall window
#                    CONSTRUCTED to cover the batch: the stall is armed
#                    with a far guard deadline and held until one
#                    headroom interval past the last accepted send, so
#                    the batch is accepted inside the window by
#                    construction; probe retirements of the silent link
#                    are designed, nothing may be consumed, and the
#                    backlog is delivered once the window ends
#   reply_stall_drain TIME WINDOW armed at the first delivery observed
#                    while a backlog drain is running: the case does not
#                    observe which frame the stall catches, so it asserts
#                    the bound - at most one message may be consumed (a
#                    run may consume none), recovery follows the spec's
#                    branches (a silence the connection survives resumes
#                    on the same connection within one probe interval of
#                    the window's end; a longer silence retires and
#                    recovery follows the reconnect backoff plus one
#                    gated probe budget), and every other message is
#                    delivered once
#   selective_drop   the per-connection worst case the spec bounds: the
#                    probes are answered, the message frames are
#                    swallowed; the verdict asserts the documented bound
#                    (at most one consumed message per answered liveness
#                    probe beyond the unproven first fetch), accounts
#                    the queue at the observation's end (client stopped,
#                    the store read once after the stop), attributes
#                    every lost message to the connection whose
#                    swallowed frame carried its marker, and
#                    cross-checks the swallowed frames against the lost
#                    rows per connection
#   idle_blackhole   both directions of an up, idle link are blackholed
#                    (no close): the idle-link probe must retire within
#                    its measured window, backoff must continue without
#                    success, and what was queued during the fault must
#                    be delivered once after the link returns
#
# WHAT IT COUNTS
# ---------------
#   reconnect_churn  MEDRE's own "reconnect attempt N" log lines
#   lost_wakes       nonces persisted at the companion that never reached
#                    MEDRE's store
#   duplicate_wakes  nonces that reached MEDRE's store more than once
#   consumed_wakes   nonces in neither store (bounded per the verdict
#                    that judges the case)
#
# Usage: lab/openhop-radio/fault_matrix.sh [--case NAME] [--evidence-dir DIR]
#                                         [--selective-drop-passthrough]
# Exit:  0 = every case that ran passed, 1 = a case failed.
# =============================================================================
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${LAB_ROOT}"

EVIDENCE_DIR="${LAB_ROOT}/lab/logs/openhop-fault"
CASE="all"
PASSTHROUGH=""
while (($# > 0)); do
	case "$1" in
	--case)
		CASE="$2"
		shift 2
		;;
	--evidence-dir)
		EVIDENCE_DIR="$2"
		shift 2
		;;
	--selective-drop-passthrough)
		# The control run: arm the selective-drop link WITHOUT the frame
		# drop, so nothing is lost and the verdict fails on the missing
		# proof by design.
		PASSTHROUGH="--selective-drop-passthrough"
		shift
		;;
	*)
		echo "unknown argument: $1" >&2
		exit 2
		;;
	esac
done
python3 lab/tools/openhop_fault_matrix.py --validate-only --case "${CASE}" ${PASSTHROUGH}
umask 077
mkdir -p "${EVIDENCE_DIR}"

COMPOSE=(docker compose --profile openhop-bridge --profile openhop-fault)
PROXY_CONTAINER="medre-lab-openhop-fault-proxy"
REPEATER_A="medre-lab-openhop-repeater-a"
MEDRE_CONTAINER="medre-lab-medre-openhop-bridge"
STATE_TOOL="lab/tools/openhop_store_snapshot.py"

log() { printf '\n=== %s ===\n' "$*"; }
fail() {
	echo "FAIL: $*" >&2
	exit 1
}

log "Phase 0: preconditions"
docker inspect -f '{{.State.Running}}' "${REPEATER_A}" 2>/dev/null | grep -qx true ||
	fail "${REPEATER_A} is not running (bring the bridge profile up first)"

# The fault proxy splices MEDRE to companion A.  MEDRE's own config points
# at the companion directly, so the splice is applied by pointing a
# dedicated MEDRE instance at the proxy instead: the proxy's upstream is
# the companion.  MEDRE stays the only client of the companion.
log "Phase 1: the splice"
# The splice reads its fault arguments from a file in the state directory,
# and compose creates a missing bind-mount source as root, which would lock
# the host-side tool out. Pre-create BOTH as the invoking user, with an
# empty fault (no fault armed) so the splice's first read succeeds.
mkdir -p lab/state/openhop-fault
printf '{}\n' >lab/state/openhop-fault/fault.json
# Compose owns the SERVICE key (openhop-fault-proxy); the container name it
# creates is PROXY_CONTAINER. Only the service key is valid to `compose up`.
"${COMPOSE[@]}" up -d --no-deps openhop-fault-proxy
docker inspect -f '{{.State.Running}}' "${PROXY_CONTAINER}" 2>/dev/null | grep -qx true ||
	fail "the fault proxy did not start"
echo "  ${PROXY_CONTAINER}: running"

log "Phase 2: the fault matrix"
exec python3 lab/tools/openhop_fault_matrix.py --evidence-dir "${EVIDENCE_DIR}" --case "${CASE}" ${PASSTHROUGH}
