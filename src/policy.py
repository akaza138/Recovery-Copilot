"""Deterministic policy engine. This is the only place in the system allowed
to decide an action — the diagnosis engine (rule-based today, Claude-assisted
later) proposes a root cause and confidence band; this module decides.

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


@dataclass(frozen=True)
class PolicyConfig:
    high_value_threshold: int = DEFAULT_HIGH_VALUE_THRESHOLD_PAISE
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS


@dataclass(frozen=True)
class PolicyInput:
    """Exactly what the policy engine is allowed to see."""

    confidence_band: ConfidenceBand
    root_cause: str
    retryable: bool
    never_auto: bool
    payment_value: int  # paise
    high_value_threshold: int
    attempt_count: int  # attempts already made, before this evaluation
    max_attempts: int
    cooldown_elapsed: bool
    dnd_opt_out: bool
    contact_count: int
    max_contact_attempts: int


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

    # 4. Stopping rules / compliance gates that only apply to the specific candidate action.
    if candidate == DecisionAction.RETRY:
        if policy_input.attempt_count >= policy_input.max_attempts:
            return PolicyDecision(DecisionAction.STAND_DOWN, "max_attempts_reached", factors)
        if not policy_input.cooldown_elapsed:
            return PolicyDecision(DecisionAction.STAND_DOWN, "cooldown_not_elapsed", factors)

    if candidate == DecisionAction.PAYMENT_LINK:
        if policy_input.contact_count >= policy_input.max_contact_attempts:
            return PolicyDecision(DecisionAction.STAND_DOWN, "contact_limit_reached", factors)

    return PolicyDecision(candidate, reason, factors)
