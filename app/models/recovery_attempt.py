import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import GUID


class DiagnosisSource(str, enum.Enum):
    RULE_BASED = "rule_based"
    CLAUDE = "claude"


class ConfidenceBand(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DecisionAction(str, enum.Enum):
    RETRY = "retry"
    PAYMENT_LINK = "payment_link"
    HUMAN_REVIEW = "human_review"  # uncertain diagnosis, or a safety-critical case (e.g. risk block) — a person decides next
    STAND_DOWN = "stand_down"  # policy refuses to act and does not queue for human attention (compliance block, cap hit, too-low confidence)


class ActionMode(str, enum.Enum):
    """Whether the action was actually executed against Razorpay test mode,
    or only simulated because no safe test-mode equivalent exists. Required
    on every row — never defaulted — so a recovered outcome can never be
    ambiguous about whether it's real."""

    REAL = "real"
    SIMULATED = "simulated"


class ActionResult(str, enum.Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PENDING = "pending"
    NOT_EXECUTED = "not_executed"  # HUMAN_REVIEW / STAND_DOWN take no Razorpay action


class RecoveryAttempt(Base):
    """One full diagnose -> decide -> act -> observe cycle for a
    FailedPayment. This row *is* the audit trail: append-only, never
    mutated, and holds every field needed to explain the decision after the
    fact without re-deriving anything.

    `model_reported_confidence` is the raw, uncalibrated number reported by
    Claude (or null for a rule-based diagnosis). Nothing downstream may read
    it directly — only `confidence_band` (HIGH/MEDIUM/LOW), which is derived
    from it once and stored, is allowed to drive the policy decision.

    `decision_factors` is a snapshot of every input the policy engine used
    to reach `decision_action` (payment value, failure type, confidence
    band, attempt count, cooldown elapsed, contact count/limit, compliance
    status) — captured at decision time, not reconstructed later.
    """

    __tablename__ = "recovery_attempts"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    failed_payment_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("failed_payments.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)

    # Diagnosis
    diagnosis_root_cause: Mapped[str] = mapped_column(String(64))
    diagnosis_source: Mapped[DiagnosisSource] = mapped_column(Enum(DiagnosisSource, name="diagnosis_source"))
    model_reported_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_band: Mapped[ConfidenceBand] = mapped_column(Enum(ConfidenceBand, name="confidence_band"))
    diagnosis_reasoning: Mapped[str] = mapped_column(String(1000))

    # Decision
    decision_action: Mapped[DecisionAction] = mapped_column(Enum(DecisionAction, name="decision_action"))
    decision_factors: Mapped[dict] = mapped_column(JSON)

    # Action
    action_mode: Mapped[ActionMode] = mapped_column(Enum(ActionMode, name="action_mode"))
    action_result: Mapped[ActionResult] = mapped_column(Enum(ActionResult, name="action_result"))
    razorpay_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    failed_payment: Mapped["FailedPayment"] = relationship(back_populates="attempts")
