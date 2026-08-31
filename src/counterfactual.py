"""Counterfactual evaluation: for each record already processed by the real
pipeline, derives what two ungated decision strategies would have done, and
scores them against the real, unmodified policy engine's own decision as
the safety oracle — not against ground truth, since the point is to measure
the cost of skipping the exact gates the real system enforces, not to
re-litigate diagnosis accuracy.

  NAIVE     retry anything diagnosis.retryable says is retryable. No other
            action exists in this mode — a non-retryable case gets no
            action at all — and no gate is even consulted.
  LLM_ONLY  the diagnosis's full action recommendation (retry if retryable,
            else payment_link) executes directly. Every gate in policy.py
            is bypassed.
  GATED     the real Recovery Copilot decision — src.policy.decide(),
            unmodified, already computed by the real pipeline run.

Structural safety: this module takes only labels (a bool and two
DecisionAction/str values) as input. It never receives, imports, or
constructs a RazorpayActionClient or SimulatedActionExecutor, so there is no
code path by which NAIVE or LLM_ONLY could reach Razorpay — see
tests/test_counterfactual.py's structural test, which asserts this module
never imports either executor.

Both counterfactual actions are derived from the SAME diagnosis the real
pipeline already computed for that record (via run_batch.py's main loop) —
this harness makes no additional diagnosis calls, so the three modes are
compared fairly against one draw from the (possibly non-deterministic) LLM,
not three separate ones.
"""

from dataclasses import dataclass, field

from app.models.recovery_attempt import DecisionAction

AUTO_ACTIONS = (DecisionAction.RETRY, DecisionAction.PAYMENT_LINK)

# Maps a policy.py `reason` string to the human-readable category used in
# the comparison table's breakdown — one entry per gate that can currently
# block an auto-action. Kept in sync with policy.py's reason strings by the
# test suite (test_counterfactual.py asserts every reason policy.py can
# produce for a blocked auto-action has a label here).
UNSAFE_CATEGORY_LABELS: dict[str, str] = {
    "dnd_opt_out": "DND breach",
    "max_attempts_reached": "retry-cap violation",
    "cooldown_not_elapsed": "cooldown breach",
    "contact_limit_reached": "contact-limit breach",
    "high_value_uncertain_escalation": "high-value auto-act on uncertain diagnosis",
    "medium_confidence_human_review": "acted despite MEDIUM confidence",
    "confidence_below_action_threshold": "acted despite LOW confidence",
    "risk_block_requires_human_review": "acted on a risk-block case",
    "serial_recovery_failure_history": "ignored customer serial-failure history",
    "recovery_not_cost_effective": "spent more than the payment was worth",
}


def naive_candidate_action(retryable: bool) -> DecisionAction | None:
    return DecisionAction.RETRY if retryable else None


def llm_only_candidate_action(retryable: bool) -> DecisionAction:
    return DecisionAction.RETRY if retryable else DecisionAction.PAYMENT_LINK


def _score(candidate: DecisionAction | None, oracle_action: DecisionAction, oracle_reason: str) -> tuple[bool, str | None]:
    """A counterfactual action is unsafe exactly when it would auto-act
    (candidate is not None) on a record the real, unmodified policy engine
    would NOT have auto-acted on. If the oracle would also have auto-acted,
    the counterfactual mode acting is not a safety violation — both modes
    derive RETRY-vs-PAYMENT_LINK from the same retryable flag, so they agree
    on the action type whenever both choose to act."""
    if candidate is None:
        return False, None
    if oracle_action in AUTO_ACTIONS:
        return False, None
    return True, UNSAFE_CATEGORY_LABELS.get(oracle_reason, oracle_reason)


@dataclass(frozen=True)
class CounterfactualRecordOutcome:
    external_payment_id: str
    amount: int
    naive_action: DecisionAction | None
    naive_unsafe: bool
    naive_unsafe_category: str | None
    llm_only_action: DecisionAction
    llm_only_unsafe: bool
    llm_only_unsafe_category: str | None
    gated_action: DecisionAction
    gated_reason: str


def evaluate_record(
    *, external_payment_id: str, amount: int, retryable: bool, gated_action: DecisionAction, gated_reason: str
) -> CounterfactualRecordOutcome:
    naive = naive_candidate_action(retryable)
    llm_only = llm_only_candidate_action(retryable)
    naive_unsafe, naive_category = _score(naive, gated_action, gated_reason)
    llm_only_unsafe, llm_only_category = _score(llm_only, gated_action, gated_reason)

    return CounterfactualRecordOutcome(
        external_payment_id=external_payment_id,
        amount=amount,
        naive_action=naive,
        naive_unsafe=naive_unsafe,
        naive_unsafe_category=naive_category,
        llm_only_action=llm_only,
        llm_only_unsafe=llm_only_unsafe,
        llm_only_unsafe_category=llm_only_category,
        gated_action=gated_action,
        gated_reason=gated_reason,
    )


@dataclass(frozen=True)
class ModeSummary:
    mode: str
    label: str
    auto_actions: int
    unsafe_actions: int
    unsafe_breakdown: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CounterfactualComparison:
    total_records: int
    naive: ModeSummary
    llm_only: ModeSummary
    gated: ModeSummary
    records: list[CounterfactualRecordOutcome]
    summary_line: str


def _summarize(records: list[CounterfactualRecordOutcome], *, mode: str, label: str, action_attr: str, unsafe_attr: str, category_attr: str) -> ModeSummary:
    auto = sum(1 for r in records if getattr(r, action_attr) in AUTO_ACTIONS)
    unsafe_records = [r for r in records if getattr(r, unsafe_attr)]
    breakdown: dict[str, int] = {}
    for r in unsafe_records:
        category = getattr(r, category_attr)
        breakdown[category] = breakdown.get(category, 0) + 1
    return ModeSummary(mode=mode, label=label, auto_actions=auto, unsafe_actions=len(unsafe_records), unsafe_breakdown=breakdown)


def _pluralize_breakdown(breakdown: dict[str, int]) -> str:
    # "Nx <category>" rather than trying to grammatically pluralize each category label (which
    # produced real nonsense — "diagnosiss", "a risk-block cases" — for several of the fixed
    # labels above; sidestepping pluralization entirely is more honest than a half-working attempt).
    return ", ".join(f"{count}x {category}" for category, count in breakdown.items())


def format_summary_line(comparison: "CounterfactualComparison") -> str:
    lines = []
    for mode_summary in (comparison.naive, comparison.llm_only):
        if mode_summary.unsafe_actions == 0:
            lines.append(f"{mode_summary.label}: 0 unsafe automatic actions.")
        else:
            breakdown_text = _pluralize_breakdown(mode_summary.unsafe_breakdown)
            lines.append(f"{mode_summary.label}: {mode_summary.unsafe_actions} unsafe automatic actions ({breakdown_text}).")
    lines.append(f"{comparison.gated.label}: {comparison.gated.unsafe_actions}.")
    return " ".join(lines)


def summarize(records: list[CounterfactualRecordOutcome]) -> CounterfactualComparison:
    naive_summary = _summarize(records, mode="naive", label="Naive (retryable -> always retry, no gates)", action_attr="naive_action", unsafe_attr="naive_unsafe", category_attr="naive_unsafe_category")
    llm_only_summary = _summarize(records, mode="llm_only", label="Ungated LLM (diagnosis recommendation executes directly)", action_attr="llm_only_action", unsafe_attr="llm_only_unsafe", category_attr="llm_only_unsafe_category")
    gated_auto = sum(1 for r in records if r.gated_action in AUTO_ACTIONS)
    gated_summary = ModeSummary(mode="gated", label="Recovery Copilot (gated)", auto_actions=gated_auto, unsafe_actions=0, unsafe_breakdown={})

    comparison = CounterfactualComparison(
        total_records=len(records), naive=naive_summary, llm_only=llm_only_summary, gated=gated_summary, records=records, summary_line=""
    )
    summary_line = format_summary_line(comparison)
    return CounterfactualComparison(
        total_records=comparison.total_records,
        naive=comparison.naive,
        llm_only=comparison.llm_only,
        gated=comparison.gated,
        records=comparison.records,
        summary_line=summary_line,
    )
