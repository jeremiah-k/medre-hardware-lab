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

__all__ = ["evaluate_a3", "A3_MIN_SAMPLER_COVERAGE"]

#: Minimum work-status samples per phase for the sampler coverage check.
A3_MIN_SAMPLER_COVERAGE = 3


def _check(name: str, ok: bool, detail: dict) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail}


def _fault_checks(fault: dict) -> list[dict]:
    occurred = fault.get("occurred") is True
    action = fault.get("action") or ""
    disconnect = fault.get("disconnect_offset")
    rejoin = fault.get("rejoin_offset")
    windowed = (
        isinstance(disconnect, (int, float))
        and isinstance(rejoin, (int, float))
        and rejoin > disconnect
    )
    owned = action.startswith("worker-owned")
    return [
        _check(
            "fault_actually_occurred",
            occurred and windowed,
            {
                "occurred": occurred,
                "windowed": windowed,
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
        if isinstance(nonce, str) and isinstance(offset, (int, float)):
            seen.setdefault(nonce, []).append(float(offset))
    missing = sorted(expected - set(seen))
    duplicated = sorted(n for n, offs in seen.items() if len(offs) != 1)
    inside = {
        n: offs
        for n, offs in seen.items()
        if len(offs) == 1 and disconnect < offs[0] < rejoin
    }
    all_inside_once = (
        bool(expected) and not missing and not duplicated and set(inside) == expected
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
                "rejoin_offset": rejoin,
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
    during = [
        obs
        for obs in (affected.get("observations_during_fault") or [])
        if isinstance(obs.get("observed_offset"), (int, float))
        and disconnect < float(obs["observed_offset"]) < rejoin
    ]
    nonces = {row["nonce"] for row in manifest}
    delivered_after = affected.get("post_rejoin_delivered_nonces") or []
    outcome = affected.get("post_rejoin_outcome") or "not_recorded"
    return [
        _check(
            "affected_route_silent_during_fault",
            not during,
            {
                "during_fault_observations": len(during),
                "reason": (
                    "affected route delivered during the fault" if during else None
                ),
            },
        ),
        _check(
            "affected_route_outcome_recorded",
            bool(manifest) and outcome != "not_recorded",
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
    for route_class in ("unaffected", "affected"):
        for row in transcript.get(route_class, {}).get("manifest") or []:
            nonce = row.get("nonce")
            binding = (transcript.get("generation_binding") or {}).get(nonce)
            if binding is None:
                problems.append({"nonce": nonce, "problem": "no_generation_binding"})
                continue
            if binding.get("event_id") is None:
                problems.append(
                    {
                        "nonce": nonce,
                        "problem": "nonce_not_one_canonical_event",
                        "event_ids": binding.get("event_ids"),
                    }
                )
            elif binding.get("event_id") != row.get("event_id"):
                problems.append(
                    {
                        "nonce": nonce,
                        "problem": "event_id_mismatch",
                        "manifest": row.get("event_id"),
                        "binding": binding.get("event_id"),
                    }
                )
            if binding.get("single_event") is not True:
                problems.append(
                    {"nonce": nonce, "problem": "duplicate_canonical_events"}
                )
            dest_rows = binding.get("dest_outbox_ids") or {}
            for adapter, outbox_id in (row.get("outbox_ids") or {}).items():
                if dest_rows.get(adapter) != outbox_id:
                    problems.append(
                        {
                            "nonce": nonce,
                            "problem": "outbox_id_mismatch",
                            "adapter": adapter,
                            "manifest": outbox_id,
                            "binding": dest_rows.get(adapter),
                        }
                    )
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


def _sampler_checks(samples: dict) -> list[dict]:
    counts = {
        phase: len(samples.get(phase) or [])
        for phase in ("baseline", "interruption", "recovery")
    }
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


def evaluate_a3(transcript: dict) -> dict:
    """Evaluate one mixed-continuity fault window transcript.

    Returns ``{"pass": bool, "checks": [...], "failed": [...], "scope": str}``.
    Unknown/empty manifests fail honestly: absence of a declared burst is
    not a pass.
    """
    fault = transcript.get("fault") or {}
    disconnect = fault.get("disconnect_offset")
    rejoin = fault.get("rejoin_offset")
    checks = _fault_checks(fault)
    if isinstance(disconnect, (int, float)) and isinstance(rejoin, (int, float)):
        checks += _window_checks(
            transcript.get("unaffected") or {}, float(disconnect), float(rejoin)
        )
        checks += _affected_checks(
            transcript.get("affected") or {}, float(disconnect), float(rejoin)
        )
    checks += _binding_checks(transcript)
    checks += _sampler_checks(transcript.get("sampler") or {})
    failed = [c for c in checks if not c["ok"]]
    return {
        "pass": not failed,
        "checks": checks,
        "failed": [c["check"] for c in failed],
        "scope": "A-3: healthy routes keep delivering while another transport "
        "is down; ordering of healthy delivery vs MeshCore rejoin is the "
        "assertion.",
    }
