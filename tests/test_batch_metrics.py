from app.models.recovery_attempt import ActionMode, ActionResult, DecisionAction, DiagnosisSource
from src.batch_metrics import RecordOutcome, compute_metrics

GT = {
    "pay_retry_ok": {"expected_action": "retry", "expected_root_cause": "issuer_timeout", "template_key": "t1"},
    "pay_plink_ok": {"expected_action": "payment_link", "expected_root_cause": "expired_card", "template_key": "t2"},
    "pay_human": {"expected_action": "human_review", "expected_root_cause": "risk_engine_block", "template_key": "t3"},
    "pay_standdown": {"expected_action": "stand_down", "expected_root_cause": "unclassified_failure", "template_key": "t4"},
    "pay_cap": {"expected_action": "stand_down", "expected_root_cause": "issuer_timeout", "template_key": "t5"},
    "pay_wrong": {"expected_action": "human_review", "expected_root_cause": "issuer_soft_decline", "template_key": "t6"},
}


def _outcome(payment_id: str, **overrides) -> RecordOutcome:
    defaults = dict(
        external_payment_id=payment_id,
        amount=10000,
        diagnosis_root_cause="issuer_timeout",
        diagnosis_source=DiagnosisSource.RULE,
        confidence_band="high",
        decision_action=DecisionAction.RETRY,
        decision_reason="high_confidence_auto_action",
        action_mode=ActionMode.SIMULATED,
        action_result=ActionResult.SUCCEEDED,
    )
    defaults.update(overrides)
    return RecordOutcome(**defaults)


def test_bucket_counts_sum_to_total_records():
    outcomes = [
        _outcome("pay_retry_ok"),
        _outcome("pay_plink_ok", decision_action=DecisionAction.PAYMENT_LINK, diagnosis_root_cause="expired_card"),
        _outcome(
            "pay_human",
            decision_action=DecisionAction.HUMAN_REVIEW,
            decision_reason="risk_block_requires_human_review",
            diagnosis_root_cause="risk_engine_block",
            action_result=ActionResult.NOT_EXECUTED,
        ),
        _outcome(
            "pay_standdown",
            decision_action=DecisionAction.STAND_DOWN,
            decision_reason="confidence_below_action_threshold",
            confidence_band="low",
            diagnosis_root_cause="unclassified_failure",
            action_result=ActionResult.NOT_EXECUTED,
        ),
        _outcome(
            "pay_cap",
            decision_action=DecisionAction.STAND_DOWN,
            decision_reason="max_attempts_reached",
            action_result=ActionResult.NOT_EXECUTED,
        ),
    ]

    metrics = compute_metrics(outcomes, GT, max_attempts=3)

    assert metrics.total_records == 5
    assert metrics.auto_recovery_attempts + metrics.policy_refusals_escalated + metrics.stopped_by_safety_rules == 5
    assert metrics.auto_recovery_attempts == 2  # retry + payment_link
    assert metrics.policy_refusals_escalated == 2  # human review + low-confidence stand-down
    assert metrics.stopped_by_safety_rules == 1  # max_attempts_reached


def test_four_way_outcome_categorization():
    outcomes = [
        _outcome("pay_retry_ok", action_mode=ActionMode.REAL, action_result=ActionResult.PENDING),  # real, unconfirmed
        _outcome("pay_plink_ok", decision_action=DecisionAction.PAYMENT_LINK),  # simulated success
        _outcome(
            "pay_human",
            decision_action=DecisionAction.HUMAN_REVIEW,
            decision_reason="risk_block_requires_human_review",
            action_mode=ActionMode.SIMULATED,
            action_result=ActionResult.NOT_EXECUTED,
        ),
        _outcome(
            "pay_cap",
            decision_action=DecisionAction.STAND_DOWN,
            decision_reason="max_attempts_reached",
            action_mode=ActionMode.SIMULATED,
            action_result=ActionResult.NOT_EXECUTED,
        ),
    ]

    metrics = compute_metrics(outcomes, GT, max_attempts=3)

    assert metrics.confirmed_recovered_count == 0  # REAL+PENDING is not a confirmed recovery
    assert metrics.simulated_recovered_count == 1  # the SIMULATED+SUCCEEDED payment_link
    assert metrics.unresolved == 1  # the REAL+PENDING retry: attempted, not (yet) succeeded
    assert metrics.policy_refusals_escalated == 1
    assert metrics.stopped_by_safety_rules == 1


def test_confirmed_recovered_requires_real_and_succeeded():
    real_pending = _outcome("pay_retry_ok", action_mode=ActionMode.REAL, action_result=ActionResult.PENDING)
    real_succeeded = _outcome("pay_retry_ok", action_mode=ActionMode.REAL, action_result=ActionResult.SUCCEEDED, amount=50000)
    simulated_succeeded = _outcome("pay_retry_ok", action_mode=ActionMode.SIMULATED, action_result=ActionResult.SUCCEEDED, amount=99999)

    metrics_pending = compute_metrics([real_pending], GT, max_attempts=3)
    assert metrics_pending.confirmed_recovered_amount == 0

    metrics_real_success = compute_metrics([real_succeeded], GT, max_attempts=3)
    assert metrics_real_success.confirmed_recovered_amount == 50000

    metrics_simulated = compute_metrics([simulated_succeeded], GT, max_attempts=3)
    assert metrics_simulated.confirmed_recovered_amount == 0  # simulated success is never "confirmed"
    assert metrics_simulated.simulated_recovered_amount == 99999


def test_incorrect_automatic_action_detected_when_action_taken_but_not_expected():
    outcome = _outcome(
        "pay_wrong", decision_action=DecisionAction.RETRY, diagnosis_root_cause="issuer_soft_decline"
    )  # ground truth expects human_review, not retry

    metrics = compute_metrics([outcome], GT, max_attempts=3)

    assert metrics.incorrect_automatic_actions == 1
    assert metrics.incorrect_automatic_action_details[0].external_payment_id == "pay_wrong"
    assert metrics.incorrect_automatic_action_details[0].expected_action == "human_review"


def test_correct_automatic_action_is_not_flagged():
    outcome = _outcome("pay_retry_ok", decision_action=DecisionAction.RETRY)  # ground truth expects retry too
    metrics = compute_metrics([outcome], GT, max_attempts=3)
    assert metrics.incorrect_automatic_actions == 0


def test_mismatched_auto_action_type_is_also_incorrect():
    """Ground truth expects payment_link, system did retry instead — still a diagnostic mismatch worth flagging."""
    outcome = _outcome("pay_plink_ok", decision_action=DecisionAction.RETRY, diagnosis_root_cause="expired_card")
    metrics = compute_metrics([outcome], GT, max_attempts=3)
    assert metrics.incorrect_automatic_actions == 1


def test_non_auto_actions_are_never_flagged_as_incorrect():
    outcome = _outcome(
        "pay_human",
        decision_action=DecisionAction.HUMAN_REVIEW,
        decision_reason="risk_block_requires_human_review",
        action_result=ActionResult.NOT_EXECUTED,
    )
    metrics = compute_metrics([outcome], GT, max_attempts=3)
    assert metrics.incorrect_automatic_actions == 0


def test_rule_based_diagnosis_accuracy_is_deterministic():
    outcomes = [
        _outcome("pay_retry_ok", diagnosis_source=DiagnosisSource.RULE, diagnosis_root_cause="issuer_timeout"),
        _outcome("pay_plink_ok", diagnosis_source=DiagnosisSource.RULE, diagnosis_root_cause="expired_card", decision_action=DecisionAction.PAYMENT_LINK),
    ]
    metrics = compute_metrics(outcomes, GT, max_attempts=3)
    assert metrics.rule_based_diagnosis_accuracy == 1.0
    assert metrics.rule_based_diagnosis_total == 2


def test_rule_based_diagnosis_accuracy_is_none_when_no_rule_records():
    outcome = _outcome("pay_human", diagnosis_source=DiagnosisSource.LLM, decision_action=DecisionAction.HUMAN_REVIEW, action_result=ActionResult.NOT_EXECUTED)
    metrics = compute_metrics([outcome], GT, max_attempts=3)
    assert metrics.rule_based_diagnosis_accuracy is None


def test_llm_and_fallback_counts_and_rates():
    outcomes = [
        _outcome("pay_retry_ok", diagnosis_source=DiagnosisSource.RULE),
        _outcome("pay_human", diagnosis_source=DiagnosisSource.LLM, decision_action=DecisionAction.HUMAN_REVIEW, decision_reason="medium_confidence_human_review", confidence_band="medium", action_result=ActionResult.NOT_EXECUTED),
        _outcome("pay_standdown", diagnosis_source=DiagnosisSource.LLM_FALLBACK, decision_action=DecisionAction.STAND_DOWN, decision_reason="confidence_below_action_threshold", confidence_band="low", action_result=ActionResult.NOT_EXECUTED),
    ]
    metrics = compute_metrics(outcomes, GT, max_attempts=3)
    assert metrics.rule_diagnosed_count == 1
    assert metrics.llm_diagnosed_count == 1
    assert metrics.llm_fallback_count == 1
    assert metrics.llm_usage_rate == (2 / 3)
    assert metrics.llm_fallback_rate == (1 / 3)


def test_max_retry_attempts_allowed_reflects_config():
    metrics = compute_metrics([], GT, max_attempts=5)
    assert metrics.max_retry_attempts_allowed == 5
    assert metrics.total_records == 0
