"""Tests for the run_vertical_slice CLI itself (not the pipeline, which is
covered by tests/test_vertical_slice.py): the --reset-db flag and graceful
handling of a database holding rows written under an older schema."""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.customer import Customer
from app.models.failed_payment import FailedPayment, FailedPaymentStatus
from app.models.recovery_attempt import ActionMode, ActionResult, ConfidenceBand, DecisionAction, DiagnosisSource, RecoveryAttempt
from src.pipeline import load_dataset_record
from src.run_vertical_slice import DATA_DIR, _stale_db_message, main, reset_db


def test_reset_db_deletes_an_existing_file(tmp_path):
    db_path = tmp_path / "test.db"
    db_path.write_text("not really a sqlite file, just needs to exist")
    assert db_path.exists()

    reset_db(db_path)

    assert not db_path.exists()


def test_reset_db_is_a_noop_when_the_file_does_not_exist(tmp_path):
    db_path = tmp_path / "does_not_exist.db"
    assert not db_path.exists()

    reset_db(db_path)  # must not raise

    assert not db_path.exists()


def test_stale_db_message_names_the_file_and_the_fix():
    message = _stale_db_message(
        "data/recovery_copilot.db", LookupError("'RULE_BASED' is not among the defined enum values")
    )
    assert "data/recovery_copilot.db" in message
    assert "--reset-db" in message
    assert "RULE_BASED" in message


def _seed_stale_recovery_attempt(db_path: Path, dataset_record: dict) -> None:
    """Writes a valid Customer/FailedPayment/RecoveryAttempt via the ORM,
    then corrupts the attempt's diagnosis_source with raw SQL to a value
    that predates the RULE_BASED -> RULE rename — reproducing "a row
    written under an older schema" without hand-writing a full INSERT
    against every column."""
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()

    customer = Customer(id=uuid.uuid4(), **dataset_record["customer"])
    db.add(customer)
    db.flush()

    failed_payment = FailedPayment(
        id=uuid.uuid4(),
        external_payment_id=dataset_record["external_payment_id"],
        order_id=dataset_record["order_id"],
        customer_id=customer.id,
        amount=dataset_record["amount"],
        currency=dataset_record["currency"],
        failure_code=dataset_record["failure_code"],
        failure_reason=dataset_record["failure_reason"],
        failure_description=dataset_record["failure_description"],
        retry_count=dataset_record["retry_count"],
        status=FailedPaymentStatus.OPEN,
        raw_payload=dataset_record["raw_payload"],
        failed_at=datetime.fromisoformat(dataset_record["failed_at"]),
    )
    db.add(failed_payment)
    db.flush()

    attempt = RecoveryAttempt(
        id=uuid.uuid4(),
        failed_payment_id=failed_payment.id,
        attempt_number=1,
        diagnosis_root_cause=dataset_record["failure_reason"],
        diagnosis_source=DiagnosisSource.RULE,
        confidence_band=ConfidenceBand.HIGH,
        diagnosis_reasoning="seeded for test",
        decision_action=DecisionAction.RETRY,
        decision_factors={},
        action_mode=ActionMode.SIMULATED,
        action_result=ActionResult.PENDING,
        ledger_sequence=0,
        previous_hash="0" * 64,
        content_hash="a" * 64,
    )
    db.add(attempt)
    db.commit()
    db.close()

    # Corrupt the enum value post-hoc — this is the "old schema" row the running code no longer knows.
    with engine.begin() as conn:
        conn.execute(text("UPDATE recovery_attempts SET diagnosis_source = 'RULE_BASED'"))
    engine.dispose()


def _disable_external_calls(monkeypatch) -> None:
    """Empty (not unset) — load_dotenv() defaults to not overriding a key
    that's already present in the environment, even if its value is "",
    so this reliably keeps RazorpayActionClient unconfigured (-> SIMULATED,
    no network call) regardless of what's in the real local .env."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "")
    monkeypatch.setenv("GROQ_API_KEY", "")


def test_stale_enum_row_gives_an_actionable_message_not_a_crash(tmp_path, monkeypatch, capsys):
    _disable_external_calls(monkeypatch)
    dataset_record = load_dataset_record(data_dir=DATA_DIR, case="a")
    db_path = tmp_path / "stale.db"
    _seed_stale_recovery_attempt(db_path, dataset_record)

    monkeypatch.setattr(
        sys,
        "argv",
        ["run_vertical_slice", "--db-path", str(db_path), "--payment-id", dataset_record["external_payment_id"]],
    )

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert str(db_path) in captured.out
    assert "--reset-db" in captured.out
    assert "Traceback" not in captured.out  # no raw stack trace leaked to the user


def test_reset_db_flag_avoids_the_stale_row_crash(tmp_path, monkeypatch, capsys):
    _disable_external_calls(monkeypatch)
    dataset_record = load_dataset_record(data_dir=DATA_DIR, case="a")
    db_path = tmp_path / "stale.db"
    _seed_stale_recovery_attempt(db_path, dataset_record)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_vertical_slice",
            "--db-path",
            str(db_path),
            "--payment-id",
            dataset_record["external_payment_id"],
            "--reset-db",
        ],
    )

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Reset local database" in captured.out
    assert "=== AUDIT ===" in captured.out  # ran the full pipeline successfully this time
