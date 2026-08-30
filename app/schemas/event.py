import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.event import EventStatus, EventType


class RevenueEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_event_id: str
    event_type: EventType
    status: EventStatus
    customer_id: uuid.UUID
    amount: int
    currency: str
    error_code: str | None
    error_reason: str | None
    error_description: str | None
    retry_count: int
    created_at: datetime
    updated_at: datetime


class RevenueEventList(BaseModel):
    total: int
    items: list[RevenueEventRead]
