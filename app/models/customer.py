import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import GUID


class Customer(Base):
    """A merchant's end customer, plus the history and compliance bounds the
    policy engine must read before attempting a recovery contact.

    `prior_recovery_attempts` / `prior_recovery_successes` capture history
    from *before* the current batch (this system has no other memory of past
    runs), so a synthetic customer can represent a "serial failure" case
    without needing a live event history to derive it from.
    """

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    external_customer_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(20))

    # Compliance / stopping-rule inputs.
    dnd_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)
    max_contact_attempts: Mapped[int] = mapped_column(Integer, default=3)
    contact_count: Mapped[int] = mapped_column(Integer, default=0)

    # History prior to this batch.
    prior_recovery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    prior_recovery_successes: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    failed_payments: Mapped[list["FailedPayment"]] = relationship(back_populates="customer")
