import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import GUID


class FailedPaymentStatus(str, enum.Enum):
    OPEN = "open"  # not yet processed by the recovery loop
    CONFIRMED_RECOVERED = "confirmed_recovered"  # real test-mode success
    SIMULATED_RECOVERED = "simulated_recovered"  # pipeline ran, modeled outcome, no live action
    ESCALATED = "escalated"  # deliberately left untouched: confidence or compliance didn't clear the bar
    UNRESOLVED = "unresolved"  # attempted or eligible, didn't succeed


class FailedPayment(Base):
    """A single failed Razorpay payment: the unit of at-risk revenue this
    system tries to recover. `raw_payload` stores the full synthetic event
    shaped like the real `payment.failed` webhook it stands in for, so the
    diagnosis engine reads it the same way it will read production data.

    `stop_reason` explains a non-recovered terminal status without requiring
    a re-derivation from the RecoveryAttempt trail — e.g.
    "max_attempts_reached", "cooldown_not_elapsed", "confidence_below_threshold",
    "high_value_uncertain", "dnd_opt_out", "contact_limit_reached".
    """

    __tablename__ = "failed_payments"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    external_payment_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    order_id: Mapped[str] = mapped_column(String(64))

    customer_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("customers.id"))

    amount: Mapped[int] = mapped_column(Integer)  # smallest currency unit (paise), matches Razorpay convention
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    failure_code: Mapped[str] = mapped_column(String(64))
    failure_reason: Mapped[str] = mapped_column(String(64))
    failure_description: Mapped[str] = mapped_column(String(255))

    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[FailedPaymentStatus] = mapped_column(
        Enum(FailedPaymentStatus, name="failed_payment_status"), default=FailedPaymentStatus.OPEN, index=True
    )
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    raw_payload: Mapped[dict] = mapped_column(JSON)

    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    customer: Mapped["Customer"] = relationship(back_populates="failed_payments")
    attempts: Mapped[list["RecoveryAttempt"]] = relationship(
        back_populates="failed_payment", order_by="RecoveryAttempt.attempt_number"
    )
