from fastapi import FastAPI

from app.api.routes import health

app = FastAPI(
    title="Recovery Copilot",
    description=(
        "An AI-assisted revenue recovery engine for failed Razorpay payments: "
        "deterministic rules plus a bounded Claude diagnosis for ambiguous cases, "
        "with a deterministic policy engine making every action decision."
    ),
    version="0.1.0",
)

app.include_router(health.router)

# Read endpoints over FailedPayment / RecoveryAttempt (batch results, audit
# trail viewer) are rebuilt once the vertical slice and batch runner exist —
# see README build order.
