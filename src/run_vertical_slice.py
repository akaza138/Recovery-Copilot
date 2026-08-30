"""CLI entrypoint for the Day-2 vertical slice: takes ONE synthetic failed
payment through diagnose -> policy -> Razorpay test-mode action -> observe
-> append-only audit record, printing each stage.

Usage:
    python -m src.run_vertical_slice                      # canonical Case A (transient failure, retry)
    python -m src.run_vertical_slice --case b               # canonical Case B (non-retryable, payment link)
    python -m src.run_vertical_slice --case c                # canonical Case C (high-value + uncertain, human review)
    python -m src.run_vertical_slice --payment-id pay_xxx      # a specific dataset record

Persists to a local SQLite file (data/recovery_copilot.db) via the same
Customer / FailedPayment / RecoveryAttempt models the rest of the system
uses — independent of the Postgres-pointed DATABASE_URL in .env, so this
runs without Docker. Re-running against the same payment id accumulates
RecoveryAttempt rows on that same case and will eventually hit the retry
cap / cooldown gates for real — a good way to see STAND_DOWN fire live.
"""

import argparse
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from src.pipeline import VerticalSliceResult, load_dataset_record, run_pipeline
from src.razorpay_action import RazorpayActionClient

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DB_PATH = DATA_DIR / "recovery_copilot.db"


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _print_input(dataset_record: dict) -> None:
    _section("INPUT")
    print(f"payment_id:         {dataset_record['external_payment_id']}")
    print(f"amount:             {dataset_record['amount']} {dataset_record['currency']}")
    print(f"failure_code:       {dataset_record['failure_code']}")
    print(f"failure_reason:     {dataset_record['failure_reason']}")
    print(f"failure_description:{dataset_record['failure_description']}")
    print(f"retry_count_so_far: {dataset_record['retry_count']}")
    customer_data = dataset_record["customer"]
    print(f"customer:           {customer_data['name']} <{customer_data['email']}>")
    print(f"dnd_opt_out:        {customer_data['dnd_opt_out']}")
    print(f"contact_count:      {customer_data['contact_count']}/{customer_data['max_contact_attempts']}")
    print(
        f"prior_history:      {customer_data['prior_recovery_attempts']} attempts, "
        f"{customer_data['prior_recovery_successes']} successes"
    )


def _print_result(result: VerticalSliceResult) -> None:
    d = result.diagnosis
    _section("DIAGNOSIS")
    print(f"root_cause:       {d.root_cause}")
    print(f"confidence:       {d.confidence:.2f} (raw, rule-derived — never passed to the policy engine directly)")
    print(f"confidence_band:  {d.confidence_band.value.upper()}")
    print(f"source:           {d.source.value}")
    print(f"evidence:         {d.evidence}")

    p = result.policy_decision
    _section("POLICY")
    print(f"decision:         {p.action.value.upper()}")
    print(f"reason:           {p.reason}")
    print("factors:")
    for key, value in p.factors.items():
        print(f"  {key}: {value}")

    a = result.action_outcome
    _section("ACTION")
    print(f"attempted:        {p.action.value}")
    print(f"action_mode:      {a.action_mode.value.upper()}")
    print(f"evidence:         {a.evidence}")

    _section("RESULT")
    print(f"action_result:            {a.action_result.value.upper()}")
    print(f"razorpay_reference:       {a.razorpay_reference or '(none)'}")
    print(f"failed_payment.status:      {result.failed_payment.status.value}")
    print(f"failed_payment.stop_reason: {result.failed_payment.stop_reason or '(none)'}")

    ra = result.recovery_attempt
    _section("AUDIT")
    print(f"recovery_attempt_id: {ra.id}")
    print(f"attempt_number:      {ra.attempt_number}")
    print(f"created_at:          {ra.created_at.isoformat()}")
    print("Persisted append-only — this row is never mutated by future attempts.")


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", choices=["a", "b", "c"], default="a", help="Canonical demo case to run (default: a).")
    parser.add_argument("--payment-id", default=None, help="Run a specific external_payment_id instead of a canonical case.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite file to persist to.")
    args = parser.parse_args()

    engine = create_engine(f"sqlite:///{args.db_path}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()

    try:
        dataset_record = load_dataset_record(data_dir=DATA_DIR, case=args.case, payment_id=args.payment_id)
        _print_input(dataset_record)

        razorpay_client = RazorpayActionClient()
        result = run_pipeline(db, dataset_record, razorpay_client=razorpay_client)

        _print_result(result)
        print(f"\n(DB: {args.db_path})")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
