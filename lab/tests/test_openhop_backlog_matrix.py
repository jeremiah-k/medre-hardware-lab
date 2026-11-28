"""Offline tests for the backlog/persistence matrix helpers.

These cover the counting rules the matrix relies on, so a wrong count is a
test failure rather than a wrong number in an evidence file:

* a nonce-bearing message counts even when it renders imperfectly (a
  corrupted duplicate still carries the marker and is counted);
* duplicates, losses and ordering are reported separately;
* the missing-index helper names exactly the nonces that never arrived.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "openhop_backlog_matrix.py"
)
spec = importlib.util.spec_from_file_location("openhop_backlog_matrix", MODULE_PATH)
assert spec and spec.loader
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


def test_nonce_index_extracts_the_marker_index():
    assert matrix.nonce_index("p4c1-17-9f2ab1") == 17
    assert matrix.nonce_index("MEDRE-RADIO-b: p4c2l-3-deadbeef") == 3


def test_nonce_marker_keeps_the_unique_tail():
    """The unique tail is what makes one message one message."""
    assert matrix.nonce_marker("p4c1-17-9f2ab1") == "p4c1-17-9f2ab1"
    assert matrix.nonce_marker("p4c1-17-aaaa11") == "p4c1-17-aaaa11"


def test_nonce_index_ignores_text_without_a_marker():
    assert matrix.nonce_index("") is None
    assert matrix.nonce_index("no marker here") is None
    assert matrix.nonce_index(None) is None


def test_count_nonce_rows_counts_corrupted_duplicates():
    rows = [
        {"text": "p4c1-0-aaaa"},
        {"text": "altered: p4c1-0-aaaa"},  # a duplicate with a bad rendering
        {"text": "p4c1-1-cccc"},
        {"text": "unrelated"},
    ]
    count, markers = matrix.count_nonce_rows(rows)
    assert count == 3
    assert markers == ["p4c1-0-aaaa", "p4c1-0-aaaa", "p4c1-1-cccc"]


def test_count_nonce_rows_can_be_scoped_to_this_run():
    """MEDRE's store is cumulative; a re-run must not count the old rows."""
    rows = [{"text": "p4c1-0-aaaa"}, {"text": "p4c1-0-old0"}]
    count, markers = matrix.count_nonce_rows(rows, only={"p4c1-0-aaaa"})
    assert count == 1
    assert markers == ["p4c1-0-aaaa"]


def test_ordering_report_separates_duplicates_losses_and_order():
    report = matrix.ordering_report(["c1-0-a", "c1-1-a", "c1-2-a", "c1-2-a", "c1-4-a"])
    assert report["arrivals"] == 5
    assert report["distinct"] == 4
    assert report["duplicate_arrivals"] == 1
    assert report["out_of_order_arrivals"] == 0
    assert report["distinct_indices"] == ["c1-0-a", "c1-1-a", "c1-2-a", "c1-4-a"]


def test_ordering_report_flags_an_out_of_order_arrival():
    report = matrix.ordering_report(["p4c1-2-aa", "p4c1-1-bb", "p4c1-0-cc"])
    assert report["out_of_order_arrivals"] == 2
    assert report["duplicate_arrivals"] == 0


def test_every_case_is_registered():
    assert set(matrix.CASES) == {"c1", "c2", "c3", "c4", "c5", "c6"}


def test_two_cases_in_one_run_do_not_collide():
    """Two cases number their nonces from 0, so the index alone collides."""
    backlog = [f"p4c2b-{i}-aa" for i in range(5)]
    live = [f"p4c2l-{i}-bb" for i in range(5)]
    report = matrix.ordering_report(backlog + live)
    assert report["arrivals"] == 10
    assert report["distinct"] == 10
    assert report["duplicate_arrivals"] == 0
    assert report["out_of_order_arrivals"] == 0


def test_ordering_is_judged_within_a_case():
    report = matrix.ordering_report(["c2b-0-a", "c2b-1-a", "c2l-0-b", "c2b-2-a"])
    assert report["out_of_order_arrivals"] == 0
    assert report["duplicate_arrivals"] == 0


def test_missing_indices_names_the_markers_that_never_arrived():
    markers = ["c2b-0-a", "c2b-2-a"]
    assert matrix.missing_indices(markers, {"c2b-0-a", "c2b-1-a", "c2b-2-a"}) == [
        "c2b-1-a"
    ]
    assert matrix.missing_indices(markers, set(markers)) == []


def test_sent_markers_scopes_a_probe_result_to_its_own_nonces():
    markers = matrix.sent_markers({"nonces": ["p4c2b-0-aa", "p4c2b-1-bb"]})
    assert markers == {"p4c2b-0-aa", "p4c2b-1-bb"}


def test_kill_mid_drain_judge_passes_the_designed_run():
    """The designed run: one outstanding message, queue held, one missing."""
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=29,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=["p4c5ab12-0-deadbeef"],
        delivered_before_kill=0,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=7.5,
    )
    assert failures == []


def test_kill_mid_drain_judge_fails_a_shrinking_queue():
    """A queue that kept shrinking means more than one message was popped."""
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=27,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=["p4c5ab12-0-deadbeef", "p4c5ab12-1-aa", "p4c5ab12-2-bb"],
        delivered_before_kill=0,
        queue_samples=[29, 27, 26, 24],
        hold_seconds=7.5,
    )
    assert any(
        "did not hold still while one message was outstanding" in failure
        for failure in failures
    )
    assert any("3 message(s) are missing" in failure for failure in failures)


def test_kill_mid_drain_judge_fails_a_short_hold():
    """The hold must span longer than two fetch windows (2 s each)."""
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=29,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=["p4c5ab12-0-deadbeef"],
        delivered_before_kill=0,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=3.0,
    )
    assert any("less than two fetch windows" in failure for failure in failures)


def test_kill_mid_drain_judge_fails_a_wrong_missing_message():
    """The drain pops oldest first, so the withheld reply is the first."""
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=29,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=["p4c5ab12-7-deadbeef"],
        delivered_before_kill=0,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=7.5,
    )
    assert any("oldest first" in failure for failure in failures)


def test_kill_mid_drain_judge_fails_something_delivered_or_preserved():
    """Nothing may be delivered before the kill or preserved afterwards."""
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=29,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=["p4c5ab12-0-deadbeef"],
        delivered_before_kill=2,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=7.5,
    )
    assert any("delivered before the kill" in failure for failure in failures)
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=28,
        duplicate_wakes=0,
        preserved_distinct=1,
        missing=["p4c5ab12-0-deadbeef"],
        delivered_before_kill=0,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=7.5,
    )
    assert any("still queued at the companion" in failure for failure in failures)


def test_kill_mid_drain_judge_fails_duplicates_and_shortfalls():
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=28,
        duplicate_wakes=1,
        preserved_distinct=0,
        missing=["p4c5ab12-0-deadbeef"],
        delivered_before_kill=0,
        queue_samples=[29, 29, 29, 29],
        hold_seconds=7.5,
    )
    assert any("duplicate wake" in failure for failure in failures)
    assert any("!= 29 of 30" in failure for failure in failures)


def test_kill_mid_drain_judge_fails_no_accepted_messages():
    failures = matrix.kill_mid_drain_failures(
        accepted=0,
        delivered_distinct=0,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=[],
        delivered_before_kill=0,
        queue_samples=[],
        hold_seconds=None,
    )
    assert failures == ["the peer sent no accepted message"]


BACKLOG_MARKERS = {f"p4c2b-{i}-aa" for i in range(4)}
LIVE_MARKERS = {f"p4c2l-{i}-bb" for i in range(2)}

# The REAL recorded shape of the failed wave17 run, kept as the regression
# fixture: all 40 backlog positions first (0-39), then all 20 live (40-59),
# and the live batch's first accepted send 9.7 s AFTER the drain had ended.
FAILED_RUN_LAST_BACKLOG_ARRIVAL = 1764491594.894445  # 2026-11-27T01:53:14.894445Z
FAILED_RUN_FIRST_LIVE_ACCEPTED = 1764491597.454345  # 2026-11-27T01:53:17.454345Z
FAILED_RUN_FIRST_BACKLOG_ARRIVAL = FAILED_RUN_LAST_BACKLOG_ARRIVAL - 6.4


def judge_c2(**overrides):
    """Run the backlog-with-live judge with a FIFO-shaped measured run.

    The default shape is the DESIGNED run: overlap in time (two live sends
    accepted before the last backlog delivery) with the recorded arrival
    order still backlog-first, exactly as the FIFO queue must behave.
    """
    kwargs = {
        "delivered_distinct": 6,
        "expected_distinct": 6,
        "duplicate_arrivals": 0,
        "arrival_order": [
            "p4c2b-0-aa",
            "p4c2b-1-aa",
            "p4c2b-2-aa",
            "p4c2b-3-aa",
            "p4c2l-0-bb",
            "p4c2l-1-bb",
        ],
        "backlog_markers": BACKLOG_MARKERS,
        "live_markers": LIVE_MARKERS,
        # The drain's recorded span (~6.4 s for the wave17 backlog); the
        # live batch is accepted inside it with the required 5 s margin
        # (first live acceptance 5.9 s before the last backlog arrival).
        "backlog_arrival_stamps": [
            FAILED_RUN_FIRST_BACKLOG_ARRIVAL + i * 2.0 for i in range(4)
        ],
        "live_accepted_stamps": [
            FAILED_RUN_FIRST_BACKLOG_ARRIVAL + 0.5,
            FAILED_RUN_FIRST_BACKLOG_ARRIVAL + 1.0,
        ],
    }
    kwargs.update(overrides)
    return matrix.backlog_with_live_failures(**kwargs)


def test_backlog_with_live_judge_passes_overlap_in_time_with_fifo_order():
    """Live accepted during the drain, arrivals still backlog-first: PASS."""
    assert judge_c2() == []


def test_backlog_with_live_judge_passes_with_overlap_and_backlog_first_order():
    """The whole live batch accepted before the last backlog delivery."""
    failures = judge_c2(
        live_accepted_stamps=[
            FAILED_RUN_FIRST_BACKLOG_ARRIVAL + 1.0,
            FAILED_RUN_FIRST_BACKLOG_ARRIVAL + 2.0,
        ],
    )
    assert failures == []


def test_the_failed_runs_real_shape_fails_for_lack_of_overlap():
    """The wave17 shape: every live send accepted after the drain ended."""
    failures = judge_c2(
        live_accepted_stamps=[
            FAILED_RUN_FIRST_LIVE_ACCEPTED,
            FAILED_RUN_FIRST_LIVE_ACCEPTED + 29.0,
        ],
    )
    assert failures != []
    assert any(
        "not prove live traffic overlapped the drain in time" in f for f in failures
    )
    assert any("less than the required 5 s margin" in f for f in failures)
    assert not any("out-of-order delivery" in f for f in failures)


def test_an_overlap_shorter_than_the_margin_fails():
    """Overlapping by less than the required 5 s margin is not a pass."""
    failures = judge_c2(
        live_accepted_stamps=[
            FAILED_RUN_LAST_BACKLOG_ARRIVAL - 1.0,
            FAILED_RUN_LAST_BACKLOG_ARRIVAL - 0.5,
        ],
    )
    assert any("less than the required 5 s margin" in f for f in failures)
    assert not any("out-of-order delivery" in f for f in failures)


def test_the_overlap_report_carries_the_margin_values():
    report = matrix.backlog_live_overlap_report(
        [FAILED_RUN_FIRST_BACKLOG_ARRIVAL, FAILED_RUN_LAST_BACKLOG_ARRIVAL],
        [FAILED_RUN_FIRST_BACKLOG_ARRIVAL + 0.5],
    )
    assert report["overlap_seconds"] == pytest.approx(
        FAILED_RUN_LAST_BACKLOG_ARRIVAL - FAILED_RUN_FIRST_BACKLOG_ARRIVAL - 0.5
    )
    assert report["required_margin_s"] == 5.0
    assert report["overlap_margin_ok"] is True


def test_the_overlap_report_carries_a_negative_overlap_when_too_late():
    report = matrix.backlog_live_overlap_report(
        [FAILED_RUN_FIRST_BACKLOG_ARRIVAL, FAILED_RUN_LAST_BACKLOG_ARRIVAL],
        [FAILED_RUN_FIRST_LIVE_ACCEPTED],
    )
    assert report["overlap_in_time"] is False
    assert report["overlap_seconds"] == pytest.approx(-2.5599)
    assert report["overlap_margin_ok"] is False


def test_the_overlap_report_names_the_recorded_facts():
    report = matrix.backlog_live_overlap_report(
        [FAILED_RUN_FIRST_BACKLOG_ARRIVAL, FAILED_RUN_LAST_BACKLOG_ARRIVAL],
        [FAILED_RUN_FIRST_LIVE_ACCEPTED, FAILED_RUN_FIRST_LIVE_ACCEPTED + 1.0],
    )
    assert report["overlap_in_time"] is False
    assert report["live_sends_before_last_backlog"] == 0
    assert report["backlog_arrivals"] == 2
    assert report["live_sends"] == 2
    assert report["first_backlog_arrival_at"] == matrix.stamp_to_iso(
        FAILED_RUN_FIRST_BACKLOG_ARRIVAL
    )
    assert report["last_backlog_arrival_at"] == matrix.stamp_to_iso(
        FAILED_RUN_LAST_BACKLOG_ARRIVAL
    )


def test_a_live_marker_ahead_of_an_older_backlog_marker_is_out_of_order():
    """A live arrival recorded before the last backlog marker is a defect."""
    failures = judge_c2(
        arrival_order=[
            "p4c2b-0-aa",
            "p4c2b-1-aa",
            "p4c2l-0-bb",
            "p4c2b-2-aa",
            "p4c2b-3-aa",
            "p4c2l-1-bb",
        ],
    )
    assert any("ahead of an older backlog message" in failure for failure in failures)
    assert any("p4c2l-0-bb" in failure for failure in failures)


def test_the_order_report_flags_live_markers_ahead_of_the_backlog():
    report = matrix.backlog_live_order_report(
        [
            "p4c2b-0-aa",
            "p4c2l-0-bb",
            "p4c2b-1-aa",
            "p4c2b-2-aa",
            "p4c2b-3-aa",
            "p4c2l-1-bb",
        ],
        BACKLOG_MARKERS,
        LIVE_MARKERS,
    )
    assert report["order_preserved"] is False
    assert report["live_markers_ahead_of_backlog"] == ["p4c2l-0-bb"]
    assert report["last_backlog_position"] == 4


def test_backlog_with_live_judge_fails_without_recorded_stamps():
    """No timestamps, no overlap proof: the case is not a pass."""
    failures = judge_c2(backlog_arrival_stamps=[], live_accepted_stamps=[])
    assert any(
        "not prove live traffic overlapped the drain in time" in f for f in failures
    )
    assert any("less than the required 5 s margin" in f for f in failures)


def test_backlog_with_live_judge_fails_a_shortfall():
    failures = judge_c2(delivered_distinct=5, expected_distinct=6)
    assert any("recorded 5 distinct nonces, expected 6" in f for f in failures)


def test_backlog_with_live_judge_fails_duplicates():
    failures = judge_c2(duplicate_arrivals=1)
    assert any("duplicate arrivals" in failure for failure in failures)


def test_start_splice_passes_the_trace_environment(tmp_path, monkeypatch):
    """Both splice starts run the proxy with the forwarded-frame trace
    ALWAYS on: no case may start a splice without it."""
    commands: list[list[str]] = []

    def fake_run(cmd, timeout=300):
        commands.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(matrix, "run", fake_run)
    monkeypatch.setattr(matrix, "FAULT_STATE_DIR", tmp_path / "fault-state")
    rig = matrix.Matrix(tmp_path / "evidence", "openhop-bridge")
    rig.start_splice({"delay_ms": 900})
    run_cmd = next(cmd for cmd in commands if cmd[:2] == ["docker", "run"])
    assert ["-e", "OPENHOP_FAULT_PROXY_TRACE=1"] in [
        run_cmd[index : index + 2] for index in range(len(run_cmd) - 1)
    ]


# ---------------------------------------------------------------------------
# The exit contract: 0 only when every case this invocation ran is PASS;
# a FAIL or a NOT RUN makes it 1; --summary-only writes no case and stays 0.
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_when_a_case_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        matrix.case_summary, "bind_source_selection", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        matrix.Matrix,
        "case_c1",
        lambda self: self.write("c1-backlog-120-medre-restart", {"pass": False}),
    )
    assert matrix.main(["--evidence-dir", str(tmp_path / "ev"), "--case", "c1"]) == 1


def test_main_exits_nonzero_when_a_case_was_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        matrix.case_summary, "bind_source_selection", lambda *args, **kwargs: None
    )

    def not_run(self):
        self.write("c2-backlog-with-live", {"pass": False})
        self.results[-1]["status"] = "NOT RUN"

    monkeypatch.setattr(matrix.Matrix, "case_c2", not_run)
    assert matrix.main(["--evidence-dir", str(tmp_path / "ev2"), "--case", "c2"]) == 1


def test_main_exits_zero_when_every_ran_case_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        matrix.case_summary, "bind_source_selection", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        matrix.Matrix,
        "case_c3",
        lambda self: self.write("c3-backlog-fifo-order", {"pass": True}),
    )
    assert matrix.main(["--evidence-dir", str(tmp_path / "ev3"), "--case", "c3"]) == 0


def test_summary_only_exits_zero_without_running_cases(tmp_path):
    assert matrix.main(["--evidence-dir", str(tmp_path / "ev4"), "--summary-only"]) == 0


# ---------------------------------------------------------------------------
# The c5 hold: samples start only once the held state is reached - the
# queue holding exactly accepted - 1 of this run's markers - so
# propagation cannot fail the hold.
# ---------------------------------------------------------------------------


def test_the_hold_state_is_the_backlog_minus_one():
    assert matrix.hold_state_reached(29, 30) is True
    assert matrix.hold_state_reached(30, 30) is False
    assert matrix.hold_state_reached(28, 30) is False
    assert matrix.hold_state_reached(0, 0) is False


def test_shrinking_samples_still_fail_and_the_count_check_still_applies():
    failures = matrix.kill_mid_drain_failures(
        accepted=30,
        delivered_distinct=29,
        duplicate_wakes=0,
        preserved_distinct=0,
        missing=["p4c5ab12-0-deadbeef"],
        delivered_before_kill=0,
        queue_samples=[29, 28, 28, 27],
        hold_seconds=7.5,
    )
    assert any(
        "did not hold still while one message was outstanding" in failure
        for failure in failures
    )
    assert not any("delivered + preserved" in failure for failure in failures)


# ---------------------------------------------------------------------------
# c3's order check runs over the WHOLE recorded arrival order - the same
# report c1 uses - not just the first two arrivals: "no newer message
# ahead of an older one" means any inversion, anywhere.
# ---------------------------------------------------------------------------


def test_c3_order_check_fails_an_inversion_beyond_the_first_two():
    """Indices [0, 1, 3, 2]: the first two are ordered, but 3 arriving
    before 2 is a newer message ahead of an older one."""
    markers = [f"p4c3-{index}-deadbeef" for index in [0, 1, 3, 2]]
    report = matrix.ordering_report(markers)
    assert report["out_of_order_arrivals"] == 1


def test_c3_order_check_passes_a_fully_ordered_sequence():
    markers = [f"p4c3-{index}-deadbeef" for index in range(10)]
    report = matrix.ordering_report(markers)
    assert report["out_of_order_arrivals"] == 0


def test_client_replacement_requires_the_new_client_to_observe_one_packet():
    nonce = "p4c4a1-0-a1b2c3d4"
    send = {"pass": True, "nonces": [nonce]}
    first = {"pass": True, "arrival_count": 0, "disconnected_before_window_end": True}
    second = {
        "pass": True,
        "arrival_count": 1,
        "disconnected_before_window_end": False,
        "arrivals": [{"text": f"sender: {nonce}"}],
    }
    assert matrix.replacement_failures(first, second, send, 1) == []
    assert matrix.replacement_failures(first, {**second, "arrival_count": 0}, send, 1)
    assert matrix.replacement_failures({**first, "arrival_count": 1}, second, send, 1)
    assert matrix.replacement_failures(first, second, send, 0)
