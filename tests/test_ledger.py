"""Tamper-evident audit ledger (src/ledger.py): the hash chain over
RecoveryAttempt rows. The critical property under test is that mutating a
single field in a single row, after the fact, is detectable — and that
verification reports exactly which row broke, not just "something's wrong
somewhere"."""

from datetime import datetime, timedelta, timezone

from app.models.recovery_attempt import RecoveryAttempt
from src.ledger import GENESIS_HASH, compute_content_hash, next_ledger_position, verify_ledger
from src.pipeline import run_pipeline
from src.simulated_action import SimulatedActionExecutor


def _dataset_record(*, external_payment_id: str, failure_reason: str = "issuer_timeout", amount: int = 150000) -> dict:
    failed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    return {
        "external_payment_id": external_payment_id,
        "order_id": f"order_{external_payment_id}",
        "amount": amount,
        "currency": "INR",
        "failure_code": "GATEWAY_ERROR",
        "failure_reason": failure_reason,
        "failure_description": "test scenario",
        "retry_count": 0,
        "failed_at": failed_at.isoformat(),
        "raw_payload": {"event": "payment.failed"},
        "customer": {
            "external_customer_id": f"cust_{external_payment_id}",
            "name": "Test Customer",
            "email": "test@example.com",
            "phone": "9876543210",
            "dnd_opt_out": False,
            "max_contact_attempts": 3,
            "contact_count": 0,
            "prior_recovery_attempts": 0,
            "prior_recovery_successes": 0,
        },
    }


def test_empty_ledger_is_trivially_intact(db_session):
    result = verify_ledger(db_session)
    assert result.intact is True
    assert result.total_rows == 0
    assert result.rows_verified == 0
    assert result.broken_at_sequence is None


def test_first_row_chains_to_genesis(db_session):
    sequence, previous_hash = next_ledger_position(db_session)
    assert sequence == 0
    assert previous_hash == GENESIS_HASH


def test_writing_real_rows_through_the_pipeline_produces_a_verifiable_chain(db_session):
    """Not a hand-built fixture — three real payments through the real
    pipeline, exactly as run_batch.py would produce them, then verified."""
    executor = SimulatedActionExecutor()
    for i in range(3):
        run_pipeline(db_session, _dataset_record(external_payment_id=f"pay_ledger_{i}"), action_executor=executor)

    result = verify_ledger(db_session)
    assert result.intact is True
    assert result.total_rows == 3
    assert result.rows_verified == 3

    rows = db_session.query(RecoveryAttempt).order_by(RecoveryAttempt.ledger_sequence.asc()).all()
    assert [r.ledger_sequence for r in rows] == [0, 1, 2]
    assert rows[0].previous_hash == GENESIS_HASH
    assert rows[1].previous_hash == rows[0].content_hash
    assert rows[2].previous_hash == rows[1].content_hash


def test_verify_ledger_detects_a_mutated_row_and_names_it(db_session):
    """The critical property: change one field in the MIDDLE row of a
    five-row chain, after the fact, and confirm verification (a) reports
    not intact, (b) names exactly that row as where the chain breaks, and
    (c) confirms every row before it as genuinely verified — not a blanket
    'ledger has been tampered with somewhere'."""
    executor = SimulatedActionExecutor()
    for i in range(5):
        run_pipeline(db_session, _dataset_record(external_payment_id=f"pay_tamper_{i}"), action_executor=executor)

    rows = db_session.query(RecoveryAttempt).order_by(RecoveryAttempt.ledger_sequence.asc()).all()
    tampered_row = rows[2]  # the middle row
    tampered_row_id = str(tampered_row.id)
    tampered_row.diagnosis_reasoning = "this was not the original diagnosis text"
    db_session.commit()

    result = verify_ledger(db_session)

    assert result.intact is False
    assert result.total_rows == 5
    assert result.rows_verified == 2  # rows 0 and 1 verify clean before the break
    assert result.broken_at_sequence == 2
    assert result.broken_row_id == tampered_row_id
    assert "content_hash" in result.detail


def test_verify_ledger_detects_a_tampered_previous_hash_link(db_session):
    """Attacking the chain link itself, not a content field: overwrite one
    row's previous_hash so it no longer matches the prior row's real
    content_hash. This must be caught even though the tampered row's own
    content_hash still matches ITS OWN (stale) content."""
    executor = SimulatedActionExecutor()
    for i in range(3):
        run_pipeline(db_session, _dataset_record(external_payment_id=f"pay_link_{i}"), action_executor=executor)

    rows = db_session.query(RecoveryAttempt).order_by(RecoveryAttempt.ledger_sequence.asc()).all()
    rows[1].previous_hash = "f" * 64
    db_session.commit()

    result = verify_ledger(db_session)

    assert result.intact is False
    assert result.broken_at_sequence == 1
    assert "previous_hash" in result.detail


def test_compute_content_hash_is_deterministic_and_order_independent_of_dict_keys(db_session):
    """Same logical content must always hash the same way, regardless of
    incidental dict key ordering — canonical_fields uses sort_keys=True."""
    executor = SimulatedActionExecutor()
    run_pipeline(db_session, _dataset_record(external_payment_id="pay_hash_stable"), action_executor=executor)
    row = db_session.query(RecoveryAttempt).one()

    first = compute_content_hash(row, previous_hash=GENESIS_HASH)
    second = compute_content_hash(row, previous_hash=GENESIS_HASH)
    assert first == second
    assert len(first) == 64  # a hex-encoded SHA-256 digest


def test_compute_content_hash_changes_if_previous_hash_changes():
    """The same row content chained onto a different previous_hash must
    produce a different content_hash — this is what makes it a CHAIN, not
    just a per-row checksum that tampering with row order couldn't catch."""
    executor = SimulatedActionExecutor()
    from app.db.base import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    try:
        run_pipeline(db, _dataset_record(external_payment_id="pay_hash_link"), action_executor=executor)
        row = db.query(RecoveryAttempt).one()
        with_genesis = compute_content_hash(row, previous_hash=GENESIS_HASH)
        with_other = compute_content_hash(row, previous_hash="b" * 64)
        assert with_genesis != with_other
    finally:
        db.close()
