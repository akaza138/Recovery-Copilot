"""CLI: independently verifies the tamper-evident audit ledger's SHA-256
hash chain (src/ledger.py) for a given SQLite database, without going
through a batch run — the check anyone (a reviewer, an auditor) should be
able to run against the DB file alone.

Usage:
    python -m src.verify_ledger                                   # verifies data/batch_run.db (the full-batch ledger)
    python -m src.verify_ledger --db-path data/recovery_copilot.db  # verifies the vertical-slice DB instead

Exit code 0 if the ledger is intact (or empty); 1 if the file is missing or
the chain is broken.
"""

import argparse
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from src.ledger import verify_ledger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DB_PATH = DATA_DIR / "batch_run.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="SQLite file whose recovery_attempts ledger to verify.")
    args = parser.parse_args()

    if not args.db_path.exists():
        print(f"No database found at {args.db_path} — nothing to verify.")
        return 1

    engine = create_engine(f"sqlite:///{args.db_path}")
    Base.metadata.create_all(bind=engine)  # no-op if the schema already matches; lets this run against a stray empty file too
    db = sessionmaker(bind=engine)()
    try:
        result = verify_ledger(db)
    finally:
        db.close()

    if result.total_rows == 0:
        print(f"Ledger at {args.db_path} is empty — nothing to verify.")
        return 0

    if result.intact:
        print(f"Ledger intact: {result.rows_verified} rows verified ({args.db_path}).")
        return 0

    print(f"LEDGER TAMPERED: verified {result.rows_verified}/{result.total_rows} rows before the chain broke.")
    print(f"  Breaks at ledger_sequence={result.broken_at_sequence} (row id={result.broken_row_id})")
    print(f"  {result.detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
