"""Deterministic, rule-based diagnosis engine for a failed payment.

Day-2 scope: rules only. A Claude-backed fallback for failure reasons this
table doesn't recognize is later work (README build order, step 3) — until
then, an unrecognized reason gets an explicit low-confidence "unclassified"
diagnosis rather than a rule-table guess dressed up as certainty.
"""

from dataclasses import dataclass

from app.models.failed_payment import FailedPayment
from app.models.recovery_attempt import ConfidenceBand, DiagnosisSource

HIGH_CONFIDENCE_THRESHOLD = 0.90
MEDIUM_CONFIDENCE_THRESHOLD = 0.60

FALLBACK_ROOT_CAUSE = "unclassified_failure"
FALLBACK_CONFIDENCE = 0.50  # deliberately below MEDIUM_CONFIDENCE_THRESHOLD: an unmatched reason must never look confident


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
    confidence: float  # raw score; nothing downstream of the policy engine may read this directly — only confidence_band may
    confidence_band: ConfidenceBand
    evidence: str
    source: DiagnosisSource
    retryable: bool
    never_auto: bool


def diagnose(failed_payment: FailedPayment) -> Diagnosis:
    rule = RULE_TABLE.get(failed_payment.failure_reason)

    if rule is not None:
        evidence = (
            f"Rule table match on failure_reason='{failed_payment.failure_reason}' "
            f"(code={failed_payment.failure_code}): {failed_payment.failure_description}"
        )
        return Diagnosis(
            root_cause=rule.root_cause,
            confidence=rule.confidence,
            confidence_band=confidence_band(rule.confidence),
            evidence=evidence,
            source=DiagnosisSource.RULE_BASED,
            retryable=rule.retryable,
            never_auto=rule.never_auto,
        )

    evidence = (
        f"No rule matches failure_reason='{failed_payment.failure_reason}' "
        f"(code={failed_payment.failure_code}): {failed_payment.failure_description}. "
        "Claude-backed diagnosis for unfamiliar reasons is not yet integrated (build-order step 3); "
        "treated as low-confidence/unclassified rather than guessed."
    )
    return Diagnosis(
        root_cause=FALLBACK_ROOT_CAUSE,
        confidence=FALLBACK_CONFIDENCE,
        confidence_band=confidence_band(FALLBACK_CONFIDENCE),
        evidence=evidence,
        source=DiagnosisSource.RULE_BASED,
        retryable=False,
        never_auto=False,
    )
