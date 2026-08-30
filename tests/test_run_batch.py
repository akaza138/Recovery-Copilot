import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models.recovery_attempt import ActionMode, DecisionAction
from src.run_batch import run_batch


def _record(
    *,
    external_payment_id: str,
    failure_reason: str,
    failure_code: str = "GATEWAY_ERROR",
    amount: int = 100000,
    retry_count: int = 0,
    hours_ago: int = 2,
    dnd_opt_out: bool = False,
) -> dict:
    failed_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "external_payment_id": external_payment_id,
        "order_id": f"order_{external_payment_id}",
        "amount": amount,
        "currency": "INR",
        "failure_code": failure_code,
        "failure_reason": failure_reason,
        "failure_description": "test scenario",
        "retry_count": retry_count,
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


def _write_dataset(data_dir: Path, records: list[dict]) -> None:
    ground_truth = {
        r["external_payment_id"]: {
            "external_payment_id": r["external_payment_id"],
            "template_key": r["external_payment_id"],
            "category": "easy",
            "canonical_demo_case": None,
            "expected_root_cause": None,
            "expected_confidence_band": None,
            "representative_model_confidence": None,
            "expected_action": None,
            "expected_final_status": None,
            "expected_stop_reason": None,
            "notes": "",
        }
        for r in records
    }
    (data_dir / "synthetic_failed_payments.json").write_text(json.dumps(records), encoding="utf-8")
    (data_dir / "ground_truth.json").write_text(json.dumps(ground_truth), encoding="utf-8")


def _small_dataset() -> list[dict]:
    return [
        _record(external_payment_id="pay_retry", failure_reason="issuer_timeout"),
        _record(external_payment_id="pay_plink", failure_reason="expired_card"),
        _record(external_payment_id="pay_dnd", failure_reason="issuer_timeout", dnd_opt_out=True),
        _record(external_payment_id="pay_cap", failure_reason="issuer_timeout", retry_count=3),
        _record(external_payment_id="pay_ambiguous", failure_reason="totally_unfamiliar_reason", failure_code="SERVER_ERROR"),
    ]


def test_batch_runner_processes_the_complete_dataset(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)  # deterministic LLM_FALLBACK for the ambiguous record
    records = _small_dataset()
    _write_dataset(tmp_path, records)

    metrics, rows = run_batch(execute_real=False, db_path=tmp_path / "batch.db", data_dir=tmp_path)

    assert len(rows) == len(records)
    assert metrics.total_records == len(records)
    assert {r["external_payment_id"] for r in rows} == {r["external_payment_id"] for r in records}


def test_four_way_outcome_categorization_from_a_real_run(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    _write_dataset(tmp_path, _small_dataset())

    metrics, rows = run_batch(execute_real=False, db_path=tmp_path / "batch.db", data_dir=tmp_path)

    by_id = {r["external_payment_id"]: r for r in rows}

    assert by_id["pay_retry"]["decision_action"] == "retry"
    assert by_id["pay_retry"]["action_result"] == "succeeded"  # simulated executor models success

    assert by_id["pay_plink"]["decision_action"] == "payment_link"
    assert by_id["pay_plink"]["action_result"] == "succeeded"

    assert by_id["pay_dnd"]["decision_action"] == "stand_down"
    assert by_id["pay_dnd"]["decision_reason"] == "dnd_opt_out"

    assert by_id["pay_cap"]["decision_action"] == "stand_down"
    assert by_id["pay_cap"]["decision_reason"] == "max_attempts_reached"

    assert by_id["pay_ambiguous"]["decision_action"] == "stand_down"
    assert by_id["pay_ambiguous"]["diagnosis_source"] == "llm_fallback"

    assert metrics.successful_recoveries == 2  # retry + payment_link, both simulated
    assert metrics.stopped_by_safety_rules == 2  # dnd + retry cap
    assert metrics.policy_refusals_escalated == 1  # the low-confidence fallback stand-down


def test_confirmed_recovered_is_zero_in_simulated_mode(tmp_path, monkeypatch):
    """Default mode never touches Razorpay, so nothing can be 'confirmed' —
    only 'simulated recovery' (a modeled number) is possible."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    _write_dataset(tmp_path, _small_dataset())

    metrics, _ = run_batch(execute_real=False, db_path=tmp_path / "batch.db", data_dir=tmp_path)

    assert metrics.confirmed_recovered_amount == 0
    assert metrics.confirmed_recovered_count == 0
    assert metrics.simulated_recovered_count == 2


def test_execute_real_never_calls_razorpay_for_human_review_or_stand_down(tmp_path, monkeypatch):
    """Structural safety guarantee: even in --execute-real mode, HUMAN_REVIEW
    and STAND_DOWN decisions must never produce a REAL Razorpay call."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fakekeyfakekey")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    _write_dataset(tmp_path, [
        _record(external_payment_id="pay_dnd", failure_reason="issuer_timeout", dnd_opt_out=True),
        _record(external_payment_id="pay_cap", failure_reason="issuer_timeout", retry_count=3),
    ])

    # No mocked transport is wired in here on purpose: if the pipeline ever tried a real network
    # call for these two decisions, this test would hang/fail on a real connection attempt instead
    # of completing — the point is that RazorpayActionClient.execute_* must never be reached at all.
    metrics, rows = run_batch(execute_real=True, db_path=tmp_path / "batch.db", data_dir=tmp_path)

    for row in rows:
        assert row["decision_action"] in ("stand_down",)
        assert row["action_mode"] == "simulated"  # SIMULATED = "no Razorpay action was taken", not "REAL, no call"


def test_only_ambiguous_filters_to_llm_path_records(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    _write_dataset(tmp_path, _small_dataset())

    metrics, rows = run_batch(execute_real=False, only_ambiguous=True, db_path=tmp_path / "batch.db", data_dir=tmp_path)

    assert len(rows) == 1
    assert rows[0]["external_payment_id"] == "pay_ambiguous"


def test_limit_caps_records_processed(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    _write_dataset(tmp_path, _small_dataset())

    metrics, rows = run_batch(execute_real=False, limit=2, db_path=tmp_path / "batch.db", data_dir=tmp_path)

    assert len(rows) == 2
    assert metrics.total_records == 2


def test_ground_truth_never_reaches_the_llm(tmp_path, monkeypatch):
    """Belt-and-suspenders at the batch level: patch diagnose_ambiguous_case
    to capture what it's actually called with, and assert no ground-truth-
    shaped keys are present in the failure signal it receives."""
    captured = {}

    def spy_diagnose(failure_signal, *, client=None, model=None):
        captured["signal"] = failure_signal
        from app.models.recovery_attempt import ConfidenceBand, DiagnosisSource
        from src.diagnosis import Diagnosis

        return Diagnosis(
            root_cause="spy_diagnosis",
            confidence=0.5,
            confidence_band=ConfidenceBand.LOW,
            evidence="spy",
            source=DiagnosisSource.LLM,
            retryable=False,
            never_auto=False,
        )

    # diagnosis.py imports diagnose_ambiguous_case lazily (inside diagnose()) to avoid a circular
    # import, so the deferred `from src.llm_diagnosis import ...` resolves this name at call time —
    # patching it on the source module here is what actually takes effect.
    import src.llm_diagnosis

    monkeypatch.setattr(src.llm_diagnosis, "diagnose_ambiguous_case", spy_diagnose)

    _write_dataset(tmp_path, [_record(external_payment_id="pay_ambiguous", failure_reason="totally_unfamiliar_reason", failure_code="SERVER_ERROR")])

    run_batch(execute_real=False, db_path=tmp_path / "batch.db", data_dir=tmp_path)

    assert "signal" in captured
    signal_str = json.dumps(captured["signal"]).lower()
    for forbidden in ("ground_truth", "expected_action", "canonical_demo_case", "dnd_opt_out", "email", "phone"):
        assert forbidden not in signal_str
