import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_customer_id: str
    name: str
    email: str
    phone: str
    max_contact_attempts: int
    dnd_opt_out: bool
    created_at: datetime
