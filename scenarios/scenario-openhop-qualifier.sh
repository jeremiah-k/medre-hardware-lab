#!/usr/bin/env bash
# =============================================================================
# Scenario: openhop-qualifier
#
# OPT-IN scenario for the openHop Docker profile (compose profile
# `openhop`). It is deliberately NOT part of ALL_SCENARIOS: the default
# scenario suite must stay free of the openHop rig, and this case SKIPs
# (exit 2) whenever the profile is not up.
#
# WHAT IT PROVES
# --------------
# A bounded, repeatable MEDRE<->openHop lifecycle check:
#   * container startup (repeater + MEDRE),
#   * the MeshCore protocol handshake against the companion
#     (APP_START -> SELF_INFO -> channel read) using the PINNED upstream
#     SDK, run as the ONLY client of the companion while MEDRE is stopped,
#   * MEDRE's own adapter connection (from MEDRE's logs and its read-only
#     diagnostics snapshot),
#   * reconnect after the repeater is restarted underneath MEDRE,
#   * companion identity and channel state persistence across that restart,
#   * a clean MEDRE stop.
#
# WHAT IT DOES NOT PROVE
# ----------------------
# Delivery. The repeater runs with `radio_type: null` and its NullRadio
# raises on every send, so nothing here can transmit. "Adapter connected"
# is a protocol fact, never a delivery fact.
#
# SINGLE-CLIENT RULE
# ------------------
# openHop's companion accepts ONE client at a time and evicts the previous
# one. The protocol probe therefore runs ONLY while MEDRE is stopped, and
# NO test in this file ever TCP-probes the companion port while MEDRE
# holds it.
#
# REQUIREMENT FOR THE RIG
# -----------------------
# compose profile `openhop` up (lab/up.sh --openhop).
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SCENARIO="openhop-qualifier"

# The openHop profile is OPT-IN: without it this scenario is a SKIP, not a
# failure. The rig must be up in compose profile `openhop`.
if ! docker compose -f "${LAB_COMPOSE_FILE}" --profile openhop ps \
	--status running --services 2>/dev/null | grep -Fxq openhop-repeater; then
	SC_SKIP "openhop profile not running (openhop-repeater is down)" \
		"REQUIREMENT FOR THE RIG: compose profile 'openhop' up (lab/up.sh --openhop); this scenario is opt-in and is not part of ALL_SCENARIOS"
fi
if ! docker compose -f "${LAB_COMPOSE_FILE}" --profile openhop ps \
	--status running --services 2>/dev/null | grep -Fxq medre-openhop; then
	SC_SKIP "openhop profile not running (medre-openhop is down)" \
		"REQUIREMENT FOR THE RIG: compose profile 'openhop' up (lab/up.sh --openhop)"
fi

echo "[${SCENARIO}] running the bounded openHop lifecycle check"
LIFECYCLE_LOG="${LAB_ROOT}/lab/logs/scenarios/${SCENARIO}-$(date +%s).log"
mkdir -p "$(dirname "${LIFECYCLE_LOG}")"

if bash "${LAB_ROOT}/lab/openhop/lifecycle_check.sh" 2>&1 | tee "${LIFECYCLE_LOG}"; then
	SC_PASS "openHop Docker lifecycle check passed" \
		"evidence: ${LIFECYCLE_LOG}" \
		"verdict: ${LAB_ROOT}/lab/logs/openhop/verdict.json"
	exit 0
fi

SC_FAIL "openHop Docker lifecycle check failed" \
	"evidence: ${LIFECYCLE_LOG}"
