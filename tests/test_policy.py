from app.models.recovery_attempt import ConfidenceBand, DecisionAction
from src.policy import PolicyInput, decide

DEFAULT_KWARGS = dict(
    root_cause="issuer_timeout",
    retryable=True,
    never_auto=False,
    payment_value=10000,
    high_value_threshold=50_00_000,
    attempt_count=0,
    max_attempts=3,
    cooldown_elapsed=True,
    dnd_opt_out=False,
    contact_count=0,
    max_contact_attempts=3,
    prior_recovery_attempts=0,
    serial_failure_attempt_threshold=2,
    retry_cost=500,
    payment_link_cost=1_500,
    max_cost_fraction=0.5,
)


def _input(**overrides) -> PolicyInput:
    kwargs = {**DEFAULT_KWARGS, **overrides}
    return PolicyInput(confidence_band=kwargs.pop("confidence_band", ConfidenceBand.HIGH), **kwargs)


def test_high_confidence_retryable_gets_retry():
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, retryable=True))
    assert decision.action == DecisionAction.RETRY
    assert decision.reason == "high_confidence_auto_action"


def test_high_confidence_non_retryable_gets_payment_link():
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, retryable=False))
    assert decision.action == DecisionAction.PAYMENT_LINK


def test_medium_confidence_normal_value_gets_human_review():
    decision = decide(_input(confidence_band=ConfidenceBand.MEDIUM, payment_value=10000))
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "medium_confidence_human_review"


def test_low_confidence_normal_value_stands_down():
    """'Policy refusal': too uncertain to guess, and not high-stakes enough to page a human."""
    decision = decide(_input(confidence_band=ConfidenceBand.LOW, payment_value=10000))
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "confidence_below_action_threshold"


def test_high_value_uncertain_escalates_to_human_review_even_at_medium_confidence():
    decision = decide(_input(confidence_band=ConfidenceBand.MEDIUM, payment_value=80_00_000))
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "high_value_uncertain_escalation"


def test_high_value_uncertain_escalates_even_at_low_confidence():
    decision = decide(_input(confidence_band=ConfidenceBand.LOW, payment_value=80_00_000))
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "high_value_uncertain_escalation"


def test_high_confidence_high_value_still_auto_acts():
    """HIGH confidence is exempt from the high-value escalation — the escalation
    rule exists specifically to catch *uncertain* diagnoses on big payments."""
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, payment_value=80_00_000, retryable=True))
    assert decision.action == DecisionAction.RETRY


def test_dnd_optout_stands_down_regardless_of_confidence():
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, dnd_opt_out=True))
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "dnd_opt_out"


def test_risk_block_still_gets_human_review_even_for_a_dnd_customer():
    """never_auto takes priority over dnd_opt_out: HUMAN_REVIEW is an
    internal-only signal that never contacts the customer, so it doesn't
    conflict with their opt-out — a risk-blocked payment still deserves a
    human's eyes regardless of contact preference."""
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, dnd_opt_out=True, never_auto=True, payment_value=80_00_000))
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "risk_block_requires_human_review"


def test_retry_cap_reached_stands_down():
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, retryable=True, attempt_count=3, max_attempts=3))
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "max_attempts_reached"


def test_retry_cap_not_yet_reached_still_retries():
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, retryable=True, attempt_count=2, max_attempts=3))
    assert decision.action == DecisionAction.RETRY


def test_cooldown_not_elapsed_stands_down():
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, retryable=True, cooldown_elapsed=False))
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "cooldown_not_elapsed"


def test_contact_limit_reached_stands_down_for_payment_link():
    decision = decide(
        _input(confidence_band=ConfidenceBand.HIGH, retryable=False, contact_count=3, max_contact_attempts=3)
    )
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "contact_limit_reached"


def test_contact_limit_does_not_block_retry():
    """Contact limit is a PAYMENT_LINK-specific gate — a silent gateway retry isn't a customer contact."""
    decision = decide(
        _input(confidence_band=ConfidenceBand.HIGH, retryable=True, contact_count=3, max_contact_attempts=3)
    )
    assert decision.action == DecisionAction.RETRY


def test_risk_block_always_human_review_regardless_of_confidence():
    for band in (ConfidenceBand.HIGH, ConfidenceBand.MEDIUM, ConfidenceBand.LOW):
        decision = decide(_input(confidence_band=band, never_auto=True))
        assert decision.action == DecisionAction.HUMAN_REVIEW
        assert decision.reason == "risk_block_requires_human_review"


def test_serial_failure_history_escalates_instead_of_retrying():
    """2+ prior failed recovery attempts on this customer (the configured
    threshold) routes to HUMAN_REVIEW even though today's diagnosis alone
    would otherwise auto-retry — resolves the Day-2 serial-customer gap."""
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, retryable=True, prior_recovery_attempts=6))
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "serial_recovery_failure_history"


def test_serial_failure_history_also_overrides_payment_link():
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, retryable=False, prior_recovery_attempts=2))
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "serial_recovery_failure_history"


def test_serial_failure_below_threshold_still_auto_acts():
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, retryable=True, prior_recovery_attempts=1))
    assert decision.action == DecisionAction.RETRY


def test_serial_failure_threshold_is_configurable():
    decision = decide(
        _input(confidence_band=ConfidenceBand.HIGH, retryable=True, prior_recovery_attempts=1, serial_failure_attempt_threshold=1)
    )
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "serial_recovery_failure_history"


def test_serial_failure_does_not_apply_when_already_stood_down():
    """The serial-failure gate only overrides an auto-action candidate — it
    has nothing to override for a case that already stands down on its own
    merits (confidence too low), so that reason should win, not be masked."""
    decision = decide(_input(confidence_band=ConfidenceBand.LOW, prior_recovery_attempts=6))
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "confidence_below_action_threshold"


def test_decision_factors_snapshot_is_json_serializable():
    import json

    decision = decide(_input())
    json.dumps(decision.factors)  # raises if anything (e.g. a bare enum) isn't serializable


def test_cost_effectiveness_blocks_a_low_value_payment_link():
    """A HIGH-confidence, non-retryable case would normally auto-send a
    payment link — but if the payment is worth less than the modeled cost
    of sending one, spending money to chase it isn't worth it."""
    decision = decide(
        _input(confidence_band=ConfidenceBand.HIGH, retryable=False, payment_value=2000, payment_link_cost=1_500, max_cost_fraction=0.5)
    )
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "recovery_not_cost_effective"


def test_cost_effectiveness_blocks_a_low_value_retry():
    decision = decide(
        _input(confidence_band=ConfidenceBand.HIGH, retryable=True, payment_value=600, retry_cost=500, max_cost_fraction=0.5)
    )
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "recovery_not_cost_effective"


def test_cost_effectiveness_does_not_block_a_normal_value_action():
    """Sanity: the gate only fires when cost genuinely exceeds the
    configured fraction of value — it must not creep into ordinary cases."""
    decision = decide(
        _input(confidence_band=ConfidenceBand.HIGH, retryable=True, payment_value=10000, retry_cost=500, max_cost_fraction=0.5)
    )
    assert decision.action == DecisionAction.RETRY
    assert decision.reason == "high_confidence_auto_action"


def test_cost_effectiveness_fraction_is_configurable():
    """The same payment/cost pair flips from blocked to allowed purely by
    widening the configured fraction — proves the threshold is read from
    input, not hardcoded."""
    tight = decide(
        _input(confidence_band=ConfidenceBand.HIGH, retryable=True, payment_value=2000, retry_cost=1_000, max_cost_fraction=0.3)
    )
    loose = decide(
        _input(confidence_band=ConfidenceBand.HIGH, retryable=True, payment_value=2000, retry_cost=1_000, max_cost_fraction=0.9)
    )
    assert tight.action == DecisionAction.STAND_DOWN
    assert tight.reason == "recovery_not_cost_effective"
    assert loose.action == DecisionAction.RETRY


def test_cost_effectiveness_does_not_apply_to_human_review_or_stand_down_candidates():
    """The gate only evaluates auto-action candidates (RETRY/PAYMENT_LINK) —
    a case that was already going to HUMAN_REVIEW or STAND_DOWN on its own
    merits must keep that original reason, not get relabeled."""
    decision = decide(
        _input(confidence_band=ConfidenceBand.MEDIUM, payment_value=100, retry_cost=500, payment_link_cost=1_500, max_cost_fraction=0.5)
    )
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "medium_confidence_human_review"
