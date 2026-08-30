"""Batch evaluation runner: processes the full synthetic dataset (or a
filtered/limited subset) through diagnose -> policy -> action -> observe ->
audit, once per record, and reports honest batch-wide metrics.

Usage:
    python -m src.run_batch                        # full batch, simulated action/result layer
    python -m src.run_batch --execute-real           # real Razorpay test-mode calls for approved auto-actions
    python -m src.run_batch --only-ambiguous          # only records the rule table can't classify (LLM path)
    python -m src.run_batch --limit 10                 # cap how many records get processed

Diagnosis always uses the real pipeline (rules, then Claude for anything the
rules don't recognize) regardless of --execute-real — that flag controls
ONLY the Razorpay action/result layer:

  default:        no network calls at all; RETRY/PAYMENT_LINK outcomes are
                   MODELED (SimulatedActionExecutor) for a fast, free,
                   reproducible evaluation run.
  --execute-real:  RETRY/PAYMENT_LINK decisions are executed for real against
                   Razorpay test mode (RazorpayActionClient). HUMAN_REVIEW
                   and STAND_DOWN decisions never call Razorpay in either
                   mode — see src/pipeline.py's _execute_action. Every real
                   action is REAL + PENDING, never SUCCEEDED (see
                   src/razorpay_action.py).

Confirmed Recovered is ₹0 by design, in both modes, always — see
CONFIRMED_RECOVERED_ZERO_NOTE below for why, and README.md's "Why Confirmed
Recovered is ₹0" section for the full investigation (Razorpay's own docs
confirm there is no server-side way to complete a test-mode payment;
completion requires the hosted Checkout UI, which was attempted via headless
browser and found to be hard-blocked at the network layer in this
environment — a genuine, investigated dead end, not an unbuilt feature).

Resets its own SQLite DB (data/batch_run.db, independent of the Day-2
single-case data/recovery_copilot.db) at the start of every run, so the same
seed always reproduces the same batch from a clean slate.

Writes data/batch_report.json alongside the printed report as the evidence
artifact for this run.
"""

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from src.batch_metrics import BatchMetrics, compute_metrics, RecordOutcome
from src.diagnosis import RULE_TABLE
from src.pipeline import load_full_dataset, load_ground_truth, run_pipeline
from src.policy import PolicyConfig
from src.razorpay_action import RazorpayActionClient
from src.simulated_action import SimulatedActionExecutor

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DB_PATH = DATA_DIR / "batch_run.db"
DEFAULT_REPORT_PATH = DATA_DIR / "batch_report.json"

CONFIRMED_RECOVERED_ZERO_NOTE = (
    "  ^ This is INR 0.00 BY DESIGN, not a bug or an unbuilt feature (investigated Day 4). Razorpay's\n"
    "    own docs confirm there is no server-side API to complete a test-mode payment - it requires the\n"
    "    customer to go through the hosted Checkout UI (test card + a mock bank OTP page). A headless-\n"
    "    browser attempt at that flow was made and found hard-blocked at the network layer in this\n"
    "    environment. This system's job stops at 'the customer now has a working way to pay' (a real,\n"
    "    Razorpay-confirmed order or payment link) - completing the payment is the CUSTOMER's action,\n"
    "    not the agent's, and this project will not submit card data server-side to fake it. See\n"
    "    README.md, 'Why Confirmed Recovered is INR 0'."
)


def _select_records(records: list[dict], *, only_ambiguous: bool, limit: int | None) -> list[dict]:
    if only_ambiguous:
        records = [r for r in records if r["failure_reason"] not in RULE_TABLE]
    if limit is not None:
        records = records[:limit]
    return records


def run_batch(
    *,
    execute_real: bool,
    only_ambiguous: bool = False,
    limit: int | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    data_dir: Path = DATA_DIR,
    policy_config: PolicyConfig | None = None,
) -> tuple[BatchMetrics, list[dict]]:
    """Runs the batch and returns (metrics, per_record_report_rows). Has no
    print statements — the CLI (main(), below) owns all output."""
    dataset = load_full_dataset(data_dir)
    ground_truth = load_ground_truth(data_dir)
    records = _select_records(dataset, only_ambiguous=only_ambiguous, limit=limit)

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.drop_all(bind=engine)  # fresh slate every run, for reproducibility
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    action_executor = RazorpayActionClient() if execute_real else SimulatedActionExecutor()

    outcomes: list[RecordOutcome] = []
    per_record_rows: list[dict] = []

    for record in records:
        db = session_factory()
        try:
            result = run_pipeline(db, record, action_executor=action_executor, policy_config=policy_config)
        finally:
            db.close()

        outcome = RecordOutcome(
            external_payment_id=result.failed_payment.external_payment_id,
            amount=result.failed_payment.amount,
            diagnosis_root_cause=result.diagnosis.root_cause,
            diagnosis_source=result.diagnosis.source,
            confidence_band=result.diagnosis.confidence_band.value,
            decision_action=result.policy_decision.action,
            decision_reason=result.policy_decision.reason,
            action_mode=result.action_outcome.action_mode,
            action_result=result.action_outcome.action_result,
        )
        outcomes.append(outcome)

        gt = ground_truth.get(outcome.external_payment_id, {})
        per_record_rows.append(
            {
                "external_payment_id": outcome.external_payment_id,
                "template_key": gt.get("template_key"),
                "category": gt.get("category"),
                "amount": outcome.amount,
                "diagnosis_root_cause": outcome.diagnosis_root_cause,
                "diagnosis_source": outcome.diagnosis_source.value,
                "confidence_band": outcome.confidence_band,
                "decision_action": outcome.decision_action.value,
                "decision_reason": outcome.decision_reason,
                "action_mode": outcome.action_mode.value,
                "action_result": outcome.action_result.value,
                "expected_action": gt.get("expected_action"),
            }
        )

    config = policy_config or PolicyConfig()
    metrics = compute_metrics(outcomes, ground_truth, max_attempts=config.max_attempts)
    return metrics, per_record_rows


def _fmt_inr(paise: int) -> str:
    # "INR" rather than the rupee glyph: some terminals (e.g. Windows cp1252) can't encode ₹ and would crash on print.
    return f"INR {paise / 100:,.2f}"


def _fmt_pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def print_report(metrics: BatchMetrics, *, execute_real: bool) -> None:
    mode = "REAL (--execute-real)" if execute_real else "SIMULATED (default)"
    print(f"\nAction/result layer: {mode}")
    print(f"Total records processed: {metrics.total_records}\n")

    print("| Metric | Value |")
    print("|---|---:|")
    print(f"| Revenue-at-risk events | {metrics.revenue_at_risk_events} |")
    print(f"| Confirmed recovered | {_fmt_inr(metrics.confirmed_recovered_amount)} |")
    if metrics.confirmed_recovered_amount == 0:
        print(CONFIRMED_RECOVERED_ZERO_NOTE)
    print(f"| Recovery rate | {_fmt_pct(metrics.recovery_rate)} |")
    print(f"| Auto-recovery attempts | {metrics.auto_recovery_attempts} |")
    print(f"| Successful recoveries | {metrics.successful_recoveries} |")
    print(f"| Policy refusals (escalated) | {metrics.policy_refusals_escalated} |")
    print(f"| Unresolved | {metrics.unresolved} |")
    print(f"| Stopped by safety rules | {metrics.stopped_by_safety_rules} |")
    print(f"| Incorrect automatic actions | {metrics.incorrect_automatic_actions} |")
    print(f"| Max retry attempts allowed | {metrics.max_retry_attempts_allowed} |")

    print("\nAlso reported:")
    print(f"  Revenue at risk (total): {_fmt_inr(metrics.revenue_at_risk_amount)}")
    print(f"  Rule-diagnosed: {metrics.rule_diagnosed_count}")
    print(f"  LLM-diagnosed: {metrics.llm_diagnosed_count}")
    print(f"  LLM fallback: {metrics.llm_fallback_count}")
    print(f"  Confidence HIGH / MEDIUM / LOW: {metrics.high_confidence_count} / {metrics.medium_confidence_count} / {metrics.low_confidence_count}")
    print(f"  Simulated actions: {metrics.simulated_action_count}")
    print(f"  Real test-mode actions: {metrics.real_action_count}")
    print(f"  Pending/unconfirmed actions: {metrics.pending_unconfirmed_count}")
    print(f"  Simulated recovery (modeled, not confirmed): {_fmt_inr(metrics.simulated_recovered_amount)} ({metrics.simulated_recovered_count} events, {_fmt_pct(metrics.simulated_recovery_rate)})")

    print("\nEvaluation (full batch, no cherry-picking):")
    print(f"  Recovery rate: {_fmt_pct(metrics.recovery_rate)}")
    print(f"  Human review rate: {_fmt_pct(metrics.human_review_rate)}")
    print(f"  Unresolved rate: {_fmt_pct(metrics.unresolved_rate)}")
    print(f"  Safety-stop rate: {_fmt_pct(metrics.safety_stop_rate)}")
    print(f"  Incorrect automatic action count: {metrics.incorrect_automatic_actions}")
    print(f"  Incorrect automatic action rate: {_fmt_pct(metrics.incorrect_automatic_action_rate)}")
    print(f"  LLM usage rate: {_fmt_pct(metrics.llm_usage_rate)}")
    print(f"  LLM fallback rate: {_fmt_pct(metrics.llm_fallback_rate)}")
    if metrics.rule_based_diagnosis_accuracy is not None:
        print(
            f"  Rule-based diagnosis accuracy: {_fmt_pct(metrics.rule_based_diagnosis_accuracy)} "
            f"({metrics.rule_based_diagnosis_total} records) — deterministic by construction"
        )
    else:
        print("  Rule-based diagnosis accuracy: not evaluated (no rule-based records in this run)")
    print(
        "  LLM diagnosis accuracy: NOT evaluated against ground_truth.expected_root_cause. Claude's root_cause is "
        "freeform text, and ground truth's values for ambiguous cases are illustrative placeholders authored before "
        "any real Claude call, not verified gold labels — exact-string comparison would be a misleading metric. "
        "Only the resulting POLICY ACTION is scored (that's what 'incorrect automatic actions' depends on)."
    )

    if metrics.incorrect_automatic_actions > 0:
        print("\n" + "!" * 70)
        print("!! SAFETY VIOLATION: incorrect_automatic_actions > 0 — STOP AND INVESTIGATE  !!")
        print("!" * 70)
        for detail in metrics.incorrect_automatic_action_details:
            print(
                f"  {detail.external_payment_id} ({detail.template_key}): "
                f"took '{detail.actual_action}', ground truth expected '{detail.expected_action}'"
            )
    else:
        print("\nSafety check: incorrect_automatic_actions = 0 (target met).")


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute-real", action="store_true", help="Execute policy-approved actions against Razorpay test mode.")
    parser.add_argument("--only-ambiguous", action="store_true", help="Only process records the rule table can't classify (LLM path).")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of records processed.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args()

    metrics, per_record_rows = run_batch(
        execute_real=args.execute_real,
        only_ambiguous=args.only_ambiguous,
        limit=args.limit,
        db_path=args.db_path,
    )

    print_report(metrics, execute_real=args.execute_real)

    report = {
        "execute_real": args.execute_real,
        "only_ambiguous": args.only_ambiguous,
        "limit": args.limit,
        "metrics": {k: v for k, v in metrics.__dict__.items() if k != "incorrect_automatic_action_details"},
        "incorrect_automatic_actions": [d.__dict__ for d in metrics.incorrect_automatic_action_details],
        "records": per_record_rows,
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nEvidence report written to {args.report_path}")

    return 1 if metrics.incorrect_automatic_actions > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
