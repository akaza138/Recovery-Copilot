import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import GUID


class EventType(str, enum.Enum):
    FAILED_PAYMENT = "failed_payment"
    FAILED_MANDATE = "failed_mandate"
    ABANDONED_CHECKOUT = "abandoned_checkout"


class EventStatus(str, enum.Enum):
    OPEN = "open"
    RECOVERED = "recovered"
    EXHAUSTED = "exhausted"  # hit the retry/attempt cap without recovering
    ESCALATED = "escalated"  # handed off to a human queue


class RevenueEvent(Base):
    """One unit of at-risk revenue: a failed payment, a failed subscription
    mandate charge, or an abandoned checkout. `raw_payload` stores the
    synthetic event shaped like the real Razorpay webhook it stands in for,
    so the diagnosis engine reads it the same way it would read production
    data.
    """

    __tablename__ = "revenue_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    external_event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    event_type: Mapped[EventType] = mapped_column(Enum(EventType, name="event_type"), index=True)
    status: Mapped[EventStatus] = mapped_column(Enum(EventStatus, name="event_status"), default=EventStatus.OPEN, index=True)

    customer_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("customers.id"))

    amount: Mapped[int] = mapped_column(Integer)  # smallest currency unit (paise), matches Razorpay convention
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    # Full synthetic Razorpay-shaped payload (payment.failed / subscription.charge.failed / custom abandoned-checkout shape).
    raw_payload: Mapped[dict] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    customer: Mapped["Customer"] = relationship(back_populates="events")
    audit_entries: Mapped[list["AuditLogEntry"]] = relationship(back_populates="event", order_by="AuditLogEntry.created_at")
