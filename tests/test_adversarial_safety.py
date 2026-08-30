"""Day 4, Priority 2: adversarial tests that specifically try to break the
refusal/safety layer, rather than just confirming its happy path.

Two levels:
  - Policy-level (decide() called directly): precise, fast, isolates one
    factor or one precedence question at a time.
  - Pipeline-level (run_pipeline() against a real DB session, with a mocked
    Razorpay transport): proves the gates hold under real accumulating
    state (retry_count incrementing across real sequential calls, cooldown
    measured against real wall-clock time) — not just a hand-set flag.

Each test names the specific thing it's trying to break in its docstring.
"""

from datetime import datetime, timedelta, timezone

import httpx

from app.models.recovery_attempt import ActionMode, ActionResult, ConfidenceBand, DecisionAction
from src.pipeline import run_pipeline
from src.policy import PolicyConfig, PolicyInput, decide
from src.razorpay_action import RazorpayActionClient

# --------------------------------------------------------------------------
# Policy-level: precedence and boundary attacks
# --------------------------------------------------------------------------

BASE_KWARGS = dict(
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
)


def _input(**overrides) -> PolicyInput:
    kwargs = {**BASE_KWARGS, **overrides}
    return PolicyInput(confidence_band=kwargs.pop("confidence_band", ConfidenceBand.HIGH), **kwargs)


def test_retry_cap_blocks_a_fresh_high_confidence_diagnosis():
    """Attack: 'surely a brand-new, confident diagnosis deserves one more try.'
    It doesn't — the cap is on attempts, not on how good the latest diagnosis looks."""
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, retryable=True, attempt_count=3, max_attempts=3))
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "max_attempts_reached"


def test_cooldown_blocks_a_fresh_high_confidence_diagnosis():
    """Attack: same as above but for pacing, not the hard cap — a confident
    diagnosis must not let a retry jump the cooldown queue."""
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, retryable=True, cooldown_elapsed=False))
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "cooldown_not_elapsed"


def test_dnd_blocks_payment_link_at_high_confidence_and_high_value():
    """Attack: stack every reason auto-action 'should' be allowed (certain
    diagnosis, big payment worth recovering) against the one compliance rule
    that must never bend. DND wins outright."""
    decision = decide(
        _input(confidence_band=ConfidenceBand.HIGH, retryable=False, dnd_opt_out=True, payment_value=90_00_000)
    )
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "dnd_opt_out"


def test_dnd_blocks_retry_at_high_confidence_and_high_value():
    decision = decide(
        _input(confidence_band=ConfidenceBand.HIGH, retryable=True, dnd_opt_out=True, payment_value=90_00_000)
    )
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "dnd_opt_out"


def test_confidence_boundary_is_deterministic_not_flaky():
    """Attack: hover a diagnosis right at the HIGH/MEDIUM line and confirm
    the policy's behavior is a hard boundary, not something that could
    waver between runs. 0.85 exactly is HIGH per spec; one hair below isn't."""
    from src.diagnosis import confidence_band

    at_threshold = confidence_band(0.85)
    just_below = confidence_band(0.8499)
    assert at_threshold == ConfidenceBand.HIGH
    assert just_below == ConfidenceBand.MEDIUM

    high_decision = decide(_input(confidence_band=at_threshold, retryable=True))
    medium_decision = decide(_input(confidence_band=just_below, retryable=True))

    assert high_decision.action == DecisionAction.RETRY
    assert medium_decision.action == DecisionAction.HUMAN_REVIEW

    # Repeat the same inputs several times: a deterministic function must
    # never disagree with itself.
    for _ in range(5):
        assert decide(_input(confidence_band=at_threshold, retryable=True)).action == DecisionAction.RETRY
        assert decide(_input(confidence_band=just_below, retryable=True)).action == DecisionAction.HUMAN_REVIEW


def test_confidence_boundary_medium_low_is_also_deterministic():
    from src.diagnosis import confidence_band

    assert confidence_band(0.60) == ConfidenceBand.MEDIUM
    assert confidence_band(0.599) == ConfidenceBand.LOW

    medium_decision = decide(_input(confidence_band=ConfidenceBand.MEDIUM, payment_value=10000))
    low_decision = decide(_input(confidence_band=ConfidenceBand.LOW, payment_value=10000))
    assert medium_decision.action == DecisionAction.HUMAN_REVIEW
    assert low_decision.action == DecisionAction.STAND_DOWN


def test_serial_failure_overrides_high_value_high_confidence_auto_action():
    """Attack: even the strongest possible case for auto-action (HIGH
    confidence, large payment worth recovering, clearly retryable) must
    still defer to a customer's serial-failure history."""
    decision = decide(
        _input(
            confidence_band=ConfidenceBand.HIGH,
            retryable=True,
            payment_value=90_00_000,
            prior_recovery_attempts=5,
        )
    )
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "serial_recovery_failure_history"


def test_precedence_dnd_beats_serial_failure_history():
    """Both dnd_opt_out and a serial-failure history are true at once — the
    reported reason must be dnd_opt_out (checked first), proving precedence
    is explicit in code order, not incidental."""
    decision = decide(_input(confidence_band=ConfidenceBand.HIGH, dnd_opt_out=True, prior_recovery_attempts=10))
    assert decision.action == DecisionAction.STAND_DOWN
    assert decision.reason == "dnd_opt_out"


def test_precedence_serial_failure_beats_retry_cap():
    """Both prior_recovery_attempts and attempt_count are simultaneously at
    their blocking thresholds — the serial-failure gate (step 4) is checked
    before the retry-cap gate (step 5), so the reported reason must be
    serial_recovery_failure_history, not max_attempts_reached."""
    decision = decide(
        _input(confidence_band=ConfidenceBand.HIGH, retryable=True, prior_recovery_attempts=5, attempt_count=3, max_attempts=3)
    )
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "serial_recovery_failure_history"


def test_precedence_never_auto_beats_dnd_and_high_value():
    """Risk-block (never_auto) is checked first, ahead of DND — because
    HUMAN_REVIEW never contacts the customer, it doesn't conflict with an
    opt-out. Stacking DND and high value on top must not change this."""
    decision = decide(
        _input(confidence_band=ConfidenceBand.HIGH, never_auto=True, dnd_opt_out=True, payment_value=90_00_000)
    )
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "risk_block_requires_human_review"


def test_high_value_gate_cannot_be_smuggled_past_by_high_confidence_alone():
    """Attack: is the high-value escalation actually gated on confidence, or
    could a sufficiently large payment always be pushed through some other
    path? HIGH confidence legitimately exempts it (by spec); anything less
    than HIGH must not, no matter how close to HIGH the raw number was."""
    decision = decide(_input(confidence_band=ConfidenceBand.MEDIUM, payment_value=50_00_000))  # exactly at threshold
    assert decision.action == DecisionAction.HUMAN_REVIEW
    assert decision.reason == "high_value_uncertain_escalation"


# --------------------------------------------------------------------------
# Pipeline-level: prove the gates hold under real accumulating state
# --------------------------------------------------------------------------

TEST_KEY_ID = "rzp_test_fake0000000001"
TEST_KEY_SECRET = "fake_secret"


def _dataset_record(
    *,
    external_payment_id: str,
    failure_reason: str = "issuer_timeout",
    amount: int = 150000,
    dnd_opt_out: bool = False,
) -> dict:
    failed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    return {
        "external_payment_id": external_payment_id,
        "order_id": f"order_{external_payment_id}",
        "amount": amount,
        "currency": "INR",
        "failure_code": "GATEWAY_ERROR",
        "failure_reason": failure_reason,
        "failure_description": "test scenario",
        "retry_count": 0,
        "failed_at": failed_at.isoformat(),
        "raw_payload": {"event": "payment.failed"},
        "customer": {
            "external_customer_id": f"cust_{external_payment_id}",
            "name": "Test Customer",
            "email": "test@example.com",
            "phone": "9876543210",
            "dnd_opt_out": dnd_opt_out,
            "max_contact_attempts": 3,
            "contact_count": 0,
            "prior_recovery_attempts": 0,
            "prior_recovery_successes": 0,
        },
    }


def _real_razorpay_mock() -> RazorpayActionClient:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if request.url.path == "/v1/orders":
            return httpx.Response(200, json={"id": f"order_mock{call_count['n']}", "status": "created"})
        if request.url.path == "/v1/payment_links":
            return httpx.Response(200, json={"id": f"plink_mock{call_count['n']}", "short_url": "https://rzp.io/x/mock"})
        raise AssertionError(f"unexpected request to {request.url.path}")

    client = RazorpayActionClient(key_id=TEST_KEY_ID, key_secret=TEST_KEY_SECRET, transport=httpx.MockTransport(handler))
    client._call_count = call_count  # for tests to assert how many real calls actually happened
    return client


def test_pipeline_blocks_the_attempt_immediately_after_the_cap_is_reached(db_session):
    """Attack: run the pipeline for real, repeatedly, on the same payment —
    not a hand-set retry_count=3 in seed data — and confirm the cap is
    enforced from real accumulated state, not a coincidence of fixtures."""
    record = _dataset_record(external_payment_id="pay_cap_attack")
    config = PolicyConfig(cooldown_seconds=0)  # isolate the cap from cooldown interference
    razorpay = _real_razorpay_mock()

    results = [run_pipeline(db_session, record, action_executor=razorpay, policy_config=config) for _ in range(3)]
    for result in results:
        assert result.policy_decision.action == DecisionAction.RETRY

    fourth = run_pipeline(db_session, record, action_executor=razorpay, policy_config=config)
    assert fourth.policy_decision.action == DecisionAction.STAND_DOWN
    assert fourth.policy_decision.reason == "max_attempts_reached"
    assert fourth.action_outcome.action_mode == ActionMode.SIMULATED  # no real call was made for the blocked attempt
    assert razorpay._call_count["n"] == 3  # exactly the 3 legitimate attempts, never a 4th


def test_pipeline_blocks_an_immediate_repeat_within_the_cooldown_window(db_session):
    """Attack: hammer the same payment twice back-to-back with the REAL
    default cooldown (30 minutes) — no override this time — and confirm the
    second call is blocked by genuine wall-clock timing, not a stubbed flag."""
    record = _dataset_record(external_payment_id="pay_cooldown_attack")
    razorpay = _real_razorpay_mock()

    first = run_pipeline(db_session, record, action_executor=razorpay)  # default PolicyConfig: 30 min cooldown
    assert first.policy_decision.action == DecisionAction.RETRY

    second = run_pipeline(db_session, record, action_executor=razorpay)  # immediately after, no time has passed
    assert second.policy_decision.action == DecisionAction.STAND_DOWN
    assert second.policy_decision.reason == "cooldown_not_elapsed"
    assert razorpay._call_count["n"] == 1  # the blocked second attempt never reached Razorpay


def test_pipeline_dnd_customer_never_triggers_a_real_payment_link_call(db_session):
    """Attack: a non-retryable failure for an opted-out customer would
    otherwise be a clean PAYMENT_LINK case — confirm no Razorpay call is
    even reachable, not just that the result happens to look right."""
    record = _dataset_record(external_payment_id="pay_dnd_plink_attack", failure_reason="expired_card", dnd_opt_out=True)
    razorpay = _real_razorpay_mock()

    result = run_pipeline(db_session, record, action_executor=razorpay)

    assert result.policy_decision.action == DecisionAction.STAND_DOWN
    assert result.policy_decision.reason == "dnd_opt_out"
    assert result.action_outcome.action_mode == ActionMode.SIMULATED
    assert result.action_outcome.razorpay_reference is None
    assert razorpay._call_count["n"] == 0  # Razorpay was never called at all


def test_pipeline_dnd_customer_never_triggers_a_real_retry_call(db_session):
    record = _dataset_record(external_payment_id="pay_dnd_retry_attack", failure_reason="issuer_timeout", dnd_opt_out=True)
    razorpay = _real_razorpay_mock()

    result = run_pipeline(db_session, record, action_executor=razorpay)

    assert result.policy_decision.action == DecisionAction.STAND_DOWN
    assert result.policy_decision.reason == "dnd_opt_out"
    assert razorpay._call_count["n"] == 0


def test_pipeline_high_value_dnd_customer_is_never_auto_acted_on(db_session):
    """Combined-factor attack at the pipeline level: high value + a
    retryable, high-confidence diagnosis + DND. DND must still win end to
    end, through the real diagnosis -> policy -> action chain."""
    record = _dataset_record(external_payment_id="pay_combined_attack", failure_reason="issuer_timeout", amount=90_00_000, dnd_opt_out=True)
    razorpay = _real_razorpay_mock()

    result = run_pipeline(db_session, record, action_executor=razorpay)

    assert result.diagnosis.confidence_band == ConfidenceBand.HIGH  # confirms this really was the strong case
    assert result.policy_decision.action == DecisionAction.STAND_DOWN
    assert result.policy_decision.reason == "dnd_opt_out"
    assert razorpay._call_count["n"] == 0
