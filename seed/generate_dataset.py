"""Generates the synthetic failed-payment batch: 50+ records spread across
easy and deliberately-hard cases (see case_catalog.py), each carrying a
Razorpay-shaped `payment.failed` webhook payload.

Writes two files:
  - data/synthetic_failed_payments.json  — what the engine is allowed to see
  - data/ground_truth.json               — expected diagnosis/decision/outcome,
                                            keyed by external_payment_id, used
                                            to score the batch later. Never
                                            fed into the engine itself.

Usage:
    python -m seed.generate_dataset [--out-dir data] [--seed 42] [--load-db]

`--load-db` additionally loads the batch into Postgres via the SQLAlchemy
models (requires the full app dependency stack + DATABASE_URL); the default
JSON-only path has no such dependency, since the dataset needs to exist and
be inspectable before the DB-backed engine does.
"""

import argparse
import json
from pathlib import Path

from seed.case_catalog import generate_cases


def write_json_dataset(out_dir: Path, cases: list[dict]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    dataset_path = out_dir / "synthetic_failed_payments.json"
    ground_truth_path = out_dir / "ground_truth.json"

    dataset = [case["dataset_record"] for case in cases]
    ground_truth = {case["ground_truth"]["external_payment_id"]: case["ground_truth"] for case in cases}

    dataset_path.write_text(json.dumps(dataset, indent=2), encoding="utf-8")
    ground_truth_path.write_text(json.dumps(ground_truth, indent=2), encoding="utf-8")

    return dataset_path, ground_truth_path


def load_into_db(cases: list[dict]) -> None:
    import uuid
    from datetime import datetime

    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.models.customer import Customer
    from app.models.failed_payment import FailedPayment, FailedPaymentStatus

    Base.metadata.create_all(bind=engine)  # convenience for local/dev runs; Alembic remains the source of truth for schema.

    db = SessionLocal()
    try:
        db.query(FailedPayment).delete()
        db.query(Customer).delete()
        db.commit()

        for case in cases:
            record = case["dataset_record"]
            customer_data = record["customer"]

            customer = Customer(id=uuid.uuid4(), **customer_data)
            db.add(customer)
            db.flush()

            db.add(
                FailedPayment(
                    id=uuid.uuid4(),
                    external_payment_id=record["external_payment_id"],
                    order_id=record["order_id"],
                    customer_id=customer.id,
                    amount=record["amount"],
                    currency=record["currency"],
                    failure_code=record["failure_code"],
                    failure_reason=record["failure_reason"],
                    failure_description=record["failure_description"],
                    retry_count=record["retry_count"],
                    status=FailedPaymentStatus.OPEN,
                    raw_payload=record["raw_payload"],
                    failed_at=datetime.fromisoformat(record["failed_at"]),
                )
            )

        db.commit()
        print(f"Loaded {len(cases)} customers/failed_payments into the database.")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--load-db", action="store_true", help="Also load the batch into Postgres via the SQLAlchemy models.")
    args = parser.parse_args()

    cases = generate_cases(seed=args.seed)
    dataset_path, ground_truth_path = write_json_dataset(args.out_dir, cases)

    by_category: dict[str, int] = {}
    for case in cases:
        category = case["ground_truth"]["category"]
        by_category[category] = by_category.get(category, 0) + 1

    print(f"Wrote {len(cases)} records to {dataset_path} and {ground_truth_path}")
    for category, count in sorted(by_category.items()):
        print(f"  {category}: {count}")

    if args.load_db:
        load_into_db(cases)


if __name__ == "__main__":
    main()
