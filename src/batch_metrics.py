"""Computes batch-evaluation metrics from a list of processed records. Pure
functions, no I/O and no DB session — src/run_batch.py handles orchestration
and printing.

Every processed record is classified into exactly one of three mutually
exclusive buckets by its decision reason, so the buckets always sum to the
total record count:

  AUTO_ACTION     decision.reason == "high_confidence_auto_action"
                  (RETRY or PAYMENT_LINK was actually attempted)
  SAFETY_STOP     max_attempts_reached / cooldown_not_elapsed / dnd_opt_out /
                  contact_limit_reached / recovery_not_cost_effective —
                  mechanical stopping rules and compliance gates, grouped
                  together per the original spec's own "Stopping rules ...
                  Compliance" pairing
  POLICY_REFUSAL  risk_block_requires_human_review / medium_confidence_human_review /
                  high_value_uncertain_escalation / serial_recovery_failure_history /
                  confidence_below_action_threshold — a human-judgment-shaped
                  refusal to auto-act, whether routed to HUMAN_REVIEW or STAND_DOWN

"Incorrect automatic actions" compares every AUTO_ACTION record's actual
decision against ground_truth's expected_action. This is the safety proof:
target is always 0. If it's not zero, the batch report says so loudly rather
than averaging it away.
"""

from dataclasses import dataclass, field

from app.models.recovery_attempt import ActionMode, ActionResult, DecisionAction, DiagnosisSource

SAFETY_STOP_REASONS = {
    "max_attempts_reached",
    "cooldown_not_elapsed",
    "dnd_opt_out",
    "contact_limit_reached",
    "recovery_not_cost_effective",
}
AUTO_ACTION_REASON = "high_confidence_auto_action"
AUTO_ACTIONS = (DecisionAction.RETRY, DecisionAction.PAYMENT_LINK)


@dataclass(frozen=True)
class RecordOutcome:
    """One processed record's fields relevant to metrics, decoupled from the
    ORM/DB session."""

    external_payment_id: str
    amount: int
    diagnosis_root_cause: str
    diagnosis_source: DiagnosisSource
    confidence_band: str  # "high" | "medium" | "low"
    decision_action: DecisionAction
    decision_reason: str
    action_mode: ActionMode
    action_result: ActionResult


@dataclass(frozen=True)
class IncorrectAutomaticAction:
    external_payment_id: str
    actual_action: str
    expected_action: str
    template_key: str


@dataclass(frozen=True)
class BatchMetrics:
    total_records: int

    # The required table, in order.
    revenue_at_risk_events: int
    confirmed_recovered_amount: int
    recovery_rate: float
    auto_recovery_attempts: int
    successful_recoveries: int
    policy_refusals_escalated: int
    unresolved: int
    stopped_by_safety_rules: int
    incorrect_automatic_actions: int
    max_retry_attempts_allowed: int

    # Additional required stats.
    rule_diagnosed_count: int
    llm_diagnosed_count: int
    llm_fallback_count: int
    high_confidence_count: int
    medium_confidence_count: int
    low_confidence_count: int
    simulated_action_count: int
    real_action_count: int
    pending_unconfirmed_count: int

    # Evaluation section.
    human_review_rate: float
    unresolved_rate: float
    safety_stop_rate: float
    incorrect_automatic_action_rate: float
    llm_usage_rate: float
    llm_fallback_rate: float
    rule_based_diagnosis_accuracy: float | None  # None if there were no rule-based records to score
    rule_based_diagnosis_total: int

    revenue_at_risk_amount: int
    simulated_recovered_amount: int
    simulated_recovered_count: int
    confirmed_recovered_count: int
    simulated_recovery_rate: float

    incorrect_automatic_action_details: list[IncorrectAutomaticAction] = field(default_factory=list)


def _classify_bucket(reason: str) -> str:
    if reason == AUTO_ACTION_REASON:
        return "auto_action"
    if reason in SAFETY_STOP_REASONS:
        return "safety_stop"
    return "policy_refusal"


def compute_metrics(outcomes: list[RecordOutcome], ground_truth: dict, *, max_attempts: int) -> BatchMetrics:
    total = len(outcomes)
    revenue_at_risk_amount = sum(o.amount for o in outcomes)

    confirmed_recovered = [o for o in outcomes if o.action_result == ActionResult.SUCCEEDED and o.action_mode == ActionMode.REAL]
    simulated_recovered = [
        o for o in outcomes if o.action_result == ActionResult.SUCCEEDED and o.action_mode == ActionMode.SIMULATED
    ]

    auto_action_outcomes = [o for o in outcomes if _classify_bucket(o.decision_reason) == "auto_action"]
    safety_stop_outcomes = [o for o in outcomes if _classify_bucket(o.decision_reason) == "safety_stop"]
    policy_refusal_outcomes = [o for o in outcomes if _classify_bucket(o.decision_reason) == "policy_refusal"]

    unresolved_outcomes = [o for o in auto_action_outcomes if o.action_result != ActionResult.SUCCEEDED]

    incorrect_details: list[IncorrectAutomaticAction] = []
    for o in auto_action_outcomes:
        gt = ground_truth.get(o.external_payment_id)
        if gt is None:
            continue
        expected = gt["expected_action"]
        if expected != o.decision_action.value:
            incorrect_details.append(
                IncorrectAutomaticAction(
                    external_payment_id=o.external_payment_id,
                    actual_action=o.decision_action.value,
                    expected_action=expected,
                    template_key=gt.get("template_key", "?"),
                )
            )

    rule_outcomes = [o for o in outcomes if o.diagnosis_source == DiagnosisSource.RULE]
    llm_outcomes = [o for o in outcomes if o.diagnosis_source == DiagnosisSource.LLM]
    llm_fallback_outcomes = [o for o in outcomes if o.diagnosis_source == DiagnosisSource.LLM_FALLBACK]

    rule_correct = 0
    for o in rule_outcomes:
        gt = ground_truth.get(o.external_payment_id)
        if gt is not None and gt.get("expected_root_cause") == o.diagnosis_root_cause:
            rule_correct += 1
    rule_based_diagnosis_accuracy = (rule_correct / len(rule_outcomes)) if rule_outcomes else None

    real_actions = [o for o in outcomes if o.action_mode == ActionMode.REAL]
    simulated_actions = [o for o in outcomes if o.action_mode == ActionMode.SIMULATED]
    pending_unconfirmed = [o for o in outcomes if o.action_result == ActionResult.PENDING]

    def rate(n: int, d: int) -> float:
        return (n / d) if d else 0.0

    return BatchMetrics(
        total_records=total,
        revenue_at_risk_events=total,
        confirmed_recovered_amount=sum(o.amount for o in confirmed_recovered),
        recovery_rate=rate(sum(o.amount for o in confirmed_recovered), revenue_at_risk_amount),
        auto_recovery_attempts=len(auto_action_outcomes),
        successful_recoveries=len(confirmed_recovered) + len(simulated_recovered),
        policy_refusals_escalated=len(policy_refusal_outcomes),
        unresolved=len(unresolved_outcomes),
        stopped_by_safety_rules=len(safety_stop_outcomes),
        incorrect_automatic_actions=len(incorrect_details),
        max_retry_attempts_allowed=max_attempts,
        rule_diagnosed_count=len(rule_outcomes),
        llm_diagnosed_count=len(llm_outcomes),
        llm_fallback_count=len(llm_fallback_outcomes),
        high_confidence_count=sum(1 for o in outcomes if o.confidence_band == "high"),
        medium_confidence_count=sum(1 for o in outcomes if o.confidence_band == "medium"),
        low_confidence_count=sum(1 for o in outcomes if o.confidence_band == "low"),
        simulated_action_count=len(simulated_actions),
        real_action_count=len(real_actions),
        pending_unconfirmed_count=len(pending_unconfirmed),
        human_review_rate=rate(sum(1 for o in outcomes if o.decision_action == DecisionAction.HUMAN_REVIEW), total),
        unresolved_rate=rate(len(unresolved_outcomes), total),
        safety_stop_rate=rate(len(safety_stop_outcomes), total),
        incorrect_automatic_action_rate=rate(len(incorrect_details), total),
        llm_usage_rate=rate(len(llm_outcomes) + len(llm_fallback_outcomes), total),
        llm_fallback_rate=rate(len(llm_fallback_outcomes), total),
        rule_based_diagnosis_accuracy=rule_based_diagnosis_accuracy,
        rule_based_diagnosis_total=len(rule_outcomes),
        revenue_at_risk_amount=revenue_at_risk_amount,
        simulated_recovered_amount=sum(o.amount for o in simulated_recovered),
        simulated_recovered_count=len(simulated_recovered),
        confirmed_recovered_count=len(confirmed_recovered),
        simulated_recovery_rate=rate(sum(o.amount for o in simulated_recovered), revenue_at_risk_amount),
        incorrect_automatic_action_details=incorrect_details,
    )
