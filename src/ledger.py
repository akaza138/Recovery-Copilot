"""Tamper-evident audit ledger. Every RecoveryAttempt row already IS the
append-only audit trail (see the model's docstring) — this module adds the
part that makes tampering *detectable*, not just discouraged: each row's
`content_hash` is SHA-256 of its own canonical field snapshot chained onto
the previous row's `content_hash` (`previous_hash`). Editing any field in
any row, even one nobody looks at again, changes that row's own
content_hash and therefore invalidates every row written after it —
`verify_ledger` walks the whole chain and reports exactly where it breaks.

This is a hash chain, the same structure behind any tamper-evident log
(git commits, blockchains, certificate transparency logs) — not a
cryptographic signature and not a substitute for real access control on the
database. It proves "this row's recorded content matches what was written
at the time", nothing about who could have written it.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.recovery_attempt import RecoveryAttempt

GENESIS_HASH = "0" * 64  # previous_hash for the very first row ever written to a given DB


def _as_utc_isoformat(value) -> str | None:
    """SQLite drops tzinfo on round-trip even for DateTime(timezone=True)
    columns (see src/pipeline.py's `_as_utc` for the same note) — a value
    read back after a commit expires the row is naive UTC, while the same
    object right after construction is tz-aware UTC. Both must hash
    identically, so this always re-tags a naive value as UTC before
    formatting rather than trusting whatever tzinfo happens to be present."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def canonical_fields(attempt: RecoveryAttempt) -> dict:
    """Every field that constitutes this row's recorded content — deliberately
    excludes ledger_sequence/previous_hash/content_hash themselves, which are
    the chain's output, not its input."""
    return {
        "id": str(attempt.id),
        "failed_payment_id": str(attempt.failed_payment_id),
        "attempt_number": attempt.attempt_number,
        "diagnosis_root_cause": attempt.diagnosis_root_cause,
        "diagnosis_source": attempt.diagnosis_source.value,
        "model_reported_confidence": attempt.model_reported_confidence,
        "confidence_band": attempt.confidence_band.value,
        "diagnosis_reasoning": attempt.diagnosis_reasoning,
        "decision_action": attempt.decision_action.value,
        "decision_factors": attempt.decision_factors,
        "action_mode": attempt.action_mode.value,
        "action_result": attempt.action_result.value,
        "razorpay_reference": attempt.razorpay_reference,
        "created_at": _as_utc_isoformat(attempt.created_at),
    }


def compute_content_hash(attempt: RecoveryAttempt, *, previous_hash: str) -> str:
    canonical = json.dumps(canonical_fields(attempt), sort_keys=True, default=str)
    payload = f"{previous_hash}|{canonical}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def next_ledger_position(db: Session) -> tuple[int, str]:
    """Returns (next ledger_sequence, previous_hash to chain onto) for a new
    row about to be written — the sequence and hash of whatever is currently
    the last row in the ledger, or the genesis values if the ledger is empty.
    Callers must use the SAME db session to both call this and insert the
    new row within one transaction, so a concurrent writer can't interleave
    between the read here and the write there."""
    last = db.scalars(select(RecoveryAttempt).order_by(RecoveryAttempt.ledger_sequence.desc())).first()
    if last is None:
        return 0, GENESIS_HASH
    return last.ledger_sequence + 1, last.content_hash


@dataclass(frozen=True)
class LedgerVerificationResult:
    intact: bool
    total_rows: int
    rows_verified: int  # rows confirmed intact before hitting a break (== total_rows when intact)
    broken_at_sequence: int | None
    broken_row_id: str | None
    detail: str | None


def verify_ledger(db: Session) -> LedgerVerificationResult:
    total = db.scalar(select(func.count()).select_from(RecoveryAttempt)) or 0
    rows = db.scalars(select(RecoveryAttempt).order_by(RecoveryAttempt.ledger_sequence.asc())).all()

    expected_previous = GENESIS_HASH
    for verified_count, row in enumerate(rows):
        if row.previous_hash != expected_previous:
            return LedgerVerificationResult(
                intact=False,
                total_rows=total,
                rows_verified=verified_count,
                broken_at_sequence=row.ledger_sequence,
                broken_row_id=str(row.id),
                detail=(
                    f"row at ledger_sequence={row.ledger_sequence} (id={row.id}) has previous_hash="
                    f"'{row.previous_hash}', but the previous row's actual content_hash is "
                    f"'{expected_previous}' — the chain link itself was tampered with, or a row was "
                    f"deleted/reordered."
                ),
            )
        recomputed = compute_content_hash(row, previous_hash=expected_previous)
        if recomputed != row.content_hash:
            return LedgerVerificationResult(
                intact=False,
                total_rows=total,
                rows_verified=verified_count,
                broken_at_sequence=row.ledger_sequence,
                broken_row_id=str(row.id),
                detail=(
                    f"row at ledger_sequence={row.ledger_sequence} (id={row.id}) has content_hash="
                    f"'{row.content_hash}', but recomputing it from the row's current field values "
                    f"gives '{recomputed}' — this row's content was altered after it was written."
                ),
            )
        expected_previous = row.content_hash

    return LedgerVerificationResult(
        intact=True, total_rows=total, rows_verified=len(rows), broken_at_sequence=None, broken_row_id=None, detail=None
    )
