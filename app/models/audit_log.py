import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import GUID


class AuditLogEntry(Base):
    """Append-only record of one diagnose/decide/act/stop step taken against
    a RevenueEvent. This is the audit trail the dashboard renders and the
    batch report is built from — every decision must be traceable back to a
    reason here, not just an outcome.
    """

    __tablename__ = "audit_log_entries"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("revenue_events.id"), index=True)

    actor: Mapped[str] = mapped_column(String(32))  # "rules_engine" | "claude" | "policy" | "human"
    action: Mapped[str] = mapped_column(String(64))  # e.g. "diagnose", "retry_now", "escalate", "stand_down"
    reasoning: Mapped[str] = mapped_column(String(1000))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    extra_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    event: Mapped["RevenueEvent"] = relationship(back_populates="audit_entries")
