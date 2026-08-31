import uuid
from datetime import datetime, timezone

from app.models.customer import Customer
from app.models.failed_payment import FailedPayment, FailedPaymentStatus
from app.models.recovery_attempt import (
    ActionMode,
    ActionResult,
    ConfidenceBand,
    DecisionAction,
    DiagnosisSource,
    RecoveryAttempt,
)


def _make_customer(**overrides) -> Customer:
    defaults = dict(
        id=uuid.uuid4(),
        external_customer_id=f"cust_{uuid.uuid4().hex[:8]}",
        name="Asha Rao",
        email="asha@example.com",
        phone="9876543210",
        dnd_opt_out=False,
        max_contact_attempts=3,
        contact_count=0,
        prior_recovery_attempts=0,
        prior_recovery_successes=0,
    )
    defaults.update(overrides)
    return Customer(**defaults)


def _make_failed_payment(customer: Customer, **overrides) -> FailedPayment:
    defaults = dict(
        id=uuid.uuid4(),
        external_payment_id=f"pay_{uuid.uuid4().hex[:8]}",
        order_id=f"order_{uuid.uuid4().hex[:8]}",
        customer_id=customer.id,
        amount=50000,
        currency="INR",
        failure_code="GATEWAY_ERROR",
        failure_reason="issuer_timeout",
        failure_description="The bank did not respond in time.",
        raw_payload={"event": "payment.failed"},
        failed_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return FailedPayment(**defaults)


def test_customer_round_trip(db_session):
    customer = _make_customer()
    db_session.add(customer)
    db_session.commit()

    fetched = db_session.get(Customer, customer.id)
    assert fetched is not None
    assert fetched.email == "asha@example.com"
    assert fetched.dnd_opt_out is False


def test_customer_history_and_compliance_fields(db_session):
    customer = _make_customer(dnd_opt_out=True, prior_recovery_attempts=6, prior_recovery_successes=0)
    db_session.add(customer)
    db_session.commit()

    fetched = db_session.get(Customer, customer.id)
    assert fetched.dnd_opt_out is True
    assert fetched.prior_recovery_attempts == 6
    assert fetched.prior_recovery_successes == 0


def test_failed_payment_defaults_and_relationship(db_session):
    customer = _make_customer()
    db_session.add(customer)
    db_session.flush()

    payment = _make_failed_payment(customer)
    db_session.add(payment)
    db_session.commit()

    fetched = db_session.get(FailedPayment, payment.id)
    assert fetched.status == FailedPaymentStatus.OPEN  # column default applies
    assert fetched.retry_count == 0
    assert fetched.stop_reason is None
    assert fetched.customer.id == customer.id
    assert fetched in customer.failed_payments


def test_recovery_attempt_is_the_audit_trail(db_session):
    customer = _make_customer()
    db_session.add(customer)
    db_session.flush()

    payment = _make_failed_payment(customer)
    db_session.add(payment)
    db_session.flush()

    attempt = RecoveryAttempt(
        id=uuid.uuid4(),
        failed_payment_id=payment.id,
        attempt_number=1,
        diagnosis_root_cause="issuer_timeout",
        diagnosis_source=DiagnosisSource.RULE,
        model_reported_confidence=None,
        confidence_band=ConfidenceBand.HIGH,
        diagnosis_reasoning="Rule table match: GATEWAY_ERROR/issuer_timeout is a known transient failure.",
        decision_action=DecisionAction.RETRY,
        decision_factors={"payment_value": payment.amount, "confidence_band": "high", "attempt_count": 1},
        action_mode=ActionMode.SIMULATED,
        action_result=ActionResult.PENDING,
        ledger_sequence=0,
        previous_hash="0" * 64,
        content_hash="a" * 64,
    )
    db_session.add(attempt)
    db_session.commit()

    fetched = db_session.get(RecoveryAttempt, attempt.id)
    assert fetched.confidence_band == ConfidenceBand.HIGH
    assert fetched.action_mode == ActionMode.SIMULATED  # never defaults silently
    assert fetched.decision_factors["attempt_count"] == 1
    assert fetched in payment.attempts


def test_action_mode_must_be_explicit(db_session):
    """action_mode has no column default: a REAL/SIMULATED label must always
    be supplied by the caller, never inferred."""
    customer = _make_customer()
    db_session.add(customer)
    db_session.flush()
    payment = _make_failed_payment(customer)
    db_session.add(payment)
    db_session.flush()

    attempt = RecoveryAttempt(
        id=uuid.uuid4(),
        failed_payment_id=payment.id,
        attempt_number=1,
        diagnosis_root_cause="expired_card",
        diagnosis_source=DiagnosisSource.RULE,
        confidence_band=ConfidenceBand.HIGH,
        diagnosis_reasoning="Rule table match: non-retryable card failure.",
        decision_action=DecisionAction.PAYMENT_LINK,
        decision_factors={},
        action_mode=ActionMode.REAL,
        action_result=ActionResult.SUCCEEDED,
        razorpay_reference="plink_abc123",
        ledger_sequence=0,
        previous_hash="0" * 64,
        content_hash="a" * 64,
    )
    db_session.add(attempt)
    db_session.commit()

    fetched = db_session.get(RecoveryAttempt, attempt.id)
    assert fetched.action_mode == ActionMode.REAL
    assert fetched.action_result == ActionResult.SUCCEEDED
