import sqlite3

import pytest

from medre_lab.worker import verify_receipts


@pytest.fixture
def evidence(tmp_path):
    db = sqlite3.connect(tmp_path / "state.sqlite")
    db.execute("CREATE TABLE event_body_content(event_id TEXT,body TEXT)")
    db.execute(
        "CREATE TABLE delivery_receipts(event_id TEXT,target_adapter TEXT,status TEXT,failure_kind TEXT)"
    )
    db.execute("INSERT INTO event_body_content VALUES('event-1','nonce-1')")
    db.execute("INSERT INTO delivery_receipts VALUES('event-1','lxmf','sent',NULL)")
    db.commit()
    db.close()
    return tmp_path, [dict(nonce="nonce-1", destination="lxmf")]


def test_exact_event_and_destination_receipt(evidence):
    run, legs = evidence
    verify_receipts(run, legs)
    assert legs[0]["event_id"] == "event-1" and legs[0]["sent_receipt"]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO event_body_content VALUES('duplicate-event','nonce-1')",
        "UPDATE delivery_receipts SET target_adapter='other'",
        "UPDATE delivery_receipts SET status='queued'",
        "INSERT INTO delivery_receipts VALUES('event-1','lxmf','failed','permanent')",
    ],
)
def test_incomplete_or_ambiguous_evidence_cannot_pass(evidence, statement):
    run, legs = evidence
    with sqlite3.connect(run / "state.sqlite") as db:
        db.execute(statement)
    with pytest.raises(AssertionError):
        verify_receipts(run, legs)
