# Importing every model here (rather than from app.db.base) registers them
# all on Base.metadata regardless of which one gets imported first —
# app.db.base only defines Base and never reaches back into app.models, so
# there's no circular-import path for any import order to trip over.
from app.models.customer import Customer  # noqa: F401
from app.models.failed_payment import FailedPayment  # noqa: F401
from app.models.recovery_attempt import RecoveryAttempt  # noqa: F401
