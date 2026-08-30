# Recovery Copilot

An agent that watches a merchant's Razorpay payment flow for money leaking
out — failed payments, abandoned checkouts, and failed subscription mandates
— diagnoses *why* each one failed, picks a bounded recovery action, executes
it against Razorpay's test-mode APIs, and reports how much was actually
recovered. Every decision is logged and explainable.

Built for Track 03 (AI Revenue Recovery), Razorpay AI Buildathon.

## Status

This is the Day 1 foundation: project scaffold, database schema, and a
synthetic dataset generator. The diagnosis engine, policy/decision layer,
Razorpay test-mode integration, and dashboard are not implemented yet — see
the build schedule below.

## Stack

- **API**: FastAPI + SQLAlchemy 2.0, Python 3.12
- **Database**: PostgreSQL, migrations via Alembic
- **Reasoning** (planned): Claude API for ambiguous root-cause classification
- **Payments** (planned): Razorpay test-mode APIs
- **Ops**: Docker Compose, pytest

## Project layout

```
app/
  core/config.py       # Settings, loaded from .env
  db/                   # Engine/session setup, cross-dialect GUID type
  models/               # SQLAlchemy models: Customer, RevenueEvent, AuditLogEntry
  schemas/               # Pydantic read models
  api/routes/            # FastAPI routers (health, read-only event listing)
  main.py                # FastAPI app
alembic/                 # DB migrations (source of truth for schema)
seed/
  payloads.py            # Builders for Razorpay-shaped synthetic webhook payloads
  generate_dataset.py    # Seeds customers + 50+ at-risk events
tests/                    # pytest, runs against in-memory SQLite
```

## Data model

Three tables carry the whole pipeline:

- **`customers`** — a merchant's end customer, plus the compliance bounds a
  future policy layer must respect: `max_contact_attempts`, `dnd_opt_out`.
- **`revenue_events`** — one unit of at-risk revenue. `event_type` is one of
  `failed_payment`, `failed_mandate`, `abandoned_checkout`. `raw_payload`
  stores the full synthetic event shaped like the real Razorpay webhook it
  stands in for (`payment.failed`, `subscription.charge.failed`,
  `payment_link.expired`), so downstream code reads it the same way it will
  read production data.
- **`audit_log_entries`** — append-only log of every diagnose/decide/act/stop
  step taken against an event. This is what the dashboard's audit trail and
  the batch report are built from.

## Running it

1. Copy `.env.example` to `.env`. Defaults work for local Docker Compose as-is;
   fill in `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` / `ANTHROPIC_API_KEY`
   when you have test-mode credentials.

   ```bash
   cp .env.example .env
   ```

2. Build and start Postgres + the API:

   ```bash
   docker compose up --build -d db
   docker compose run --rm api alembic upgrade head
   docker compose up --build -d api
   ```

3. Seed the synthetic dataset (50+ events across all three types):

   ```bash
   docker compose run --rm api python -m seed.generate_dataset --reset
   ```

4. Check it's up:

   ```bash
   curl http://localhost:8000/health
   curl http://localhost:8000/events
   ```

## Tests

Tests run against an in-memory SQLite database (via a cross-dialect UUID
type in `app/db/types.py`), so no Postgres container is required:

```bash
docker compose run --rm api pytest -q
```

## Build schedule

| Day | Focus |
| --- | --- |
| 1 | Repo scaffold, DB schema, synthetic dataset (this) |
| 2 | Rules-based diagnosis engine; Claude API fallback for ambiguous cases |
| 3 | Recovery/decision engine — retry backoff, stopping rules, compliance gating; Razorpay test-mode integration |
| 4 | Batch runner end-to-end; metrics; Prometheus + JSON logging; CI |
| 5 | Dashboard — at-risk pipeline, audit trail viewer, batch results |
| 6 | Pitch video, README/architecture doc, final test pass, submit |

## Out of scope for the buildathon

- B2B receivables chaser and Hinglish voice recovery (future work).
- Real production Razorpay credentials — test mode only, by design.
- A polished multi-tenant SaaS shell — this is a single-merchant demo.
