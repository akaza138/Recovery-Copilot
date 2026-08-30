import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuditLogEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    actor: str
    action: str
    reasoning: str
    confidence: float | None
    extra_data: dict | None
    created_at: datetime
