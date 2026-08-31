"""Deterministic policy engine. This is the only place in the system allowed
to decide an action — the diagnosis engine (rules, or Claude for cases the
rules don't recognize) proposes a root cause and confidence band; this
module decides. The LLM is advisory, not authoritative: it cannot bypass any
gate here, because it never sees this module's inputs or output.

`PolicyInput` is deliberately the *entire* interface: no raw model-reported
confidence float, no direct access to ORM objects, no fields beyond what a
decision is allowed to depend on. If a factor isn't on PolicyInput, this
function cannot see it.
"""

from dataclasses import asdict, dataclass

from app.models.recovery_attempt import ConfidenceBand, DecisionAction

DEFAULT_HIGH_VALUE_THRESHOLD_PAISE = 50_00_000  # ₹50,000
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_COOLDOWN_SECONDS = 30 * 60  # 30 minutes between retry attempts on the same payment
DEFAULT_SERIAL_FAILURE_ATTEMPT_THRESHOLD = 2  # 2+ prior failed recovery attempts -> human review, not another auto-action
DEFAULT_SERIAL_FAILURE_LOOKBACK_DAYS = 30  # documented, not independently enforced — see note on PolicyInput.prior_recovery_attempts
DEFAULT_RETRY_COST_PAISE = 500  # ₹5 — modeled gateway/processing cost of one retry attempt
DEFAULT_PAYMENT_LINK_COST_PAISE = 1_500  # ₹15 — modeled SMS/notification + support-follow-up cost of one payment link
DEFAULT_MAX_COST_FRACTION_OF_VALUE = 0.5  # never spend more than this fraction of the payment's own value recovering it


@dataclass(frozen=True)
class PolicyConfig:
    high_value_threshold: int = DEFAULT_HIGH_VALUE_THRESHOLD_PAISE
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    serial_failure_attempt_threshold: int = DEFAULT_SERIAL_FAILURE_ATTEMPT_THRESHOLD
    serial_failure_lookback_days: int = DEFAULT_SERIAL_FAILURE_LOOKBACK_DAYS
    retry_cost_paise: int = DEFAULT_RETRY_COST_PAISE
    payment_link_cost_paise: int = DEFAULT_PAYMENT_LINK_COST_PAISE
    max_cost_fraction_of_value: float = DEFAULT_MAX_COST_FRACTION_OF_VALUE


@dataclass(frozen=True)
class PolicyInput:
    """Exactly what the policy engine is allowed to see."""

    confidence_band: ConfidenceBand
    root_cause: str
    retryable: bool
    never_auto: bool
    payment_value: int  # paise
    high_value_threshold: int
    attempt_count: int  # attempts already made on THIS payment, before this evaluation
    max_attempts: int
    cooldown_elapsed: bool
    dnd_opt_out: bool
    contact_count: int
    max_contact_attempts: int
    prior_recovery_attempts: int  # this CUSTOMER's failed recovery attempts before this batch (see Customer model).
    # `serial_failure_lookback_days` documents the intended window, but this build has no dated attempt
    # history to filter by date — Customer.prior_recovery_attempts is a pre-aggregated count assumed to
    # already be scoped to the lookback by whatever process populates it. A real system would filter a
    # dated attempt log at query time; this is a known simplification, not a hidden one.
    serial_failure_attempt_threshold: int
    retry_cost: int  # paise — modeled cost of one RETRY attempt, from PolicyConfig.retry_cost_paise
    payment_link_cost: int  # paise — modeled cost of one PAYMENT_LINK send, from PolicyConfig.payment_link_cost_paise
    max_cost_fraction: float  # refuse an auto-action whose modeled cost exceeds this fraction of payment_value


@dataclass(frozen=True)
class PolicyDecision:
    action: DecisionAction
    reason: str
    factors: dict  # JSON-serializable snapshot of every PolicyInput field, captured at decision time


def decide(policy_input: PolicyInput) -> PolicyDecision:
    factors = asdict(policy_input)
    factors["confidence_band"] = policy_input.confidence_band.value

    # 1. Risk-engine blocks always go to a human, no matter how confident the diagnosis is — and
    #    this is checked before the DND gate below, deliberately: HUMAN_REVIEW never contacts the
    #    customer (it's purely an internal queue), so it doesn't conflict with their opt-out.
    if policy_input.never_auto:
        return PolicyDecision(DecisionAction.HUMAN_REVIEW, "risk_block_requires_human_review", factors)

    # 2. Compliance: never contact an opted-out customer, full stop, before anything else is considered.
    if policy_input.dnd_opt_out:
        return PolicyDecision(DecisionAction.STAND_DOWN, "dnd_opt_out", factors)

    # 3. Candidate action from confidence band, value, and retryability.
    high_value = policy_input.payment_value >= policy_input.high_value_threshold

    if policy_input.confidence_band == ConfidenceBand.HIGH:
        candidate = DecisionAction.RETRY if policy_input.retryable else DecisionAction.PAYMENT_LINK
        reason = "high_confidence_auto_action"
    elif high_value:
        # Not HIGH confidence, and the value is large enough that a wrong guess is expensive:
        # escalate regardless of whether the underlying confidence is MEDIUM or LOW.
        candidate = DecisionAction.HUMAN_REVIEW
        reason = "high_value_uncertain_escalation"
    elif policy_input.confidence_band == ConfidenceBand.MEDIUM:
        candidate = DecisionAction.HUMAN_REVIEW
        reason = "medium_confidence_human_review"
    else:
        candidate = DecisionAction.STAND_DOWN
        reason = "confidence_below_action_threshold"

    # 4. Customer-level trust signal: a customer with a history of failed recovery attempts doesn't
    #    get another blind auto-action (retry OR payment link) even if today's diagnosis looks clean —
    #    a repeated-failure pattern is itself a reason for a human to look, regardless of confidence.
    if candidate in (DecisionAction.RETRY, DecisionAction.PAYMENT_LINK):
        if policy_input.prior_recovery_attempts >= policy_input.serial_failure_attempt_threshold:
            return PolicyDecision(DecisionAction.HUMAN_REVIEW, "serial_recovery_failure_history", factors)

    # 5. Stopping rules / compliance gates that only apply to the specific candidate action.
    if candidate == DecisionAction.RETRY:
        if policy_input.attempt_count >= policy_input.max_attempts:
            return PolicyDecision(DecisionAction.STAND_DOWN, "max_attempts_reached", factors)
        if not policy_input.cooldown_elapsed:
            return PolicyDecision(DecisionAction.STAND_DOWN, "cooldown_not_elapsed", factors)

    if candidate == DecisionAction.PAYMENT_LINK:
        if policy_input.contact_count >= policy_input.max_contact_attempts:
            return PolicyDecision(DecisionAction.STAND_DOWN, "contact_limit_reached", factors)

    # 6. Cost-effectiveness: everything above establishes that this action would otherwise be
    #    legitimate — this gate asks whether it's worth the money. A modeled per-action cost
    #    (gateway/processing fee for a retry, SMS + support follow-up for a payment link) is
    #    checked against a configurable fraction of the payment's own value; refuse rather than
    #    spend more chasing a payment than it's worth. Applies to both auto-action types alike,
    #    since it's a business-sense gate, not a risk or compliance one.
    if candidate in (DecisionAction.RETRY, DecisionAction.PAYMENT_LINK):
        estimated_cost = policy_input.retry_cost if candidate == DecisionAction.RETRY else policy_input.payment_link_cost
        max_cost = policy_input.payment_value * policy_input.max_cost_fraction
        if estimated_cost > max_cost:
            return PolicyDecision(DecisionAction.STAND_DOWN, "recovery_not_cost_effective", factors)

    return PolicyDecision(candidate, reason, factors)
