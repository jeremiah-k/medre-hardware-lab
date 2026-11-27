#!/usr/bin/env bash
# =============================================================================
# Phase 3B bridge proof: MEDRE <-> openHop radio overlay <-> MeshC.
#
# WHAT IT PROVES
# --------------
# That MEDRE bridges REAL traffic in BOTH directions between the openHop
# radio overlay and MeshC:
#
#   1. MC peer -> MEDRE -> MeshC peer
#      A unique channel-0 nonce is submitted through companion B by the
#      virtual MeshCore peer (the only client of B). Repeater B encrypts
#      it and transmits; the lab's virtual radio carries it to repeater A,
#      whose companion delivers it to MEDRE (the only client of A). MEDRE's
#      meshcore adapter decodes it into a canonical event, the bidirectional
#      route openhop-a <-> radio-c relays it to the meshtastic adapter, and
#      the independent MeshC observer on meshtasticd-c-peer sees it EXACTLY
#      ONCE.
#
#   2. MeshC peer -> MEDRE -> MC peer
#      A unique channel-0 nonce is injected at meshtasticd-c-peer. It
#      reaches MEDRE over MeshC; MEDRE relays it through its meshcore
#      adapter to companion A; repeater A transmits it; the virtual radio
#      carries it to repeater B; the virtual MC peer (the only client of B)
#      receives it ONCE in this run, with openHop's own "<node name>: " wire
#      prefix.
#
# ASSERTIONS
#   * exact bytes on both arrivals (the openHop prefix is accounted for);
#   * exactly ONE arrival per direction in the proof window;
#   * NO duplicate client delivery in either direction: the arrival count
#     equals the distinct arrival count AND the client-level duplicate
#     counter is zero. The openHop companion's duplicate-delivery race
#     (D5) is pinned shut in the candidate sources this profile runs on,
#     so a duplicate is a REGRESSION and fails the proof;
#   * no echo back to companion B during direction 1;
#   * fresh nonces per direction (never reused).
#
# WHAT IT DOES NOT PROVE
#   Anything about physical RF. The relay simulates the MEDIUM only; the
#   MeshCore packets crossing it are real. See docs/openhop-bridge-
#   qualification.md.
#
# Usage: lab/openhop-bridge/bridge_proof.sh [--evidence-dir DIR]
# Exit:  0 = PASS, 1 = FAIL.
# =============================================================================
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${LAB_ROOT}"

EVIDENCE_DIR="${LAB_ROOT}/lab/logs/openhop-bridge"
while (($# > 0)); do
	case "$1" in
	--evidence-dir)
		EVIDENCE_DIR="$2"
		shift 2
		;;
	*)
		echo "unknown argument: $1" >&2
		exit 1
		;;
	esac
done
umask 077
mkdir -p "${EVIDENCE_DIR}"
# Clear the artifacts this run rewrites, so a FAILED run never leaves the
# previous run's verdict.json behind: a stale "pass" would misreport the
# current state.
rm -f "${EVIDENCE_DIR}/bridge-proof.json" \
	"${EVIDENCE_DIR}/bridge-proof.json.err" \
	"${EVIDENCE_DIR}/evidence-mc-to-mesh.json" \
	"${EVIDENCE_DIR}/evidence-mesh-to-mc.json" \
	"${EVIDENCE_DIR}/verdict.json" \
	"${EVIDENCE_DIR}/failures.txt" \
	"${EVIDENCE_DIR}/medre-state.json" \
	"${EVIDENCE_DIR}/medre-state-summary.json"

# A later evidence/read failure must not leave an earlier PASS artifact.
# shellcheck disable=SC2329 # Invoked by the EXIT trap below, not by name.
write_failed_verdict() {
	local rc=$?
	if ((rc != 0)); then
		python3 - "${EVIDENCE_DIR}/verdict.json" "${rc}" <<'PYVERDICT'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    verdict = json.loads(path.read_text())
except (OSError, ValueError):
    verdict = {"label": "protocol-simulated"}
verdict["pass"] = False
verdict.setdefault("complete", False)
verdict["exit_status"] = int(sys.argv[2])
path.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n")
PYVERDICT
	fi
}
trap write_failed_verdict EXIT
COMPOSE=(docker compose --profile openhop-bridge)
MEDRE_CONTAINER="medre-lab-medre-openhop-bridge"

log() { printf '\n=== %s ===\n' "$*"; }
fail() {
	echo "FAIL: $*" >&2
	exit 1
}

log "Phase 0: preconditions"
docker inspect -f '{{.State.Running}}' "${MEDRE_CONTAINER}" 2>/dev/null |
	grep -qx true || fail "${MEDRE_CONTAINER} is not running"
echo "  ${MEDRE_CONTAINER}: running"

# The expected openHop wire prefix on the direction-2 arrival is repeater
# A's COMPANION node name (the sending side stamps it on the wire).
EXPECT_PREFIX="$(
	python3 - <<'PYPREFIX'
import yaml

with open("lab/state/config/openhop-radio-repeater-a.yaml", encoding="utf-8") as handle:
    config = yaml.safe_load(handle)
for companion in config["identities"]["companions"]:
    print(f"{companion['settings']['node_name']}: ")
PYPREFIX
)"
echo "  expected direction-2 wire prefix: ${EXPECT_PREFIX}"

# Baseline the radio-path counters so the proof can assert that the
# MEDIUM carried each bridged packet once over the proof window. The relay
# logs every transmission it forwards and each repeater logs every
# group-text reception, so the deltas over the proof window are the
# radio-level single-crossing fact.
log "Phase 0b: baselining the radio-path counters"
RELAY_LOG="${EVIDENCE_DIR}/relay.log"
A_LOG="${EVIDENCE_DIR}/repeater-a.log"
B_LOG="${EVIDENCE_DIR}/repeater-b.log"
docker logs medre-lab-openhop-radio-relay >"${RELAY_LOG}" 2>&1 || true
docker logs medre-lab-openhop-repeater-a >"${A_LOG}" 2>&1 || true
docker logs medre-lab-openhop-repeater-b >"${B_LOG}" 2>&1 || true
RELAY_BASELINE="$(grep -c "transmitted" "${RELAY_LOG}" || true)"
A_RX_BASELINE="$(grep -c "RX GRP_TXT" "${A_LOG}" || true)"
B_RX_BASELINE="$(grep -c "RX GRP_TXT" "${B_LOG}" || true)"
echo "  relay transmissions baseline: ${RELAY_BASELINE}"
echo "  repeater A receptions baseline: ${A_RX_BASELINE}"
echo "  repeater B receptions baseline: ${B_RX_BASELINE}"

log "Phase 1: the two-direction bridge proof"
PROOF_JSON="${EVIDENCE_DIR}/bridge-proof.json"
# The probe's exit status is NOT the proof's verdict: a FAILED probe still
# produced the measurements, and those measurements are the evidence of
# WHAT went wrong. So the probe's status is recorded, the JSON is parsed
# and the evidence is written, and only then does the proof fail.
PROBE_RC=0
"${COMPOSE[@]}" run --rm --no-deps \
	-e "OPENHOP_BRIDGE_EXPECT_PREFIX=${EXPECT_PREFIX}" \
	-e "OPENHOP_BRIDGE_GET_MSG_LOCK=${OPENHOP_BRIDGE_GET_MSG_LOCK:-0}" \
	openhop-bridge-probe >"${PROOF_JSON}" 2>"${PROOF_JSON}.err" || PROBE_RC=$?
if [[ ${PROBE_RC} != "0" ]]; then
	echo "  the probe reported a failure (exit ${PROBE_RC}); the evidence below says why"
fi
if [[ ! -s ${PROOF_JSON} ]]; then
	echo "--- probe stderr ---" >&2
	cat "${PROOF_JSON}.err" >&2 || true
	fail "the bridge probe produced no result (see ${PROOF_JSON})"
fi

cat "${PROOF_JSON}"

python3 - "${PROOF_JSON}" "${EVIDENCE_DIR}" "${PROBE_RC}" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path("lab/tools").resolve()))
from openhop_bridge_check import check_proof

proof_path = Path(sys.argv[1])
evidence_dir = Path(sys.argv[2])
proof = json.loads(proof_path.read_text())

# STRICT assertions, shared with the offline unit tests
# (lab/tests/test_openhop_bridge_check.py):
#   * exactly one distinct arrival per direction,
#   * arrival_count == distinct_arrival_count (no repeat delivery),
#   * duplicate_client_events == 0 (D5 is pinned: a duplicate is a
#     regression, not a known upstream quirk),
#   * byte-exact text, no echo, fresh nonces.
failures = check_proof(proof)
if int(sys.argv[3]) != 0:
    failures.append("probe exited unsuccessfully")

d1 = proof.get("direction_mc_to_mesh") or {}
d2 = proof.get("direction_mesh_to_mc") or {}

arrivals = d1.get("observer_arrivals") or []
text = arrivals[0].get("text") if arrivals else None

evidence_common = {
    "label": "protocol-simulated",
    "evidence_boundary": (
        "Real MeshCore packets cross the lab's virtual radio relay; the RF "
        "MEDIUM is simulated. NOT evidence of physical RF delivery, range, "
        "airtime or regulatory behaviour."
    ),
    "case": proof.get("case"),
    "sdk_version": proof.get("sdk_version"),
    "channel_index": proof.get("channel_index"),
    "generated_at": proof.get("finished_at"),
}

ev1 = dict(evidence_common)
ev1.update(
    {
        "direction": "mc-peer -> MEDRE -> meshc-peer",
        "nonce": d1.get("nonce"),
        "send_result": d1.get("send_event"),
        "send_started_at": d1.get("started_at"),
        "send_finished_at": d1.get("send_finished_at"),
        "observer": "meshtasticd-c-peer (independent of MEDRE)",
        "observer_bytes": text,
        "observer_arrivals": arrivals,
        "arrival_count": d1.get("arrival_count"),
        "distinct_arrival_count": d1.get("distinct_arrival_count"),
        "duplicate_client_events": d1.get("duplicate_client_events"),
        "arrival_timestamp": arrivals[0].get("received_at") if arrivals else None,
        "source_channel": "0",
        "destination_channel": "0",
        "native_id": arrivals[0].get("packet_id") if arrivals else None,
        "native_sender_id": arrivals[0].get("from_id") if arrivals else None,
        "echo_at_b": d1.get("echo_at_b"),
        "mc_peer": proof.get("mc_peer"),
    }
)
(evidence_dir / "evidence-mc-to-mesh.json").write_text(
    json.dumps(ev1, indent=2, sort_keys=True) + "\n"
)

mc_arrivals = d2.get("mc_peer_arrivals") or []
mc_arrival = mc_arrivals[0] if mc_arrivals else {}
nonce = d2.get("nonce")
ev2 = dict(evidence_common)
ev2.update(
    {
        "direction": "meshc-peer -> MEDRE -> mc-peer",
        "nonce": nonce,
        "send_result": d2.get("injection", {}).get("packet_id"),
        "send_started_at": d2.get("started_at"),
        "send_finished_at": d2.get("injection", {}).get("finished_at"),
        "observer": "virtual MeshCore peer on companion B (independent of MEDRE)",
        "expected_bytes": d2.get("expected_text"),
        "observer_bytes": mc_arrival.get("text"),
        "observer_arrivals": mc_arrivals,
        "arrival_count": d2.get("arrival_count"),
        "distinct_arrival_count": d2.get("distinct_arrival_count"),
        "duplicate_client_events": d2.get("duplicate_client_events"),
        "arrival_timestamp": mc_arrival.get("received_at"),
        "source_channel": "0",
        "destination_channel": "0",
        "native_id": mc_arrival.get("message_id"),
        "native_sender_id": mc_arrival.get("sender_id"),
        "mc_peer": proof.get("mc_peer"),
        "openhop_wire_prefix": (d2.get("expected_text") or "")[:-len(nonce)] if nonce else None,
    }
)
(evidence_dir / "evidence-mesh-to-mc.json").write_text(
    json.dumps(ev2, indent=2, sort_keys=True) + "\n"
)

verdict = {
    "case": proof.get("case"),
    "label": "protocol-simulated",
    "pass": False,
    "complete": False,
    "failures": failures,
    "directions": {
        "mc_to_mesh": {
            "arrival_count": d1.get("arrival_count"),
            "distinct_arrival_count": d1.get("distinct_arrival_count"),
            "duplicate_client_events": d1.get("duplicate_client_events"),
            "bytes_exact": text == d1.get("nonce"),
            "echo_at_b": len(d1.get("echo_at_b") or []),
        },
        "mesh_to_mc": {
            "arrival_count": d2.get("arrival_count"),
            "distinct_arrival_count": d2.get("distinct_arrival_count"),
            "duplicate_client_events": d2.get("duplicate_client_events"),
            # Byte-exactness is about the TEXT the peer received, not about
            # how many times: the distinct set of the arrivals that carry
            # the bridged text must be exactly the expected text.
            "bytes_exact": (
                {
                    entry.get("text")
                    for entry in (d2.get("mc_peer_arrivals") or [])
                    if entry.get("text") == d2.get("expected_text")
                }
                == {d2.get("expected_text")}
            ),
        },
    },
    "fresh_nonces": bool(d1.get("nonce") and nonce and d1["nonce"] != nonce),
    "evidence": [
        "evidence-mc-to-mesh.json",
        "evidence-mesh-to-mc.json",
    ],
}
(evidence_dir / "verdict.json").write_text(
    json.dumps(verdict, indent=2, sort_keys=True) + "\n"
)
print("evidence written")

# The evidence is written FIRST, so a failed run leaves a verdict that says
# so; a stale verdict from an earlier passing run must never survive a
# failed one. The failure list is handed to the shell, which reports it
# AFTER the remaining evidence phases have run.
if failures:
    Path(evidence_dir / "failures.txt").write_text(
        "\n".join(failures) + "\n", encoding="utf-8"
    )
    raise SystemExit(0)
PY

log "Phase 2: MEDRE's own mapping store (canonical events, native ids, receipts)"
MEDRE_STATE="${EVIDENCE_DIR}/medre-state.json"
docker cp lab/tools/openhop_bridge_medre_state.py "${MEDRE_CONTAINER}:/tmp/openhop_bridge_medre_state.py" >/dev/null
docker exec "${MEDRE_CONTAINER}" python3 /tmp/openhop_bridge_medre_state.py >"${MEDRE_STATE}" 2>&1 ||
	fail "could not read MEDRE's mapping store"

python3 - "${MEDRE_STATE}" "${EVIDENCE_DIR}" "${PROOF_JSON}" <<'PY'
import json
import sys
from pathlib import Path

# The store-row selection is importable and offline-tested: the bridged
# text lives in the canonical event's metadata (payload carries "{}" or a
# portnum object), so the nonce is matched anywhere in the stored row.
sys.path.insert(0, "lab/tools")
import openhop_bridge_store_select as store_select

state = json.loads(Path(sys.argv[1]).read_text())
evidence_dir = Path(sys.argv[2])
proof = json.loads(Path(sys.argv[3]).read_text())

# Select the test messages first, then associate every store transition
# by event ID. A receipt can commit after the peer has observed delivery.
selection = store_select.select_nonce_events(
    state,
    (
        (proof.get("direction_mc_to_mesh") or {}).get("nonce"),
        (proof.get("direction_mesh_to_mc") or {}).get("nonce"),
    ),
)
nonces = selection["nonces"]
events = selection["events"]
event_ids = selection["event_ids"]
receipts = selection["receipts"]
outbox = selection["outbox"]
refs = selection["refs"]

# Exactly ONE canonical event per bridged direction: no duplicate client
# event at companion A, and no double-bridged event. The failures are
# COLLECTED, not raised, so the rest of the evidence is still captured.
store_failures: list[str] = []
if len(nonces) != 2 or any(
    count != 1 for count in selection["events_per_nonce"].values()
):
    store_failures.append("expected one canonical event for each proof nonce")
if len(events) != 2:
    store_failures.append(
        f"MEDRE created {len(events)} canonical events, expected 2"
    )
if len(outbox) != 2:
    store_failures.append(
        f"MEDRE recorded {len(outbox)} delivery outbox rows, expected 2 (one per direction)"
    )
# Each delivery writes a 'queued' and a 'sent' attempt receipt; exactly one
# 'sent' receipt per delivery is the single-delivery fact.
sent_receipts = [row for row in receipts if row.get("status") == "sent"]
if len(sent_receipts) != 2:
    store_failures.append(
        f"MEDRE recorded {len(sent_receipts)} sent receipts, expected 2 (one per direction)"
    )
if {row["event_id"] for row in sent_receipts} != {event["event_id"] for event in events}:
    store_failures.append(
        "the sent receipts do not cover exactly the two canonical events"
    )
routes = sorted(row["route_id"] for row in outbox)
if routes != ["openhop-bridge", "openhop-bridge__rev_0"]:
    store_failures.append(f"unexpected delivery routes {routes}")
if any(row["status"] != "sent" for row in outbox):
    store_failures.append("a MEDRE delivery did not reach 'sent'")

summary = {
    "canonical_event_count": len(events),
    "delivery_outbox_count": len(outbox),
    "delivery_receipt_count": len(receipts),
    "native_message_ref_count": len(refs),
    "events": [
        {
            "event_id": event["event_id"],
            "event_kind": event["event_kind"],
            "source_adapter": event["source_adapter"],
            "source_channel_id": event["source_channel_id"],
            "source_native_adapter": event["source_native_adapter"],
            "source_native_channel_id": event["source_native_channel_id"],
            "source_native_message_id": event["source_native_message_id"],
            "created_at": event["created_at"],
        }
        for event in events
    ],
    "native_message_refs": refs,
    "delivery_receipts": receipts,
    "delivery_outbox": outbox,
}
(evidence_dir / "medre-state-summary.json").write_text(
    json.dumps(summary, indent=2, sort_keys=True) + "\n"
)

# Attach the MEDRE-side facts to the per-direction evidence files.
ev1 = json.loads((evidence_dir / "evidence-mc-to-mesh.json").read_text())
ev2 = json.loads((evidence_dir / "evidence-mesh-to-mc.json").read_text())
for event in summary["events"]:
    target = ev1 if event["source_adapter"] == "openhop-a" else ev2
    target["medre_canonical_event_id"] = event["event_id"]
    target["medre_canonical_event_created_at"] = event["created_at"]
    target["medre_source_native_channel_id"] = event["source_native_channel_id"]
    target["medre_source_native_message_id"] = event["source_native_message_id"]
    target["medre_source_channel_id"] = event["source_channel_id"]
(evidence_dir / "evidence-mc-to-mesh.json").write_text(
    json.dumps(ev1, indent=2, sort_keys=True) + "\n"
)
(evidence_dir / "evidence-mesh-to-mc.json").write_text(
    json.dumps(ev2, indent=2, sort_keys=True) + "\n"
)

# The store failures join the ones the strict checker found, so the
# verdict lists every reason the run failed.
if store_failures:
    existing = ""
    failures_path = evidence_dir / "failures.txt"
    if failures_path.exists():
        existing = failures_path.read_text(encoding="utf-8")
    failures_path.write_text(
        existing + "\n".join(store_failures) + "\n", encoding="utf-8"
    )

print("MEDRE mapping-store evidence attached")
PY

log "Phase 3: capturing the MEDRE and repeater logs"
docker logs "${MEDRE_CONTAINER}" >"${EVIDENCE_DIR}/medre-openhop-bridge.log" 2>&1 || true
docker logs medre-lab-openhop-repeater-a >"${A_LOG}" 2>&1 || true
docker logs medre-lab-openhop-repeater-b >"${B_LOG}" 2>&1 || true
docker logs medre-lab-openhop-radio-relay >"${RELAY_LOG}" 2>&1 || true

log "Phase 4: the medium carried each bridged packet once"
RELAY_AFTER="$(grep -c "transmitted" "${RELAY_LOG}" || true)"
A_RX_AFTER="$(grep -c "RX GRP_TXT" "${A_LOG}" || true)"
B_RX_AFTER="$(grep -c "RX GRP_TXT" "${B_LOG}" || true)"
RELAY_DELTA=$((RELAY_AFTER - RELAY_BASELINE))
A_RX_DELTA=$((A_RX_AFTER - A_RX_BASELINE))
B_RX_DELTA=$((B_RX_AFTER - B_RX_BASELINE))
echo "  relay transmissions: ${RELAY_DELTA}"
echo "  repeater A receptions: ${A_RX_DELTA}"
echo "  repeater B receptions: ${B_RX_DELTA}"
# Two bridged packets (one per direction) cross the medium once each:
# one transmission each way, one reception each way.
RADIO_FAILURES=()
if [[ ${RELAY_DELTA} != "2" ]]; then
	RADIO_FAILURES+=("expected exactly two transmissions over the virtual radio, got ${RELAY_DELTA}")
fi
if [[ ${A_RX_DELTA} != "1" || ${B_RX_DELTA} != "1" ]]; then
	RADIO_FAILURES+=("expected exactly one reception per repeater, got A=${A_RX_DELTA} B=${B_RX_DELTA}")
fi
# The radio-path failures join the ones already collected, so the verdict
# lists EVERY reason the run failed.
FAILURES_FILE="${EVIDENCE_DIR}/failures.txt"
if ((${#RADIO_FAILURES[@]} > 0)); then
	printf '%s\n' "${RADIO_FAILURES[@]}" >>"${FAILURES_FILE}"
fi

python3 - "${EVIDENCE_DIR}/verdict.json" "${RELAY_DELTA}" "${A_RX_DELTA}" "${B_RX_DELTA}" "${FAILURES_FILE}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
verdict = json.loads(path.read_text())
verdict["radio_path"] = {
    "relay_transmissions": int(sys.argv[2]),
    "repeater_a_receptions": int(sys.argv[3]),
    "repeater_b_receptions": int(sys.argv[4]),
}
# The verdict carries the FULL failure list, so it says on its own why the
# run failed without a second file.
failures_path = Path(sys.argv[5])
failures = []
if failures_path.exists():
    failures = [
        line for line in failures_path.read_text(encoding="utf-8").splitlines() if line
    ]
verdict["failures"] = failures
verdict["pass"] = not failures
verdict["complete"] = True
path.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n")
print("radio-path facts recorded")
PY

# --- Verdict -----------------------------------------------------------------
# Every failure is reported together, at the END, so the evidence for a
# failed run is complete: a duplicate client delivery and the radio-path
# counters are the two halves of the same story.
ALL_FAILURES=()
if [[ -s ${FAILURES_FILE} ]]; then
	while IFS= read -r line; do
		[[ -n ${line} ]] && ALL_FAILURES+=("${line}")
	done <"${FAILURES_FILE}"
	rm -f "${FAILURES_FILE}"
fi
if ((${#ALL_FAILURES[@]} > 0)); then
	for failure in "${ALL_FAILURES[@]}"; do
		echo "FAIL: ${failure}" >&2
	done
	fail "the Phase 3B bridge proof failed ${#ALL_FAILURES[@]} assertion(s) (see ${EVIDENCE_DIR}/verdict.json)"
fi

log "PASS: the Phase 3B bridge delivered both directions once each in this run"
echo "  evidence: ${EVIDENCE_DIR}"
exit 0
