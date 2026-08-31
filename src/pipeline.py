"""Orchestrates one failed payment through diagnose -> policy -> action ->
observe -> audit. Takes a DB session and a dataset record dict (as produced
by seed/generate_dataset.py) and returns a VerticalSliceResult. Nothing here
prints — that's the callers' job (run_vertical_slice.py, run_batch.py).
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.failed_payment import FailedPayment, FailedPaymentStatus
from app.models.recovery_attempt import ActionMode, ActionResult, DecisionAction, DiagnosisSource, RecoveryAttempt
from src.diagnosis import Diagnosis, diagnose
from src.ledger import compute_content_hash, next_ledger_position
from src.policy import PolicyConfig, PolicyDecision, PolicyInput, decide
from src.razorpay_action import ActionOutcome


class ActionExecutor(Protocol):
    """Either RazorpayActionClient (real, HTTPS) or SimulatedActionExecutor
    (no network call) satisfies this — see src/razorpay_action.py and
    src/simulated_action.py."""

    def execute_retry(self, *, amount: int, currency: str, receipt: str) -> ActionOutcome: ...
    def execute_payment_link(self, *, amount: int, currency: str, description: str) -> ActionOutcome: ...


def load_dataset_record(*, data_dir: Path, case: str | None = None, payment_id: str | None = None) -> dict:
    dataset = json.loads((data_dir / "synthetic_failed_payments.json").read_text(encoding="utf-8"))
    by_id = {record["external_payment_id"]: record for record in dataset}

    if payment_id:
        if payment_id not in by_id:
            raise ValueError(f"No dataset record with external_payment_id={payment_id!r}")
        return by_id[payment_id]

    ground_truth = json.loads((data_dir / "ground_truth.json").read_text(encoding="utf-8"))
    label = f"case_{case}"
    match = next((gt for gt in ground_truth.values() if gt["canonical_demo_case"] == label), None)
    if match is None:
        raise ValueError(f"No canonical demo case tagged {label!r} in ground_truth.json")
    return by_id[match["external_payment_id"]]


def load_full_dataset(data_dir: Path) -> list[dict]:
    return json.loads((data_dir / "synthetic_failed_payments.json").read_text(encoding="utf-8"))


def load_ground_truth(data_dir: Path) -> dict:
    return json.loads((data_dir / "ground_truth.json").read_text(encoding="utf-8"))


def get_or_create_case(db: Session, dataset_record: dict) -> tuple[Customer, FailedPayment]:
    """Get-or-create by external id, so re-running the pipeline against the
    same dataset record accumulates RecoveryAttempt rows on the same
    FailedPayment instead of duplicating it — this is what lets repeated CLI
    runs naturally demonstrate the retry-cap and cooldown gates."""
    customer_data = dataset_record["customer"]
    customer = db.scalars(
        select(Customer).where(Customer.external_customer_id == customer_data["external_customer_id"])
    ).first()
    if customer is None:
        customer = Customer(id=uuid.uuid4(), **customer_data)
        db.add(customer)
        db.flush()

    failed_payment = db.scalars(
        select(FailedPayment).where(FailedPayment.external_payment_id == dataset_record["external_payment_id"])
    ).first()
    if failed_payment is None:
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

    return customer, failed_payment


def _next_attempt_number(db: Session, failed_payment: FailedPayment) -> int:
    """Counts prior RecoveryAttempt rows for this payment, regardless of
    action type — distinct from failed_payment.retry_count, which only
    tracks RETRY-specific attempts for the retry-cap policy gate."""
    count = db.scalar(
        select(func.count()).select_from(RecoveryAttempt).where(RecoveryAttempt.failed_payment_id == failed_payment.id)
    )
    return (count or 0) + 1


def _as_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip even for DateTime(timezone=True)
    columns (it has no native tz-aware type) — every value written here was
    UTC to begin with, so a naive read-back is safely re-tagged as UTC
    rather than compared incorrectly against an aware `now`."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _cooldown_elapsed(db: Session, failed_payment: FailedPayment, *, cooldown_seconds: int, now: datetime) -> bool:
    last_attempt = db.scalars(
        select(RecoveryAttempt)
        .where(RecoveryAttempt.failed_payment_id == failed_payment.id)
        .order_by(RecoveryAttempt.created_at.desc())
    ).first()
    reference_time = last_attempt.created_at if last_attempt is not None else failed_payment.failed_at
    return (_as_utc(now) - _as_utc(reference_time)) >= timedelta(seconds=cooldown_seconds)


def build_policy_input(
    customer: Customer,
    failed_payment: FailedPayment,
    diagnosis: Diagnosis,
    db: Session,
    *,
    config: PolicyConfig,
    now: datetime,
) -> PolicyInput:
    return PolicyInput(
        confidence_band=diagnosis.confidence_band,
        root_cause=diagnosis.root_cause,
        retryable=diagnosis.retryable,
        never_auto=diagnosis.never_auto,
        payment_value=failed_payment.amount,
        high_value_threshold=config.high_value_threshold,
        attempt_count=failed_payment.retry_count,
        max_attempts=config.max_attempts,
        cooldown_elapsed=_cooldown_elapsed(db, failed_payment, cooldown_seconds=config.cooldown_seconds, now=now),
        dnd_opt_out=customer.dnd_opt_out,
        contact_count=customer.contact_count,
        max_contact_attempts=customer.max_contact_attempts,
        prior_recovery_attempts=customer.prior_recovery_attempts,
        serial_failure_attempt_threshold=config.serial_failure_attempt_threshold,
        retry_cost=config.retry_cost_paise,
        payment_link_cost=config.payment_link_cost_paise,
        max_cost_fraction=config.max_cost_fraction_of_value,
    )


def _execute_action(
    action: DecisionAction, action_executor: ActionExecutor, failed_payment: FailedPayment, reason: str
) -> ActionOutcome:
    if action == DecisionAction.RETRY:
        return action_executor.execute_retry(
            amount=failed_payment.amount, currency=failed_payment.currency, receipt=failed_payment.external_payment_id
        )
    if action == DecisionAction.PAYMENT_LINK:
        return action_executor.execute_payment_link(
            amount=failed_payment.amount,
            currency=failed_payment.currency,
            description=f"Complete your payment for {failed_payment.external_payment_id}",
        )
    return ActionOutcome(
        action_mode=ActionMode.SIMULATED,
        action_result=ActionResult.NOT_EXECUTED,
        razorpay_reference=None,
        evidence=f"No Razorpay action executed: policy decision was {action.value} ({reason}).",
    )


def _apply_outcome(customer: Customer, failed_payment: FailedPayment, decision: PolicyDecision, outcome: ActionOutcome) -> None:
    if decision.action == DecisionAction.RETRY:
        failed_payment.retry_count += 1
    elif decision.action == DecisionAction.PAYMENT_LINK:
        customer.contact_count += 1

    if outcome.action_result == ActionResult.SUCCEEDED:
        # Reachable from the SIMULATED batch executor (src/simulated_action.py) — a MODELED outcome,
        # never from the REAL executor (src/razorpay_action.py), which never reports SUCCEEDED.
        failed_payment.status = (
            FailedPaymentStatus.CONFIRMED_RECOVERED
            if outcome.action_mode == ActionMode.REAL
            else FailedPaymentStatus.SIMULATED_RECOVERED
        )
        failed_payment.stop_reason = None
    elif decision.action == DecisionAction.STAND_DOWN:
        if decision.reason == "max_attempts_reached":
            failed_payment.status = FailedPaymentStatus.UNRESOLVED
            failed_payment.stop_reason = decision.reason
        elif decision.reason == "cooldown_not_elapsed":
            # A temporary pacing gate, not a terminal outcome: this case is still eligible, it just
            # needs to wait out the cooldown window. Leave status/stop_reason untouched so it isn't
            # miscounted as escalated or unresolved — it will be re-evaluated on a later run.
            pass
        else:
            failed_payment.status = FailedPaymentStatus.ESCALATED
            failed_payment.stop_reason = decision.reason
    elif decision.action == DecisionAction.HUMAN_REVIEW:
        failed_payment.status = FailedPaymentStatus.ESCALATED
        failed_payment.stop_reason = decision.reason
    # RETRY/PAYMENT_LINK with a PENDING or FAILED result stay OPEN: the case is still in flight
    # (or the API call itself failed) — no conclusive outcome exists yet, so status must not move
    # to a terminal value. See razorpay_action.py's module docstring for why PENDING, not SUCCEEDED.


@dataclass
class VerticalSliceResult:
    customer: Customer
    failed_payment: FailedPayment
    diagnosis: Diagnosis
    policy_decision: PolicyDecision
    action_outcome: ActionOutcome
    recovery_attempt: RecoveryAttempt


def run_pipeline(
    db: Session,
    dataset_record: dict,
    *,
    action_executor: ActionExecutor,
    policy_config: PolicyConfig | None = None,
    now: datetime | None = None,
    llm_client: Any | None = None,
) -> VerticalSliceResult:
    config = policy_config or PolicyConfig()
    now = now or datetime.now(timezone.utc)

    customer, failed_payment = get_or_create_case(db, dataset_record)

    diagnosis = diagnose(failed_payment, llm_client=llm_client)
    policy_input = build_policy_input(customer, failed_payment, diagnosis, db, config=config, now=now)
    decision = decide(policy_input)
    outcome = _execute_action(decision.action, action_executor, failed_payment, decision.reason)

    attempt_number = _next_attempt_number(db, failed_payment)
    model_reported_confidence = diagnosis.confidence if diagnosis.source == DiagnosisSource.LLM else None
    ledger_sequence, previous_hash = next_ledger_position(db)

    recovery_attempt = RecoveryAttempt(
        id=uuid.uuid4(),
        failed_payment_id=failed_payment.id,
        attempt_number=attempt_number,
        diagnosis_root_cause=diagnosis.root_cause,
        diagnosis_source=diagnosis.source,
        model_reported_confidence=model_reported_confidence,
        confidence_band=diagnosis.confidence_band,
        diagnosis_reasoning=diagnosis.evidence,
        decision_action=decision.action,
        decision_factors=decision.factors,
        action_mode=outcome.action_mode,
        action_result=outcome.action_result,
        razorpay_reference=outcome.razorpay_reference,
        created_at=now,
        ledger_sequence=ledger_sequence,
        previous_hash=previous_hash,
        content_hash="",  # placeholder — computed below, once every other field is set
    )
    recovery_attempt.content_hash = compute_content_hash(recovery_attempt, previous_hash=previous_hash)
    db.add(recovery_attempt)

    _apply_outcome(customer, failed_payment, decision, outcome)

    db.commit()
    db.refresh(recovery_attempt)
    db.refresh(failed_payment)
    db.refresh(customer)

    return VerticalSliceResult(
        customer=customer,
        failed_payment=failed_payment,
        diagnosis=diagnosis,
        policy_decision=decision,
        action_outcome=outcome,
        recovery_attempt=recovery_attempt,
    )
