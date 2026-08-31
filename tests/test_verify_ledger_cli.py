"""Tests for the verify-ledger CLI (src/verify_ledger.py) — the standalone
check someone should be able to run against a DB file alone, independent
of a batch run."""

import sys

from sqlalchemy import create_engine, text

from app.db.base import Base
from src.pipeline import load_dataset_record, run_pipeline
from src.run_batch import DATA_DIR
from src.simulated_action import SimulatedActionExecutor
from src.verify_ledger import main


def _write_intact_ledger(db_path, *, n: int = 3) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    from sqlalchemy.orm import sessionmaker

    session_factory = sessionmaker(bind=engine)
    executor = SimulatedActionExecutor()
    record = load_dataset_record(data_dir=DATA_DIR, case="a")
    for i in range(n):
        db = session_factory()
        try:
            record = dict(record, external_payment_id=f"pay_cli_verify_{i}", order_id=f"order_cli_verify_{i}")
            record["customer"] = dict(record["customer"], external_customer_id=f"cust_cli_verify_{i}")
            run_pipeline(db, record, action_executor=executor)
        finally:
            db.close()
    engine.dispose()


def test_missing_db_file_reports_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "does_not_exist.db"
    monkeypatch.setattr(sys, "argv", ["verify_ledger", "--db-path", str(db_path)])

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No database found" in captured.out


def test_intact_ledger_reports_success_and_exits_zero(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "intact.db"
    _write_intact_ledger(db_path, n=3)
    monkeypatch.setattr(sys, "argv", ["verify_ledger", "--db-path", str(db_path)])

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "intact" in captured.out
    assert "3 rows verified" in captured.out


def test_tampered_ledger_reports_the_breaking_row_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "tampered.db"
    _write_intact_ledger(db_path, n=4)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("UPDATE recovery_attempts SET diagnosis_reasoning = 'TAMPERED' WHERE ledger_sequence = 1"))
    engine.dispose()

    monkeypatch.setattr(sys, "argv", ["verify_ledger", "--db-path", str(db_path)])

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "LEDGER TAMPERED" in captured.out
    assert "ledger_sequence=1" in captured.out
    assert "verified 1/4" in captured.out
