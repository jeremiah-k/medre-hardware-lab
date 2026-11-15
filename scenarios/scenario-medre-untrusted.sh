#!/usr/bin/env bash
# =============================================================================
# Scenario: medre-untrusted
#
# @untrusted forges canonical peer fields - cloning the medre block of a REAL
# medre-bot event and adapting every identity claim (meshnet label
# UntrustedClaims, sender Pretender, node id !f0rged00, packet id 999001) so
# the forgery is coherent and valid-shaped. Trust must be denied by SENDER
# identity, not by shape. BOTH implementations must reject:
#
#   1. No radio traffic on EITHER mesh carries any forged claim (collectors
#      on both peers across the whole claim window): no bridge adopts the
#      forged provenance and no peer-trust is granted. The visible authored
#      body MAY relay (documented mmrelay contract for untrusted users; the
#      medre bridge's visible-text policy is observed and reported - only
#      CLAIM adoption is a failure).
#   2. mmrelay message maps: any row keyed by the forged event carries only
#      the local meshnet label and that relay's own numeric packet id - never
#      UntrustedClaims or 999001 (mirrors untrusted-forged-envelope).
#   3. The medre bridge's state: no canonical event, native ref, or relation
#      adopts the forged claims (no ref keyed by 999001, no stored event
#      referencing UntrustedClaims/Pretender/!f0rged00).
#   4. Unmapped-room negative for medre: @alice sends PLAIN text in #lab-other
#      (mapped NOWHERE) - no radio traffic carrying it on either mesh, no
#      medre-state row, no mmrelay map rows.
#
# Requires the @untrusted account (mx_forge.py sends as @untrusted).
# =============================================================================
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source=lib/medre_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/medre_lib.sh"

require_rig
require_medre_bridge
resolve_rooms

SCENARIO="medre-untrusted"
UNTRUSTED_TOKEN="$(lib_py lab_env_probe.py untrusted_token | tr -d '"')"
[[ -n "${UNTRUSTED_TOKEN}" && "${UNTRUSTED_TOKEN}" != "null" ]] ||
	SC_SKIP "untrusted account token missing from runtime.json" \
		"the rig must provision @untrusted (scenarios/README.md scenario list)"

ALICE_TOKEN="$(lib_py mx_user.py token "${HUMAN_ALICE_LOCALPART}" | tr -d '"')"
[[ -n "${ALICE_TOKEN}" && "${ALICE_TOKEN}" != "null" ]] ||
	SC_SKIP "no token for ${HUMAN_ALICE_LOCALPART}" \
		"the rig must provision @alice and publish LAB_TOKEN_ALICE (scenarios/README.md)"

CLAIMS=("UntrustedClaims" "Pretender" "!f0rged00" "999001")

# wait_collector TMPFILE: bounded wait for the collector's final result line
# (printed when its capture window closes).
wait_collector() {
	local file="$1" window="$2"
	wait_until "$((window + 15))" grep -q '"packets"' "${file}" ||
		SC_FAIL "collector window ended without a result" "collector log: $(cat "${file}")"
}

# radio_hits TMPFILE MARKER_OR_CLAIM... -> JSON verdict {hits, count}
radio_hits() {
	local file="$1"
	shift
	local result needle
	result="$(collector_result "${file}")"
	printf '%s' "${result}" | tools_exec python3 -c "
import json, sys
data = json.load(sys.stdin)
needles = sys.argv[1:]
hits = [p for p in data.get('packets', []) for n in needles if n in (p.get('text') or '')]
print(json.dumps({'hits': hits, 'count': len(hits), 'seen_total': data.get('seen_total')}))
" "$@"
}

# =============================================================================
# Act 1: capture a REAL medre-bot event as the forgery template
# =============================================================================
SEED_MARKER="FORGE-SEED-MEDRE-$(SC_UID)"
SINCE_MS="$(date +%s%3N)"
capture out lib_py radio.py send-text "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" "${SEED_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "seed radio injection failed" "${out}"
capture seed_json lib_py mx_events.py wait "${ROOM_ALPHA_CH0_ID}" \
	--sender "${MEDRE_BOT_MXID}" --body-contains "${SEED_MARKER}" \
	--require-envelope --envelope-kind message.created --since-ms "${SINCE_MS}" \
	--timeout "${MATRIX_WAIT_SECONDS}"
((SC_STATUS == 0)) || SC_FAIL "no real medre-bot event to clone (seed)" "${seed_json}"
SEED_EVENT_ID="$(jq_get "${seed_json}" event.event_id | tr -d '"')"
medre_assert_valid "${seed_json}" "seed medre-bot event"
SC_PASS "captured a real medre-bot event as the forgery template" \
	"template_event=${SEED_EVENT_ID}"

# =============================================================================
# Act 2: collectors armed, then @untrusted sends the forged medre-shaped event
# =============================================================================
CLAIM_WINDOW=$((SNIFF_WAIT_SECONDS + 60))
start_collector COLLECT_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	--timeout "${CLAIM_WINDOW}"
start_collector COLLECT_B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	--timeout "${CLAIM_WINDOW}"
trap 'stop_collector "${COLLECT_A_PID:-}" "${COLLECT_A:-}"; stop_collector "${COLLECT_B_PID:-}" "${COLLECT_B:-}"' EXIT

FORGE_MARKER="MEDRE-FORGED-$(SC_UID)"
capture forge_json lib_py mx_forge.py "${ROOM_ALPHA_CH0_ID}" "${seed_json}" "${FORGE_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "forged send failed" "${forge_json}"
FORGED_EVENT_ID="$(jq_get "${forge_json}" event_id | tr -d '"')"
SC_PASS "@untrusted sent a medre-shaped forged event" \
	"event_id=${FORGED_EVENT_ID} forged_meshnet=$(jq_get "${forge_json}" forged_meshnet | tr -d '"')" \
	"forged_sender=$(jq_get "${forge_json}" forged_sender | tr -d '"')" \
	"cloned_canonical_event_id=$(jq_get "${forge_json}" cloned_canonical_event_id | tr -d '"')"

sleep "${SETTLE_SECONDS}"

# --- Assertion 2+3: claim-absence in mmrelay maps and medre state -------------
for claim in "${CLAIMS[@]}"; do
	for map_db in "${MAP_DB_A}" "${MAP_DB_B}"; do
		capture rows lib_py maps.py rows-for-text "${map_db}" "${claim}"
		count=$(jq_get "${rows}" 'rows' | grep -c '"meshtastic_id"' || true)
		((count == 0)) ||
			SC_FAIL "forged claim '${claim}' leaked into an mmrelay message map" \
				"db=${map_db} rows=${rows}"
	done
	capture out medre_state_py events-for-text "${MEDRE_DB_A}" "${claim}"
	count=$(jq_get "${out}" events | grep -c '"event_id"' || true)
	((count == 0)) ||
		SC_FAIL "forged claim '${claim}' adopted into the medre bridge state" \
			"db=${MEDRE_DB_A} events=${out}"
done
SC_PASS "no forged claim in either relay's map or the medre bridge state" \
	"checked=${CLAIMS[*]} dbs=${MAP_DB_A},${MAP_DB_B},${MEDRE_DB_A}"

capture out medre_state_py events-for-native "${MEDRE_DB_A}" "999001"
count=$(printf '%s' "${out}" | grep -c '"native_message_id"' || true)
((count == 0)) ||
	SC_FAIL "forged packet id 999001 adopted as a native ref in the medre bridge state" \
		"db=${MEDRE_DB_A} events=${out}"
SC_PASS "forged packet id never adopted as a medre native ref" "db=${MEDRE_DB_A}"

# mmrelay claim-free handling of the forged event's VISIBLE text (documented
# contract): if a map row exists it carries local meshnet + own packet id.
capture rows_a lib_py maps.py rows-for-event "${MAP_DB_A}" "${FORGED_EVENT_ID}"
if jq_get "${rows_a}" rows | grep -q '"meshtastic_id"'; then
	meshnet_a="$(printf '%s' "${rows_a}" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
print(",".join(sorted({str(r.get("meshtastic_meshnet")) for r in rows})))')"
	ids_a="$(printf '%s' "${rows_a}" | tools_exec python3 -c '
import json, sys
rows = json.load(sys.stdin)["rows"]
print(",".join(str(r.get("meshtastic_id")) for r in rows))')"
	[[ "${meshnet_a}" == "${MESHNET_A}" ]] ||
		SC_FAIL "mmrelay adopted the forged meshnet label" \
			"expected=${MESHNET_A} found=${meshnet_a} rows=${rows_a}"
	[[ "${ids_a}" =~ ^[0-9]+(,[0-9]+)*$ ]] ||
		SC_FAIL "mmrelay map meshtastic_id is not its own radio packet id" \
			"meshtastic_id=${ids_a}"
	SC_PASS "mmrelay relayed only the visible text with local attribution" \
		"db=${MAP_DB_A} meshnet=${meshnet_a} packet_ids=${ids_a}"
else
	SC_LOG "NOTE: mmrelay did not map the forged event's visible text (observed behavior; untrusted-forged-envelope documents the baseline)"
fi

# medre state: whatever the medre bridge did with the forged event's visible
# text, its stored canonical event must adopt nothing AUTHORITATIVE. The
# candidate contract (core 93f43311) carries inbound wire claims as
# UNVERIFIED SOURCE OBSERVATIONS confined to
# metadata.native.data.matrix.relay.* - those are exempt. Authoritative
# surfaces are the canonical identity columns, payload/body, native ref
# identity (adapter/native_message_id/direction), and relation rows: none of
# them may carry a forged claim, and the real sender must be preserved.
capture out medre_state_py events-for-native "${MEDRE_DB_A}" "${FORGED_EVENT_ID}"
if printf '%s' "${out}" | grep -q '"event_id"'; then
	verdict="$(printf '%s' "${out}" | tools_exec python3 -c '
import json, sys
data = json.load(sys.stdin)
claims = ["UntrustedClaims", "Pretender", "!f0rged00", "999001"]
problems = []
for event in data.get("events", []):
    event_id = event.get("event_id")
    authoritative = {
        "source_adapter": event.get("source_adapter"),
        "source_transport_id": event.get("source_transport_id"),
        "source_channel_id": event.get("source_channel_id"),
        "payload": event.get("payload"),
        "body_content": event.get("body_content"),
        "native_refs": [
            {k: ref.get(k) for k in ("adapter", "native_channel_id", "native_message_id", "direction")}
            for ref in event.get("native_refs", [])
        ],
        "relations": [
            {k: rel.get(k) for k in ("relation_type", "target_native_adapter", "target_native_message_id", "key")}
            for rel in event.get("relations", [])
        ],
    }
    blob = json.dumps(authoritative, ensure_ascii=False)
    for claim in claims:
        if claim in blob:
            problems.append("event %s carries forged claim %s in an authoritative surface" % (event_id, claim))
    if event.get("source_transport_id") not in ("@untrusted:lab.test", None, ""):
        problems.append(
            "event %s: source_transport_id=%r - real sender not preserved" % (event_id, event.get("source_transport_id"))
        )
print(json.dumps({"problems": problems, "count": len(data.get("events", []))}))
')"
	printf '%s' "${verdict}" | grep -q '"problems": \[\]' ||
		SC_FAIL "medre adopted forged claims into authoritative canonical state" \
			"verdict: ${verdict}"
	SC_PASS "medre stored only unverified observations of the forged claims; authoritative state adopts nothing" \
		"db=${MEDRE_DB_A} verdict: ${verdict}"
else
	SC_LOG "NOTE: medre state holds no canonical event for the forged event id (observed behavior: untrusted visible text not ingested, or ingested under medre's own canonical id)"
fi

# --- Assertion 1: claim-absence on radio across the claim window --------------
wait_collector "${COLLECT_A}" "${CLAIM_WINDOW}"
wait_collector "${COLLECT_B}" "${CLAIM_WINDOW}"
verdict_a="$(radio_hits "${COLLECT_A}" "${CLAIMS[@]}")"
verdict_b="$(radio_hits "${COLLECT_B}" "${CLAIMS[@]}")"
printf '%s' "${verdict_a}" | grep -q '"count": 0' ||
	SC_FAIL "forged claims appeared on Alpha radio" "verdict: ${verdict_a}" $'\n'"collector: $(collector_result "${COLLECT_A}")"
printf '%s' "${verdict_b}" | grep -q '"count": 0' ||
	SC_FAIL "forged claims appeared on Beta radio" "verdict: ${verdict_b}" $'\n'"collector: $(collector_result "${COLLECT_B}")"
SC_PASS "no forged claims on either mesh's radio across the claim window" \
	"alpha=${verdict_a}" $'\n'"beta=${verdict_b}"
stop_collector "${COLLECT_A_PID}" "${COLLECT_A}"
stop_collector "${COLLECT_B_PID}" "${COLLECT_B}"
trap - EXIT

# =============================================================================
# Act 3: unmapped-room negative for medre (fresh collector generation)
# =============================================================================
OTHER_MARKER="MEDRE-OTHER-$(SC_UID)"
OTHER_WINDOW=$((MATRIX_WAIT_SECONDS + SNIFF_WAIT_SECONDS))
start_collector COLLECT_A "${NODE_A_PEER_HOST}" "${NODE_A_PEER_PORT}" \
	--timeout "${OTHER_WINDOW}" --contains "${OTHER_MARKER}"
start_collector COLLECT_B "${NODE_B_PEER_HOST}" "${NODE_B_PEER_PORT}" \
	--timeout "${OTHER_WINDOW}" --contains "${OTHER_MARKER}"
trap 'stop_collector "${COLLECT_A_PID:-}" "${COLLECT_A:-}"; stop_collector "${COLLECT_B_PID:-}" "${COLLECT_B:-}"' EXIT

capture out lib_py mx_user.py send "${HUMAN_ALICE_LOCALPART}" "${ROOM_OTHER_ID}" "${OTHER_MARKER}"
((SC_STATUS == 0)) || SC_FAIL "unmapped-room send failed (is @alice in #lab-other?)" "${out}"
OTHER_EVENT_ID="$(jq_get "${out}" event_id | tr -d '"')"
SC_PASS "plain text sent into the unmapped room #lab-other" \
	"event=${OTHER_EVENT_ID} marker=${OTHER_MARKER}"

wait_collector "${COLLECT_A}" "${OTHER_WINDOW}"
wait_collector "${COLLECT_B}" "${OTHER_WINDOW}"
verdict_a="$(radio_hits "${COLLECT_A}" "${OTHER_MARKER}")"
verdict_b="$(radio_hits "${COLLECT_B}" "${OTHER_MARKER}")"
printf '%s' "${verdict_a}" | grep -q '"count": 0' ||
	SC_FAIL "unmapped-room text leaked to Alpha radio" "verdict: ${verdict_a}" $'\n'"collector: $(collector_result "${COLLECT_A}")"
printf '%s' "${verdict_b}" | grep -q '"count": 0' ||
	SC_FAIL "unmapped-room text leaked to Beta radio" "verdict: ${verdict_b}" $'\n'"collector: $(collector_result "${COLLECT_B}")"
SC_PASS "unmapped-room negative: radio silence on both meshes" \
	"alpha=${verdict_a}" $'\n'"beta=${verdict_b}"

capture rows lib_py maps.py rows-for-event "${MAP_DB_A}" "${OTHER_EVENT_ID}"
count=$(jq_get "${rows}" 'rows' | grep -c '"meshtastic_id"' || true)
((count == 0)) || SC_FAIL "unmapped-room negative: mmrelay A wrote a map row for #lab-other text" "${rows}"
capture rows lib_py maps.py rows-for-event "${MAP_DB_B}" "${OTHER_EVENT_ID}"
count=$(jq_get "${rows}" 'rows' | grep -c '"meshtastic_id"' || true)
((count == 0)) || SC_FAIL "unmapped-room negative: mmrelay B wrote a map row for #lab-other text" "${rows}"
capture out medre_state_py events-for-native "${MEDRE_DB_A}" "${OTHER_EVENT_ID}"
count=$(printf '%s' "${out}" | grep -c '"event_id"' || true)
((count == 0)) || SC_FAIL "unmapped-room negative: medre state holds a canonical event for #lab-other text" "${out}"
SC_PASS "unmapped-room negative: no map rows (both relays) and no medre state row" \
	"event=${OTHER_EVENT_ID} dbs=${MAP_DB_A},${MAP_DB_B},${MEDRE_DB_A}"
stop_collector "${COLLECT_A_PID}" "${COLLECT_A}"
stop_collector "${COLLECT_B_PID}" "${COLLECT_B}"
trap - EXIT

SC_LOG "[${SCENARIO}] complete"
