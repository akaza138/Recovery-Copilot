from src.generate_report_html import render_html

BASE_METRICS = dict(
    total_records=2,
    revenue_at_risk_events=2,
    confirmed_recovered_amount=0,
    recovery_rate=0.0,
    auto_recovery_attempts=1,
    successful_recoveries=1,
    policy_refusals_escalated=1,
    unresolved=0,
    stopped_by_safety_rules=0,
    incorrect_automatic_actions=0,
    max_retry_attempts_allowed=3,
    revenue_at_risk_amount=200000,
    rule_diagnosed_count=2,
    llm_diagnosed_count=0,
    llm_fallback_count=0,
    high_confidence_count=2,
    medium_confidence_count=0,
    low_confidence_count=0,
    simulated_action_count=2,
    real_action_count=0,
    pending_unconfirmed_count=0,
    simulated_recovered_amount=100000,
    simulated_recovered_count=1,
    human_review_rate=0.5,
    unresolved_rate=0.0,
    safety_stop_rate=0.0,
    incorrect_automatic_action_rate=0.0,
    llm_usage_rate=0.0,
    llm_fallback_rate=0.0,
    simulated_recovery_rate=0.5,
)

BASE_RECORD = dict(
    external_payment_id="pay_test1",
    template_key="issuer_timeout_retry_success",
    category="easy",
    canonical_demo_case=None,
    amount=100000,
    currency="INR",
    failure_code="GATEWAY_ERROR",
    failure_reason="issuer_timeout",
    failure_description="The bank did not respond in time.",
    diagnosis_root_cause="issuer_timeout",
    diagnosis_source="rule",
    confidence_band="high",
    model_reported_confidence=None,
    diagnosis_evidence="Rule table match on failure_reason='issuer_timeout'.",
    decision_action="retry",
    decision_reason="high_confidence_auto_action",
    decision_factors={"confidence_band": "high", "attempt_count": 0},
    action_mode="simulated",
    action_result="succeeded",
    action_evidence="Simulated retry, modeled as succeeding.",
    razorpay_reference=None,
    failed_payment_status="simulated_recovered",
    stop_reason=None,
    expected_action="retry",
)


BASE_COUNTERFACTUAL = {
    "total_records": 2,
    "summary_line": "Naive (retryable -> always retry, no gates): 1 unsafe automatic actions (1 DND breach). "
    "Ungated LLM (diagnosis recommendation executes directly): 1 unsafe automatic actions (1 DND breach). "
    "Recovery Copilot (gated): 0.",
    "naive": {"mode": "naive", "label": "Naive (retryable -> always retry, no gates)", "auto_actions": 2, "unsafe_actions": 1, "unsafe_breakdown": {"DND breach": 1}},
    "llm_only": {"mode": "llm_only", "label": "Ungated LLM (diagnosis recommendation executes directly)", "auto_actions": 2, "unsafe_actions": 1, "unsafe_breakdown": {"DND breach": 1}},
    "gated": {"mode": "gated", "label": "Recovery Copilot (gated)", "auto_actions": 1, "unsafe_actions": 0, "unsafe_breakdown": {}},
    "records": [],
}


BASE_LEDGER = {
    "intact": True,
    "total_rows": 2,
    "rows_verified": 2,
    "broken_at_sequence": None,
    "broken_row_id": None,
    "detail": None,
}


def _report(**metric_overrides) -> dict:
    metrics = {**BASE_METRICS, **metric_overrides}
    return {
        "execute_real": False,
        "only_ambiguous": False,
        "limit": None,
        "counterfactual": BASE_COUNTERFACTUAL,
        "ledger": BASE_LEDGER,
        "metrics": metrics,
        "records": [BASE_RECORD],
    }


def test_renders_valid_html_shell():
    html_out = render_html(_report())
    assert html_out.strip().startswith("<!doctype html>")
    assert "<title>" in html_out


def test_never_calls_itself_an_autonomous_agent_affirmatively():
    html_out = render_html(_report())
    # The only permitted occurrence is the explicit disclaimer "not an autonomous agent".
    assert html_out.lower().count("autonomous agent") == 1
    assert "not an autonomous agent" in html_out.lower()


def test_incorrect_automatic_actions_row_always_present_and_unrounded():
    html_out = render_html(_report(incorrect_automatic_actions=0))
    assert "Incorrect automatic actions" in html_out
    assert "<td>0</td>" in html_out  # the exact integer, not blended into a rate or omitted

    html_out_nonzero = render_html(_report(incorrect_automatic_actions=3))
    assert "<td>3</td>" in html_out_nonzero


def test_confirmed_recovered_zero_shows_the_explanatory_note():
    html_out = render_html(_report(confirmed_recovered_amount=0))
    assert "by design" in html_out
    assert "Razorpay" in html_out


def test_confirmed_recovered_nonzero_does_not_show_the_zero_note():
    html_out = render_html(_report(confirmed_recovered_amount=50000))
    assert "by design" not in html_out


def test_real_and_simulated_action_modes_get_visually_distinct_badge_classes():
    real_record = {**BASE_RECORD, "external_payment_id": "pay_real", "action_mode": "real", "action_result": "pending"}
    simulated_record = {**BASE_RECORD, "external_payment_id": "pay_sim", "action_mode": "simulated", "action_result": "succeeded"}
    report = {**_report(), "records": [real_record, simulated_record]}

    html_out = render_html(report)

    assert "badge-real" in html_out
    assert "badge-simulated" in html_out
    assert "badge-real" != "badge-simulated"  # sanity: the classes really are different strings


def test_canonical_demo_case_gets_a_visible_tag():
    record = {**BASE_RECORD, "canonical_demo_case": "case_c"}
    report = {**_report(), "records": [record]}

    html_out = render_html(report)

    assert "CASE C" in html_out
    assert "demo-tag" in html_out


def test_no_canonical_demo_case_gets_no_tag_for_that_record():
    html_out = render_html(_report())  # BASE_RECORD has canonical_demo_case=None
    # ".demo-tag" (the CSS rule) is always present in the stylesheet; what must be absent is an
    # actual rendered tag element, i.e. the class applied to a span.
    assert 'class="demo-tag"' not in html_out


def test_evidence_text_is_html_escaped():
    record = {**BASE_RECORD, "diagnosis_evidence": "Root cause looked like <script>alert(1)</script> & ambiguous"}
    report = {**_report(), "records": [record]}

    html_out = render_html(report)

    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_decision_factors_are_rendered_for_the_audit_trail():
    html_out = render_html(_report())
    assert "confidence_band" in html_out
    assert "attempt_count" in html_out


def test_model_reported_confidence_shown_only_for_llm_source():
    llm_record = {**BASE_RECORD, "diagnosis_source": "llm", "model_reported_confidence": 0.72}
    report = {**_report(), "records": [llm_record]}
    html_out = render_html(report)
    assert "0.72" in html_out
    assert "never read by the policy engine" in html_out

    rule_html = render_html(_report())  # model_reported_confidence=None for rule-based
    assert "n/a (rule-based" in rule_html


def test_counterfactual_table_renders_before_the_metrics_table():
    """It's the headline number in the project — must appear first on the page."""
    html_out = render_html(_report())
    cf_index = html_out.index("Counterfactual evaluation")
    metrics_index = html_out.index("Batch metrics")
    assert cf_index < metrics_index


def test_counterfactual_table_shows_all_three_modes_and_their_counts():
    html_out = render_html(_report())
    assert "Naive (retryable -&gt; always retry, no gates)" in html_out or "Naive (retryable -> always retry, no gates)" in html_out
    assert "Ungated LLM (diagnosis recommendation executes directly)" in html_out
    assert "Recovery Copilot (gated)" in html_out
    assert "DND breach" in html_out


def test_counterfactual_summary_line_is_rendered():
    html_out = render_html(_report())
    assert "Recovery Copilot (gated): 0." in html_out


def test_missing_counterfactual_key_does_not_crash():
    """Backward compatibility: an older report JSON without a counterfactual
    section should still render (just without that panel), not raise."""
    report = _report()
    del report["counterfactual"]
    html_out = render_html(report)  # must not raise
    assert "Counterfactual evaluation" not in html_out
    assert "Batch metrics" in html_out


def test_intact_ledger_shows_a_positive_status():
    html_out = render_html(_report())
    assert "Ledger: intact, 2 rows verified" in html_out


def test_tampered_ledger_shows_the_breaking_row():
    report = _report()
    report["ledger"] = {
        "intact": False,
        "total_rows": 5,
        "rows_verified": 2,
        "broken_at_sequence": 2,
        "broken_row_id": "abc-123",
        "detail": "content_hash mismatch",
    }
    html_out = render_html(report)
    assert "LEDGER TAMPERED" in html_out
    assert "ledger_sequence=2" in html_out
    assert "abc-123" in html_out


def test_missing_ledger_key_does_not_crash():
    report = _report()
    del report["ledger"]
    html_out = render_html(report)  # must not raise
    assert "Batch metrics" in html_out
