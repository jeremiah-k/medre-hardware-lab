"""Pure A-3 evaluator: healthy routes deliver while another transport is down.

The assertion this module encodes (the mixed-transport continuity campaign's
core claim): with one MEDRE owner holding a healthy route and a faulted
MeshCore host link, the healthy route's deliveries are observed by an
independent Matrix recipient INSIDE the declared fault window, strictly
before the MeshCore session rejoins. Healthy traffic observed only after the
rejoin is a FAILURE, not a pass. Every input is a plain transcript dict, so
all regressions run offline against synthetic timelines.

Time discipline: offsets are durations relative to a recorded phase start
(monotonic clock); the evaluator only ever compares offsets, never
time-of-day values.
"""

from __future__ import annotations

import math

__all__ = ["evaluate_a3", "A3_MIN_SAMPLER_COVERAGE"]

#: Minimum work-status samples per phase for the sampler coverage check.
A3_MIN_SAMPLER_COVERAGE = 3


def _offset(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _identity(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check(name: str, ok: bool, detail: dict) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail}


def _fault_checks(fault: dict) -> list[dict]:
    occurred = fault.get("occurred") is True
    action = fault.get("action") or ""
    disconnect = fault.get("disconnect_offset")
    rejoin = fault.get("rejoin_offset")
    last_down = fault.get("last_disconnected_offset")
    transitions = fault.get("connection_transitions")
    continuous = (
        isinstance(transitions, list)
        and len(transitions) == 2
        and all(
            isinstance(entry, dict) and _offset(entry.get("offset"))
            for entry in transitions
        )
        and transitions[0].get("connected") is False
        and transitions[1].get("connected") is True
    )
    windowed = (
        _offset(disconnect)
        and _offset(rejoin)
        and _offset(last_down)
        and disconnect < last_down <= rejoin
        and continuous
        and transitions[0]["offset"]
        <= disconnect
        < last_down
        < transitions[1]["offset"]
        <= rejoin
    )
    owned = (
        isinstance(action, str)
        and action == "worker-owned busctl disconnect of the MEDRE-owned board"
        and fault.get("disconnect_command_succeeded") is True
    )
    return [
        _check(
            "fault_actually_occurred",
            occurred and windowed,
            {
                "occurred": occurred,
                "windowed": windowed,
                "continuous_transition_evidence": continuous,
                "action": action,
                "reason": (
                    "no disconnect/rejoin pair was observed"
                    if not (occurred and windowed)
                    else None
                ),
            },
        ),
        _check(
            "fault_was_worker_owned",
            owned,
            {
                "action": action,
                "reason": None if owned else "fault action not worker-owned",
            },
        ),
    ]


def _window_checks(unaffected: dict, disconnect: float, rejoin: float) -> list[dict]:
    manifest = unaffected.get("manifest") or []
    observations = unaffected.get("observations") or []
    expected = {row["nonce"] for row in manifest}
    seen: dict[str, list[float]] = {}
    for obs in observations:
        nonce = obs.get("nonce")
        offset = obs.get("observed_offset")
        if isinstance(nonce, str) and _offset(offset):
            seen.setdefault(nonce, []).append(float(offset))
    ids = [obs.get("event_id") for obs in observations]
    unique_witness = all(_identity(i) for i in ids) and len(set(ids)) == len(ids)
    missing = sorted(expected - set(seen))
    duplicated = sorted(n for n, offs in seen.items() if len(offs) != 1)
    inside = {
        n: offs
        for n, offs in seen.items()
        if len(offs) == 1 and disconnect < offs[0] < rejoin
    }
    all_inside_once = (
        bool(expected)
        and unique_witness
        and len(manifest) == len(expected)
        and len(observations) == len(expected)
        and not missing
        and not duplicated
        and set(inside) == expected
        and unaffected.get("coverage_complete") is True
        and all(obs.get("occurrences") == 1 for obs in observations)
    )
    first_inside = bool(inside) and min(min(o) for o in inside.values()) < rejoin
    return [
        _check(
            "healthy_route_delivered_inside_declared_window",
            bool(all_inside_once),
            {
                "expected_nonces": len(expected),
                "missing": missing,
                "duplicated": duplicated,
                "outside_window": sorted(
                    n for n in set(seen) - set(inside) if n not in missing
                ),
                "reason": (
                    "healthy-route traffic did not arrive exactly once per "
                    "manifest nonce strictly inside the fault window"
                    if not all_inside_once
                    else None
                ),
            },
        ),
        _check(
            "healthy_delivery_precedes_rejoin",
            bool(first_inside),
            {
                "first_healthy_offset": min(
                    (min(o) for o in inside.values()), default=None
                ),
                "last_disconnected_offset": rejoin,
            },
        ),
    ]


def _affected_checks(affected: dict, disconnect: float, rejoin: float) -> list[dict]:
    """Affected-route expectations, without overclaiming.

    During the fault the affected route must not deliver (no far-side
    observation inside the window); after the rejoin its recorded outcome is
    reported per the route's retry authority, never asserted beyond what the
    transcript captured.
    """
    manifest = affected.get("manifest") or []
    # The producer supplies all matching pre-rejoin or ambiguously timed
    # packets. Missing timestamps are not evidence of silence.
    during = affected.get("observations_during_fault")
    nonces = {row["nonce"] for row in manifest}
    delivered_after = affected.get("post_rejoin_delivered_nonces")
    during_present = isinstance(during, list)
    after_present = isinstance(delivered_after, list)
    during = during if during_present else []
    delivered_after = delivered_after if after_present else []
    outcome = affected.get("post_rejoin_outcome") or "not_recorded"
    return [
        _check(
            "affected_route_silent_during_fault",
            during_present and not during and affected.get("coverage_complete") is True,
            {
                "during_fault_observations": len(during),
                "reason": (
                    "affected route delivered during the fault" if during else None
                ),
            },
        ),
        _check(
            "affected_route_outcome_recorded",
            after_present
            and bool(manifest)
            and outcome in ("all_delivered", "partial", "not_delivered")
            and set(delivered_after) <= nonces
            and len(set(delivered_after)) == len(delivered_after)
            and (outcome != "all_delivered" or set(delivered_after) == nonces)
            and (outcome != "not_delivered" or not delivered_after)
            and (outcome != "partial" or bool(delivered_after)),
            {
                "manifest_nonces": len(nonces),
                "post_rejoin_delivered": len(delivered_after),
                "outcome": outcome,
            },
        ),
    ]


def _binding_checks(transcript: dict) -> list[dict]:
    """Generation binding: observations and evidence must name exact rows.

    Every manifest nonce must bind to exactly one canonical event (a
    non-None event id, no duplicate ingest) with exactly one destination
    outbox row, and the recorded durable identity must match the manifest's
    claim. A mismatch is a FAIL, never a note.
    """
    problems: list[dict] = []
    bindings = transcript.get("generation_binding") or {}
    manifests = {}
    for route_class, adapter in (("unaffected", "matrix1"), ("affected", "meshcore")):
        rows = transcript.get(route_class, {}).get("manifest") or []
        nonces = [row.get("nonce") for row in rows]
        if (
            not nonces
            or not all(_identity(n) for n in nonces)
            or len(set(nonces)) != len(nonces)
        ):
            problems.append(
                {"route": route_class, "problem": "invalid_manifest_nonces"}
            )
        manifests[route_class] = set(nonces)
        for row in rows:
            nonce = row.get("nonce")
            binding = bindings.get(nonce) or {}
            event_id = binding.get("event_id")
            outbox_id = (row.get("outbox_ids") or {}).get(adapter)
            dest_rows = binding.get("dest_outbox_ids") or {}
            if (
                not _identity(event_id)
                or event_id != row.get("event_id")
                or binding.get("single_event") is not True
            ):
                problems.append({"nonce": nonce, "problem": "event_id_mismatch"})
            if (
                set(row.get("outbox_ids") or {}) != {adapter}
                or not _identity(outbox_id)
                or dest_rows.get(adapter) != outbox_id
                or set(dest_rows) != {"matrix1", "meshcore"}
            ):
                problems.append(
                    {
                        "nonce": nonce,
                        "problem": "outbox_id_mismatch",
                        "adapter": adapter,
                    }
                )
    if (
        manifests["affected"] != manifests["unaffected"]
        or set(bindings) != manifests["affected"]
    ):
        problems.append({"problem": "route_manifest_mismatch"})
    sightings = {}
    for observation in transcript.get("unaffected", {}).get("observations") or []:
        sightings.setdefault(observation.get("nonce"), []).append(
            observation.get("event_id")
        )
    ingress_ids = []
    for nonce, binding in bindings.items():
        ingress = binding.get("matrix_ingress_event_id")
        delivery = binding.get("matrix_delivery_event_id")
        ingress_ids.append(ingress)
        if (
            not _identity(ingress)
            or binding.get("matrix_source_matches") is not True
            or not _identity(delivery)
            or delivery == ingress
            or sightings.get(nonce) != [delivery]
        ):
            problems.append(
                {"nonce": nonce, "problem": "matrix_native_identity_mismatch"}
            )
    if len(set(ingress_ids)) != len(ingress_ids):
        problems.append({"problem": "matrix_ingress_reused"})
    events = [binding.get("event_id") for binding in bindings.values()]
    outboxes = [
        outbox
        for binding in bindings.values()
        for outbox in (binding.get("dest_outbox_ids") or {}).values()
    ]
    if len(set(events)) != len(events) or len(set(outboxes)) != len(outboxes):
        problems.append({"problem": "generation_reused_across_nonces_or_destinations"})
    return [
        _check(
            "generation_binding_exact",
            not problems,
            {
                "problems": problems,
                "reason": (
                    None if not problems else "evidence bound the wrong generation"
                ),
            },
        )
    ]


def _sampler_checks(samples: dict, fault: dict) -> list[dict]:
    disconnect = fault.get("disconnect_offset")
    last_down = fault.get("last_disconnected_offset")
    rejoin = fault.get("rejoin_offset")
    counts = dict.fromkeys(("baseline", "interruption", "recovery"), 0)
    valid = all(_offset(t) for t in (disconnect, last_down, rejoin))
    seen = set()
    if valid:
        for phase in counts:
            for sample in samples.get(phase) or []:
                if not isinstance(sample, dict):
                    continue
                start, end = sample.get("start"), sample.get("end")
                if (
                    not (_offset(start) and _offset(end) and start <= end)
                    or (start, end) in seen
                ):
                    continue
                seen.add((start, end))
                if (
                    phase == "baseline"
                    and end < disconnect
                    or phase == "interruption"
                    and disconnect < start <= end < last_down
                    or phase == "recovery"
                    and start > rejoin
                ):
                    counts[phase] += 1
    ok = all(count >= A3_MIN_SAMPLER_COVERAGE for count in counts.values())
    return [
        _check(
            "sampler_coverage",
            ok,
            {
                "samples_per_phase": counts,
                "minimum": A3_MIN_SAMPLER_COVERAGE,
                "reason": None if ok else "sustained-load sampler too sparse",
            },
        )
    ]


def _evaluate_a3(transcript: dict) -> dict:
    """Evaluate one mixed-continuity fault window transcript.

    Returns ``{"pass": bool, "checks": [...], "failed": [...], "scope": str}``.
    Unknown/empty manifests fail honestly: absence of a declared burst is
    not a pass.
    """
    fault = transcript.get("fault") or {}
    disconnect = fault.get("disconnect_offset")
    rejoin = fault.get("last_disconnected_offset")
    checks = _fault_checks(fault)
    if _offset(disconnect) and _offset(rejoin):
        checks += _window_checks(
            transcript.get("unaffected") or {}, float(disconnect), float(rejoin)
        )
        checks += _affected_checks(
            transcript.get("affected") or {}, float(disconnect), float(rejoin)
        )
    checks += _binding_checks(transcript)
    checks += _sampler_checks(transcript.get("sampler") or {}, fault)
    failed = [c for c in checks if not c["ok"]]
    return {
        "pass": not failed,
        "checks": checks,
        "failed": [c["check"] for c in failed],
        "scope": "A-3: healthy routes keep delivering while another transport "
        "is down; ordering of healthy delivery vs MeshCore rejoin is the "
        "assertion.",
    }


def evaluate_a3(transcript: dict) -> dict:
    """Fail closed for malformed, incomplete or contradictory evidence."""
    try:
        return _evaluate_a3(transcript)
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return {
            "pass": False,
            "checks": [_check("invalid_transcript", False, {})],
            "failed": ["invalid_transcript"],
            "scope": "A-3: malformed evidence",
        }
