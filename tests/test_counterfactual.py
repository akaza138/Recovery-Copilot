import ast
from pathlib import Path

from app.models.recovery_attempt import DecisionAction
from src.counterfactual import (
    UNSAFE_CATEGORY_LABELS,
    evaluate_record,
    format_summary_line,
    llm_only_candidate_action,
    naive_candidate_action,
    summarize,
)
from src.policy import decide

# Every reason string policy.decide() can produce, from a direct read of its source — kept in sync
# by test_every_blocking_policy_reason_has_a_label below, rather than hand-maintained separately.
ALLOWED_ACTION_REASON = "high_confidence_auto_action"


def test_naive_candidate_is_retry_when_retryable():
    assert naive_candidate_action(True) == DecisionAction.RETRY


def test_naive_candidate_is_none_when_not_retryable():
    """Naive has no payment-link capability at all — a non-retryable case gets no action, not a
    fallback action. This is what makes it distinct from LLM_ONLY."""
    assert naive_candidate_action(False) is None


def test_llm_only_candidate_is_retry_when_retryable():
    assert llm_only_candidate_action(True) == DecisionAction.RETRY


def test_llm_only_candidate_is_payment_link_when_not_retryable():
    assert llm_only_candidate_action(False) == DecisionAction.PAYMENT_LINK


def test_naive_action_matching_a_gated_auto_action_is_not_unsafe():
    outcome = evaluate_record(
        external_payment_id="pay_1", amount=10000, retryable=True, gated_action=DecisionAction.RETRY, gated_reason="high_confidence_auto_action"
    )
    assert outcome.naive_action == DecisionAction.RETRY
    assert outcome.naive_unsafe is False
    assert outcome.naive_unsafe_category is None


def test_naive_no_action_is_never_unsafe():
    outcome = evaluate_record(
        external_payment_id="pay_1", amount=10000, retryable=False, gated_action=DecisionAction.HUMAN_REVIEW, gated_reason="risk_block_requires_human_review"
    )
    assert outcome.naive_action is None
    assert outcome.naive_unsafe is False


def test_dnd_breach_is_flagged_and_categorized():
    outcome = evaluate_record(
        external_payment_id="pay_1", amount=10000, retryable=True, gated_action=DecisionAction.STAND_DOWN, gated_reason="dnd_opt_out"
    )
    assert outcome.naive_unsafe is True
    assert outcome.naive_unsafe_category == "DND breach"
    assert outcome.llm_only_unsafe is True
    assert outcome.llm_only_unsafe_category == "DND breach"


def test_retry_cap_violation_is_flagged():
    outcome = evaluate_record(
        external_payment_id="pay_1", amount=10000, retryable=True, gated_action=DecisionAction.STAND_DOWN, gated_reason="max_attempts_reached"
    )
    assert outcome.naive_unsafe_category == "retry-cap violation"


def test_cooldown_breach_is_flagged():
    outcome = evaluate_record(
        external_payment_id="pay_1", amount=10000, retryable=True, gated_action=DecisionAction.STAND_DOWN, gated_reason="cooldown_not_elapsed"
    )
    assert outcome.naive_unsafe_category == "cooldown breach"


def test_contact_limit_breach_is_flagged_for_llm_only_non_retryable_case():
    """Naive takes no action on a non-retryable case, so only LLM_ONLY (which
    always picks PAYMENT_LINK when not retryable) can trigger this one."""
    outcome = evaluate_record(
        external_payment_id="pay_1", amount=10000, retryable=False, gated_action=DecisionAction.STAND_DOWN, gated_reason="contact_limit_reached"
    )
    assert outcome.naive_action is None
    assert outcome.llm_only_unsafe is True
    assert outcome.llm_only_unsafe_category == "contact-limit breach"


def test_high_value_uncertain_auto_act_is_flagged():
    outcome = evaluate_record(
        external_payment_id="pay_1", amount=90_00_000, retryable=True, gated_action=DecisionAction.HUMAN_REVIEW, gated_reason="high_value_uncertain_escalation"
    )
    assert outcome.naive_unsafe_category == "high-value auto-act on uncertain diagnosis"


def test_risk_block_auto_act_is_flagged():
    outcome = evaluate_record(
        external_payment_id="pay_1", amount=10000, retryable=False, gated_action=DecisionAction.HUMAN_REVIEW, gated_reason="risk_block_requires_human_review"
    )
    assert outcome.llm_only_unsafe_category == "acted on a risk-block case"


def test_serial_failure_history_ignored_is_flagged():
    outcome = evaluate_record(
        external_payment_id="pay_1", amount=10000, retryable=True, gated_action=DecisionAction.HUMAN_REVIEW, gated_reason="serial_recovery_failure_history"
    )
    assert outcome.naive_unsafe_category == "ignored customer serial-failure history"


def test_high_confidence_high_value_is_not_unsafe():
    """When the real policy engine itself would auto-act (HIGH confidence
    exempts the high-value gate — see policy.py), the counterfactual modes
    agreeing with it is not a safety violation."""
    outcome = evaluate_record(
        external_payment_id="pay_1", amount=90_00_000, retryable=True, gated_action=DecisionAction.RETRY, gated_reason="high_confidence_auto_action"
    )
    assert outcome.naive_unsafe is False
    assert outcome.llm_only_unsafe is False


def test_summarize_aggregates_counts_and_breakdown():
    records = [
        evaluate_record(external_payment_id="pay_1", amount=10000, retryable=True, gated_action=DecisionAction.RETRY, gated_reason="high_confidence_auto_action"),
        evaluate_record(external_payment_id="pay_2", amount=10000, retryable=True, gated_action=DecisionAction.STAND_DOWN, gated_reason="dnd_opt_out"),
        evaluate_record(external_payment_id="pay_3", amount=10000, retryable=True, gated_action=DecisionAction.STAND_DOWN, gated_reason="dnd_opt_out"),
        evaluate_record(external_payment_id="pay_4", amount=10000, retryable=False, gated_action=DecisionAction.PAYMENT_LINK, gated_reason="high_confidence_auto_action"),
    ]

    comparison = summarize(records)

    assert comparison.total_records == 4
    assert comparison.naive.auto_actions == 3  # pay_1, pay_2, pay_3 are all retryable -> naive retries all three
    assert comparison.naive.unsafe_actions == 2  # pay_2, pay_3 (both DND, both retried anyway)
    assert comparison.naive.unsafe_breakdown == {"DND breach": 2}
    assert comparison.llm_only.auto_actions == 4  # pay_1/2/3 (retry) + pay_4 (payment_link, not retryable)
    assert comparison.llm_only.unsafe_actions == 2
    assert comparison.gated.auto_actions == 2  # pay_1, pay_4
    assert comparison.gated.unsafe_actions == 0  # gated is the oracle — always 0 by construction


def test_gated_is_always_zero_unsafe_by_construction():
    """The oracle can't be unsafe against itself — this is a structural
    property of the design, not something that depends on test data."""
    records = [
        evaluate_record(external_payment_id=f"pay_{i}", amount=10000, retryable=True, gated_action=DecisionAction.STAND_DOWN, gated_reason="dnd_opt_out")
        for i in range(5)
    ]
    comparison = summarize(records)
    assert comparison.gated.unsafe_actions == 0


def test_summary_line_format():
    records = [
        evaluate_record(external_payment_id="pay_1", amount=10000, retryable=True, gated_action=DecisionAction.STAND_DOWN, gated_reason="dnd_opt_out"),
    ]
    comparison = summarize(records)
    line = format_summary_line(comparison)
    assert "1 unsafe automatic actions (1x DND breach)" in line
    assert "Recovery Copilot (gated): 0." in line


def test_summary_line_zero_unsafe_reads_cleanly():
    records = [
        evaluate_record(external_payment_id="pay_1", amount=10000, retryable=True, gated_action=DecisionAction.RETRY, gated_reason="high_confidence_auto_action"),
    ]
    comparison = summarize(records)
    line = format_summary_line(comparison)
    assert "0 unsafe automatic actions." in line


def test_every_blocking_policy_reason_has_a_label():
    """Reads policy.py's own source for every reason string it can produce —
    both literal string arguments to PolicyDecision(...) calls, and string
    literals assigned to a `reason` variable (policy.py's candidate-selection
    block sets `reason = "..."` then returns PolicyDecision(candidate,
    reason, factors) — a literal-args-only scan would silently miss those
    four) — and asserts each one that isn't the allowed-auto-action reason
    has an entry in UNSAFE_CATEGORY_LABELS. Catches the label table drifting
    out of sync with policy.py without needing to touch policy.py itself."""
    policy_source = Path(__file__).resolve().parent.parent / "src" / "policy.py"
    tree = ast.parse(policy_source.read_text(encoding="utf-8"))

    reasons: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "PolicyDecision":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                reasons.add(node.args[1].value)
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "reason" for t in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    reasons.add(node.value.value)

    assert reasons, "expected to find at least one policy reason string in policy.py"
    # Sanity floor: if this ever drops back to 6 (the literal-only count), the variable-assignment
    # scan silently stopped working and this test is no longer trustworthy.
    assert len(reasons) >= 9, f"expected at least 9 distinct reason strings in policy.py, found {len(reasons)}: {reasons}"

    blocking_reasons = reasons - {ALLOWED_ACTION_REASON}
    missing = blocking_reasons - set(UNSAFE_CATEGORY_LABELS)
    assert not missing, f"policy.py reasons with no counterfactual label: {missing}"


def test_module_never_imports_a_real_action_executor():
    """Structural guarantee: NAIVE and LLM_ONLY must never be able to reach
    Razorpay. Asserted via AST (import statements only, not prose) rather
    than trusting convention — the module's own docstring legitimately
    *names* both executors while explaining why it never imports them, so a
    raw substring search over the whole file would false-positive on its
    own documentation."""
    source = Path(__file__).resolve().parent.parent / "src" / "counterfactual.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported_names.update(alias.asname or alias.name for alias in node.names)

    assert "RazorpayActionClient" not in imported_names
    assert "SimulatedActionExecutor" not in imported_names

    # And no function anywhere in the module accepts an executor-shaped parameter.
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            param_names = [a.arg.lower() for a in node.args.args]
            assert not any("executor" in name or "razorpay" in name for name in param_names)


def test_evaluate_record_and_helpers_take_no_executor_argument():
    """Belt-and-suspenders at the signature level: none of the public
    functions in this module accept anything resembling an executor."""
    import inspect

    for fn in (naive_candidate_action, llm_only_candidate_action, evaluate_record, summarize, format_summary_line):
        params = inspect.signature(fn).parameters
        assert not any("executor" in name.lower() or "razorpay" in name.lower() for name in params)


def test_harness_matches_real_policy_decide_for_a_known_high_confidence_case():
    """End-to-end sanity check against the real, unmodified policy.decide()
    (not a re-implementation) for a case where all three modes should agree."""
    from src.policy import PolicyInput
    from app.models.recovery_attempt import ConfidenceBand

    policy_input = PolicyInput(
        confidence_band=ConfidenceBand.HIGH,
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
    decision = decide(policy_input)

    outcome = evaluate_record(
        external_payment_id="pay_1", amount=10000, retryable=True, gated_action=decision.action, gated_reason=decision.reason
    )

    assert outcome.gated_action == DecisionAction.RETRY
    assert outcome.naive_unsafe is False
    assert outcome.llm_only_unsafe is False
