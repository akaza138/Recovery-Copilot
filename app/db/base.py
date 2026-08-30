from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# Import models here so Alembic's autogenerate can discover them via Base.metadata.
from app.models.customer import Customer  # noqa: E402,F401
from app.models.failed_payment import FailedPayment  # noqa: E402,F401
from app.models.recovery_attempt import RecoveryAttempt  # noqa: E402,F401
