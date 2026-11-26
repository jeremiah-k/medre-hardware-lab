"""Offline tests for the fault splice's request-opened reply stall.

The splice is protocol-blind: its request trigger is the EXISTENCE of
client->server traffic, never its meaning.  These tests pin the latch
rules the killed-with-one-in-flight backlog case depends on:

* the first client->server chunk observed while the flag is armed opens
  the stall;
* requests keep passing (observing a request never withholds anything by
  itself);
* replies are withheld only while the flag is still armed and the latch
  is open;
* clearing the flag ends the stall and resets the latch, so disarm and
  re-arm starts a new stall.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "openhop-radio" / "openhop_fault_proxy.py"
)
spec = importlib.util.spec_from_file_location("openhop_fault_proxy", MODULE_PATH)
assert spec and spec.loader
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


@pytest.fixture(autouse=True)
def reset_splice_stats():
    """STATS is module-level state; each test starts from zero."""
    for key in ("request_stall_opens", "request_stall_chunks_withheld"):
        proxy.STATS[key] = 0
    yield


def write_fault(path: Path, fault: dict) -> None:
    path.write_text(json.dumps(fault) + "\n", encoding="utf-8")


def make_stall(tmp_path: Path) -> tuple[proxy.RequestStall, Path]:
    fault_file = tmp_path / "fault.json"
    write_fault(fault_file, {})
    return proxy.RequestStall(str(fault_file)), fault_file


def test_disarmed_stall_ignores_requests_and_forwards_replies(tmp_path):
    stall, fault_file = make_stall(tmp_path)
    stall.observe_request(connection=1)
    assert stall.withholds_replies() is False
    assert proxy.STATS["request_stall_opens"] == 0


def test_armed_stall_opens_on_the_first_request(tmp_path):
    stall, fault_file = make_stall(tmp_path)
    write_fault(fault_file, {"stall_server_to_client_on_request": True})
    stall.observe_request(connection=1)
    assert proxy.STATS["request_stall_opens"] == 1
    assert stall.withholds_replies() is True


def test_second_request_and_repeated_polls_do_not_reopen(tmp_path):
    stall, fault_file = make_stall(tmp_path)
    write_fault(fault_file, {"stall_server_to_client_on_request": True})
    stall.observe_request(connection=1)
    stall.observe_request(connection=1)
    assert proxy.STATS["request_stall_opens"] == 1
    assert stall.withholds_replies() is True
    assert stall.withholds_replies() is True


def test_requests_pass_and_only_replies_are_withheld(tmp_path):
    """Observing a request never withholds anything by itself."""
    stall, fault_file = make_stall(tmp_path)
    write_fault(fault_file, {"stall_server_to_client_on_request": True})
    assert stall.withholds_replies() is False
    stall.observe_request(connection=1)
    assert stall.withholds_replies() is True


def test_clearing_the_flag_ends_the_stall_and_resets_the_latch(tmp_path):
    stall, fault_file = make_stall(tmp_path)
    write_fault(fault_file, {"stall_server_to_client_on_request": True})
    stall.observe_request(connection=1)
    assert stall.withholds_replies() is True
    write_fault(fault_file, {})
    assert stall.withholds_replies() is False
    write_fault(fault_file, {"stall_server_to_client_on_request": True})
    assert stall.withholds_replies() is False
    stall.observe_request(connection=2)
    assert stall.withholds_replies() is True
    assert proxy.STATS["request_stall_opens"] == 2


def test_unreadable_fault_file_never_opens_the_stall(tmp_path):
    stall, fault_file = make_stall(tmp_path)
    fault_file.unlink()
    stall.observe_request(connection=1)
    assert stall.withholds_replies() is False
