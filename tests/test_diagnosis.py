from datetime import datetime, timezone

from app.models.recovery_attempt import ConfidenceBand, DiagnosisSource
from src.diagnosis import confidence_band, diagnose


def _failed_payment(failure_reason: str, failure_code: str = "GATEWAY_ERROR"):
    from app.models.failed_payment import FailedPayment

    return FailedPayment(
        external_payment_id="pay_test",
        order_id="order_test",
        amount=10000,
        currency="INR",
        failure_code=failure_code,
        failure_reason=failure_reason,
        failure_description="test description",
        failed_at=datetime.now(timezone.utc),
    )


def test_confidence_band_boundaries():
    assert confidence_band(0.90) == ConfidenceBand.HIGH
    assert confidence_band(0.899) == ConfidenceBand.MEDIUM
    assert confidence_band(0.60) == ConfidenceBand.MEDIUM
    assert confidence_band(0.599) == ConfidenceBand.LOW


def test_known_transient_reason_is_high_confidence_retryable():
    diagnosis = diagnose(_failed_payment("issuer_timeout"))
    assert diagnosis.root_cause == "issuer_timeout"
    assert diagnosis.confidence_band == ConfidenceBand.HIGH
    assert diagnosis.retryable is True
    assert diagnosis.never_auto is False
    assert diagnosis.source == DiagnosisSource.RULE_BASED


def test_known_non_retryable_reason():
    diagnosis = diagnose(_failed_payment("expired_card"))
    assert diagnosis.root_cause == "expired_card"
    assert diagnosis.confidence_band == ConfidenceBand.HIGH
    assert diagnosis.retryable is False


def test_risk_block_is_flagged_never_auto():
    diagnosis = diagnose(_failed_payment("payment_blocked_risk"))
    assert diagnosis.never_auto is True
    assert diagnosis.confidence_band == ConfidenceBand.HIGH  # confident it's a risk block, just never auto-actioned


def test_unfamiliar_reason_falls_back_to_low_confidence_unclassified():
    diagnosis = diagnose(_failed_payment("some_reason_no_rule_recognizes"))
    assert diagnosis.root_cause == "unclassified_failure"
    assert diagnosis.confidence_band == ConfidenceBand.LOW
    assert diagnosis.retryable is False
    assert "no rule matches" in diagnosis.evidence.lower()
