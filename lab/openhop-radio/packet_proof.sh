#!/usr/bin/env bash
# =============================================================================
# openHop VIRTUAL RADIO packet proof.
#
# WHAT THIS PROVES
# ----------------
# That a REAL MeshCore packet crosses between two independent openHop
# repeaters over the lab's virtual radio medium:
#
#   1. a client on companion B sends a unique channel-0 nonce;
#   2. repeater B encrypts it with its channel-0 secret and transmits it;
#   3. the lab relay (openHop Modem protocol over TCP) carries it to
#      repeater A;
#   4. repeater A decrypts it and delivers it to the client on companion A;
#   5. exactly ONE CHANNEL_MSG_RECV arrives at A with the exact text, and
#      NOTHING echoes back to B.
#
# WHAT IT DOES NOT PROVE
# ----------------------
# Anything about physical RF. The relay simulates the MEDIUM (the RF hop);
# the packets are real, the radio PHY, the spectrum, the range and the
# antenna are not. See docs/openhop-radio-qualification.md.
#
# The proof runs inside the lab network (no host ports): it is a one-shot
# container of the medre-openhop image, which carries the PINNED upstream
# MeshCore SDK (meshcore==2.3.15).
#
# Usage: lab/openhop-radio/packet_proof.sh [--evidence-dir DIR]
# Exit:  0 = PASS, 1 = FAIL.
# =============================================================================
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${LAB_ROOT}"

EVIDENCE_DIR="${LAB_ROOT}/lab/logs/openhop-radio"
while (($# > 0)); do
	case "$1" in
	--evidence-dir)
		EVIDENCE_DIR="$2"
		shift 2
		;;
	*)
		echo "Unknown option: $1" >&2
		exit 2
		;;
	esac
done
mkdir -p "${EVIDENCE_DIR}"

COMPOSE=(docker compose --profile openhop-radio)
RELAY_CONTAINER="medre-lab-openhop-radio-relay"
REPEATER_A="medre-lab-openhop-repeater-a"
REPEATER_B="medre-lab-openhop-repeater-b"

log() { printf '\n=== %s ===\n' "$*"; }
fail() {
	echo "FAIL: $*" >&2
	exit 1
}

require_running() {
	local container=$1
	docker inspect -f '{{.State.Running}}' "${container}" 2>/dev/null |
		grep -qx true ||
		fail "${container} is not running (bring the profile up first)"
}

log "Phase 0: preconditions"
require_running "${RELAY_CONTAINER}"
require_running "${REPEATER_A}"
require_running "${REPEATER_B}"

# The proof's subscriber must be companion A's ONLY client: the openHop
# companion server accepts one client at a time and evicts the previous
# one. A MEDRE runtime evicted this way treats it as an ordinary link
# loss, reconnects within seconds, and re-evicts the subscriber, so the
# proof would measure zero deliveries. Refuse BEFORE connecting instead
# of disturbing a running rig: name the container that holds the slot.
# The documented run order runs this proof on its own rig
# (lab/up.sh --openhop-radio), where no MEDRE exists yet, so the refusal
# never fires on the documented path.
for holder in medre-lab-medre-openhop-bridge medre-lab-medre-openhop-fault; do
	if docker inspect -f '{{.State.Running}}' "${holder}" 2>/dev/null | grep -qx true; then
		fail "companion A's single client slot is held by the running container ${holder}: the openHop companion accepts ONE client at a time and evicts the previous one (one client per companion), so this proof's subscriber would evict MEDRE and MEDRE's reconnect would evict it right back. Stop ${holder} first, or run this proof on its own rig (lab/up.sh --openhop-radio)."
	fi
done

# The relay logs every transmission it forwards and keeps running across
# proofs, so the crossing count is measured as a DELTA over the proof
# window: the log line count before the proof is the baseline.
RELAY_LOG="${EVIDENCE_DIR}/relay.log"
docker logs "${RELAY_CONTAINER}" >"${RELAY_LOG}" 2>&1 || true
RELAY_BASELINE="$(grep -c "transmitted" "${RELAY_LOG}" || true)"
echo "  relay transmission baseline: ${RELAY_BASELINE}"

# Repeater A's reception count is measured the same way: a baseline taken
# BEFORE the proof, so the delta covers the proof window only.
A_LOG="${EVIDENCE_DIR}/repeater-a.log"
docker logs "${REPEATER_A}" >"${A_LOG}" 2>&1 || true
A_RX_BASELINE="$(grep -c "RX GRP_TXT" "${A_LOG}" || true)"
echo "  repeater A reception baseline: ${A_RX_BASELINE}"

log "Phase 1: packet proof (channel-0 nonce B -> A)"
PROOF_JSON="${EVIDENCE_DIR}/packet-proof.json"
"${COMPOSE[@]}" run --rm --no-deps --name medre-lab-openhop-radio-probe-run \
	openhop-radio-probe >"${PROOF_JSON}" 2>"${PROOF_JSON}.err" ||
	{
		echo "  the proof container failed:" >&2
		cat "${PROOF_JSON}.err" >&2 || true
		fail "the packet proof container failed"
	}

cat "${PROOF_JSON}"
python3 - "${PROOF_JSON}" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
if not data.get("pass"):
    raise SystemExit(f"the packet proof did not pass: {data.get('reason')!r}")
if data.get("delivered_count_at_a") != 1:
    raise SystemExit(
        f"expected exactly one delivery at A, got {data.get('delivered_count_at_a')!r}"
    )
if data.get("echo_count_at_b"):
    raise SystemExit(f"the packet echoed back to B: {data.get('echo_count_at_b')!r}")
# The client-level duplicate count is a REGRESSION counter on the candidate
# pins: openHop's companion could hand the same queued message to a client
# twice (in-memory queue AND SQLite at the same time). That race is pinned
# shut in the candidate sources this profile runs on, so a duplicate is a
# REGRESSION and fails the proof instead of being reported and hidden.
if data.get("duplicate_client_events_at_a"):
    raise SystemExit(
        "the client received the same message more than once: "
        f"{data.get('duplicate_client_events_at_a')!r}"
    )
print("  packet proof PASSED")
PY

log "Phase 2: capturing relay and repeater logs"
docker logs "${RELAY_CONTAINER}" >"${EVIDENCE_DIR}/relay.log" 2>&1 || true
docker logs "${REPEATER_A}" >"${EVIDENCE_DIR}/repeater-a.log" 2>&1 || true
docker logs "${REPEATER_B}" >"${EVIDENCE_DIR}/repeater-b.log" 2>&1 || true

log "Phase 3: exactly ONE packet crossed the medium (relay transmission log)"
# The relay logs every transmission it forwards. The proof's nonce crossed
# once: the radio path must carry one packet, not two.
CROSSINGS="$(grep -c "transmitted" "${RELAY_LOG}" || true)"
CROSSINGS=$((CROSSINGS - RELAY_BASELINE))
echo "  relay transmissions: ${CROSSINGS}"
if [[ ${CROSSINGS} != "1" ]]; then
	fail "expected exactly one packet to cross the virtual radio, got ${CROSSINGS}"
fi
# Repeater A must have RECEIVED the packet once. The single relay
# transmission above also proves A did not repeat it: a repeat would have
# crossed the medium a second time (mode: monitor suppresses it) and would
# have been echoed back to B.
A_RX_COUNT="$(grep -c "RX GRP_TXT" "${A_LOG}" || true)"
A_RX_COUNT=$((A_RX_COUNT - A_RX_BASELINE))
echo "  repeater A receptions: ${A_RX_COUNT}"
if [[ ${A_RX_COUNT} != "1" ]]; then
	fail "expected exactly one packet to be received by repeater A, got ${A_RX_COUNT}"
fi

cat >"${EVIDENCE_DIR}/verdict.json" <<EOF
{
  "case": "openhop-virtual-radio-packet-path",
  "pass": true,
  "scope": "MEDRE<->openHop virtual radio packet path",
  "legs": {
    "repeater_b_tx_to_relay": "pass",
    "relay_to_repeater_a_rx": "pass",
    "exactly_one_packet_crossed_the_medium": "pass",
    "client_level_duplicate_events_at_a": "asserted 0 (the duplicate-delivery race is pinned shut in the candidate sources this profile runs on; a nonzero value fails the proof)",
    "exactly_one_channel_msg_recv_at_a": "pass",
    "exact_text_match": "pass",
    "no_echo_at_b": "pass"
  },
  "evidence_boundary": "Real MeshCore packets encrypted by one openHop repeater and decrypted by the other. The RF MEDIUM (the LoRa PHY, the spectrum, the range) is SIMULATED by the lab relay. This is NOT physical RF delivery evidence, and NO MEDRE delivery is claimed (the MEDRE bridge route is Phase 3B)."
}
EOF

log "PASS: openHop virtual radio packet proof"
echo "  evidence: ${EVIDENCE_DIR}"
exit 0
