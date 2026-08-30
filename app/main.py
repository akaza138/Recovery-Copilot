from fastapi import FastAPI

from app.api.routes import events, health

app = FastAPI(
    title="Recovery Copilot",
    description="Diagnoses and recovers at-risk Razorpay revenue (failed payments, failed mandates, abandoned checkouts) within auditable, bounded guardrails.",
    version="0.1.0",
)

app.include_router(health.router)
app.include_router(events.router)
