"""Diagnosis engine entrypoint for a failed payment: deterministic rules
first, a Claude-backed diagnosis (src/llm_diagnosis.py) for anything the
rule table doesn't recognize.

The LLM is advisory, not authoritative: it may only ever produce a
diagnosis (root_cause, confidence, evidence). It cannot choose an action,
cannot see policy inputs (DND, retry counts, contact limits, ground truth),
and never touches Razorpay. The policy engine (src/policy.py) remains the
sole decision authority regardless of which diagnosis source produced the
input.
"""

from dataclasses import dataclass
from typing import Any

from app.models.failed_payment import FailedPayment
from app.models.recovery_attempt import ConfidenceBand, DiagnosisSource

HIGH_CONFIDENCE_THRESHOLD = 0.85
MEDIUM_CONFIDENCE_THRESHOLD = 0.60


@dataclass(frozen=True)
class Rule:
    root_cause: str
    confidence: float
    retryable: bool
    never_auto: bool = False  # e.g. risk-engine blocks: never auto-act, regardless of confidence


RULE_TABLE: dict[str, Rule] = {
    "issuer_timeout": Rule("issuer_timeout", 0.97, retryable=True),
    "insufficient_funds": Rule("insufficient_funds", 0.93, retryable=True),
    "incorrect_otp": Rule("customer_authentication_error", 0.95, retryable=True),
    "expired_card": Rule("expired_card", 0.98, retryable=False),
    "invalid_cvv": Rule("invalid_card_details", 0.96, retryable=False),
    "card_declined_do_not_honor": Rule("permanent_card_decline", 0.95, retryable=False),
    "authentication_abandoned": Rule("customer_dropped_authentication", 0.88, retryable=True),
    "payment_blocked_risk": Rule("risk_engine_block", 0.92, retryable=False, never_auto=True),
}


def confidence_band(confidence: float) -> ConfidenceBand:
    if confidence >= HIGH_CONFIDENCE_THRESHOLD:
        return ConfidenceBand.HIGH
    if confidence >= MEDIUM_CONFIDENCE_THRESHOLD:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


@dataclass(frozen=True)
class Diagnosis:
    root_cause: str
    confidence: float  # raw score — the model-reported number. Never treat this as a calibrated
    # probability; nothing downstream of the policy engine may read it directly, only confidence_band may.
    confidence_band: ConfidenceBand
    evidence: str
    source: DiagnosisSource
    retryable: bool
    never_auto: bool


def _diagnose_from_rule(failed_payment: FailedPayment, rule: Rule) -> Diagnosis:
    evidence = (
        f"Rule table match on failure_reason='{failed_payment.failure_reason}' "
        f"(code={failed_payment.failure_code}): {failed_payment.failure_description}"
    )
    return Diagnosis(
        root_cause=rule.root_cause,
        confidence=rule.confidence,
        confidence_band=confidence_band(rule.confidence),
        evidence=evidence,
        source=DiagnosisSource.RULE,
        retryable=rule.retryable,
        never_auto=rule.never_auto,
    )


def diagnose(failed_payment: FailedPayment, *, llm_client: Any | None = None) -> Diagnosis:
    """`llm_client` is an injection seam for tests (a fake Anthropic-like
    client); production code leaves it None and src/llm_diagnosis.py builds
    a real one from ANTHROPIC_API_KEY."""
    rule = RULE_TABLE.get(failed_payment.failure_reason)
    if rule is not None:
        return _diagnose_from_rule(failed_payment, rule)

    # Deferred import: llm_diagnosis.py imports Diagnosis/confidence_band from this module,
    # so importing it at module load time here would create a circular import.
    from src.llm_diagnosis import diagnose_ambiguous_case

    failure_signal = {
        "failure_code": failed_payment.failure_code,
        "failure_reason": failed_payment.failure_reason,
        "failure_description": failed_payment.failure_description,
        "amount": failed_payment.amount,
        "currency": failed_payment.currency,
        "retry_count": failed_payment.retry_count,
    }
    return diagnose_ambiguous_case(failure_signal, client=llm_client)
