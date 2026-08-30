from datetime import datetime, timedelta, timezone

import httpx

from app.models.failed_payment import FailedPaymentStatus
from app.models.recovery_attempt import ActionMode, ActionResult, DecisionAction, RecoveryAttempt
from src.pipeline import run_pipeline
from src.policy import PolicyConfig
from src.razorpay_action import RazorpayActionClient

TEST_KEY_ID = "rzp_test_fake0000000001"
TEST_KEY_SECRET = "fake_secret"


def _dataset_record(
    *,
    external_payment_id: str = "pay_vs_test",
    failure_reason: str = "issuer_timeout",
    failure_code: str = "GATEWAY_ERROR",
    amount: int = 150000,
    retry_count: int = 0,
    hours_ago: int = 2,
    dnd_opt_out: bool = False,
    contact_count: int = 0,
    max_contact_attempts: int = 3,
) -> dict:
    failed_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "external_payment_id": external_payment_id,
        "order_id": f"order_{external_payment_id}",
        "amount": amount,
        "currency": "INR",
        "failure_code": failure_code,
        "failure_reason": failure_reason,
        "failure_description": "test scenario",
        "retry_count": retry_count,
        "failed_at": failed_at.isoformat(),
        "raw_payload": {"event": "payment.failed"},
        "customer": {
            "external_customer_id": f"cust_{external_payment_id}",
            "name": "Test Customer",
            "email": "test@example.com",
            "phone": "9876543210",
            "dnd_opt_out": dnd_opt_out,
            "max_contact_attempts": max_contact_attempts,
            "contact_count": contact_count,
            "prior_recovery_attempts": 0,
            "prior_recovery_successes": 0,
        },
    }


def _real_razorpay_mock() -> RazorpayActionClient:
    """A client that looks configured and hits the REAL code path, but
    against a mocked transport — no live network call, but the real request
    construction / response handling runs."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/orders":
            return httpx.Response(200, json={"id": "order_mock123", "status": "created"})
        if request.url.path == "/v1/payment_links":
            return httpx.Response(200, json={"id": "plink_mock123", "short_url": "https://rzp.io/x/mock"})
        raise AssertionError(f"unexpected request to {request.url.path}")

    return RazorpayActionClient(key_id=TEST_KEY_ID, key_secret=TEST_KEY_SECRET, transport=httpx.MockTransport(handler))


def test_successful_retry_path(db_session):
    """Transient failure, no compliance blocks, first attempt: policy approves a
    retry and the Razorpay Orders API call succeeds (REAL, PENDING — see
    'no fake recovered result' below for why not SUCCEEDED)."""
    record = _dataset_record(failure_reason="issuer_timeout")

    result = run_pipeline(db_session, record, action_executor=_real_razorpay_mock())

    assert result.policy_decision.action == DecisionAction.RETRY
    assert result.action_outcome.action_mode == ActionMode.REAL
    assert result.action_outcome.action_result == ActionResult.PENDING
    assert result.action_outcome.razorpay_reference == "order_mock123"
    assert result.failed_payment.retry_count == 1  # the attempt was consumed
    assert result.failed_payment.status == FailedPaymentStatus.OPEN  # still in flight, not claimed recovered


def test_policy_refusal_low_confidence_stands_down(db_session):
    """An unfamiliar failure reason gets a low-confidence diagnosis; policy
    refuses to guess and stands down rather than acting or escalating."""
    record = _dataset_record(failure_reason="some_reason_no_rule_recognizes", failure_code="SERVER_ERROR")

    result = run_pipeline(db_session, record, action_executor=_real_razorpay_mock())

    assert result.policy_decision.action == DecisionAction.STAND_DOWN
    assert result.policy_decision.reason == "confidence_below_action_threshold"
    assert result.action_outcome.action_result == ActionResult.NOT_EXECUTED
    assert result.failed_payment.status == FailedPaymentStatus.ESCALATED
    assert result.failed_payment.stop_reason == "confidence_below_action_threshold"


def test_dnd_refusal(db_session):
    """An opted-out customer must never be auto-contacted, regardless of how
    confident and retryable the diagnosis is."""
    record = _dataset_record(failure_reason="issuer_timeout", dnd_opt_out=True)

    result = run_pipeline(db_session, record, action_executor=_real_razorpay_mock())

    assert result.policy_decision.action == DecisionAction.STAND_DOWN
    assert result.policy_decision.reason == "dnd_opt_out"
    assert result.action_outcome.action_result == ActionResult.NOT_EXECUTED
    assert result.failed_payment.status == FailedPaymentStatus.ESCALATED
    assert result.failed_payment.stop_reason == "dnd_opt_out"
    # No Razorpay call should have been reachable for this path at all.
    assert result.action_outcome.action_mode == ActionMode.SIMULATED


def test_retry_cap_refusal(db_session):
    """A payment that already hit the max-attempt cap must be stood down even
    though the underlying diagnosis still looks easy and retryable."""
    record = _dataset_record(failure_reason="issuer_timeout", retry_count=3)

    result = run_pipeline(db_session, record, action_executor=_real_razorpay_mock(), policy_config=PolicyConfig(max_attempts=3))

    assert result.policy_decision.action == DecisionAction.STAND_DOWN
    assert result.policy_decision.reason == "max_attempts_reached"
    assert result.failed_payment.status == FailedPaymentStatus.UNRESOLVED
    assert result.failed_payment.stop_reason == "max_attempts_reached"
    assert result.failed_payment.retry_count == 3  # not incremented — no attempt was actually made


def test_audit_record_creation(db_session):
    """Every run appends exactly one immutable RecoveryAttempt row carrying
    the full diagnosis/decision/action/result trail."""
    record = _dataset_record(failure_reason="expired_card")

    result = run_pipeline(db_session, record, action_executor=_real_razorpay_mock())

    stored = db_session.get(RecoveryAttempt, result.recovery_attempt.id)
    assert stored is not None
    assert stored.failed_payment_id == result.failed_payment.id
    assert stored.attempt_number == 1
    assert stored.diagnosis_root_cause == "expired_card"
    assert stored.confidence_band == result.diagnosis.confidence_band
    assert stored.decision_action == DecisionAction.PAYMENT_LINK
    assert stored.action_mode == ActionMode.REAL
    assert stored.action_result == ActionResult.PENDING
    assert stored.razorpay_reference == "plink_mock123"
    assert stored.created_at is not None
    assert stored.diagnosis_reasoning  # evidence is non-empty
    assert isinstance(stored.decision_factors, dict) and stored.decision_factors  # factors snapshot persisted

    # Running it again on the same payment appends a second row rather than mutating the first.
    result2 = run_pipeline(db_session, record, action_executor=_real_razorpay_mock(), policy_config=PolicyConfig(cooldown_seconds=0))
    assert result2.recovery_attempt.id != result.recovery_attempt.id
    assert result2.recovery_attempt.attempt_number == 2
    still_there = db_session.get(RecoveryAttempt, result.recovery_attempt.id)
    assert still_there.action_result == ActionResult.PENDING  # untouched by the second run
    assert still_there.attempt_number == 1  # the first row's own attempt_number is also unchanged


def test_no_fake_recovered_result(db_session):
    """Across every decision path this pipeline can reach today, the system
    must never claim CONFIRMED_RECOVERED / SIMULATED_RECOVERED or
    ActionResult.SUCCEEDED — those require a real payment-completion
    confirmation this build doesn't have yet (see razorpay_action.py)."""
    scenarios = [
        _dataset_record(external_payment_id="pay_a", failure_reason="issuer_timeout"),  # would-be "successful" retry
        _dataset_record(external_payment_id="pay_b", failure_reason="expired_card"),  # would-be "successful" payment link
        _dataset_record(external_payment_id="pay_c", failure_reason="unfamiliar_thing", failure_code="SERVER_ERROR"),
        _dataset_record(external_payment_id="pay_d", dnd_opt_out=True),
        _dataset_record(external_payment_id="pay_e", retry_count=3),
    ]

    for record in scenarios:
        result = run_pipeline(db_session, record, action_executor=_real_razorpay_mock())
        assert result.action_outcome.action_result != ActionResult.SUCCEEDED
        assert result.failed_payment.status not in (
            FailedPaymentStatus.CONFIRMED_RECOVERED,
            FailedPaymentStatus.SIMULATED_RECOVERED,
        )
