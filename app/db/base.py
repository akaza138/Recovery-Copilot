from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# Import models here so Alembic's autogenerate can discover them via Base.metadata.
from app.models.customer import Customer  # noqa: E402,F401
from app.models.event import RevenueEvent  # noqa: E402,F401
from app.models.audit_log import AuditLogEntry  # noqa: E402,F401
