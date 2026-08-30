# Recovery Copilot

An AI-assisted revenue recovery engine for failed Razorpay payments. It is
**not an autonomous agent**: deterministic rules do the obvious diagnosis
work, a Claude API call is reserved for genuinely ambiguous cases, and a
deterministic policy engine — never the model — makes the final call on
whether to act. The goal is not to maximize automated actions; it's to
maximize *safely* recovered revenue, and to visibly refuse to act when
confidence is too low on a high-value transaction.

Built for Track 03 (AI Revenue Recovery), Razorpay AI Buildathon (submission
deadline September 5, 2026).

## The one hero flow

```
Failed payment → diagnose why → decide if recovery is worth attempting →
check policy → execute or refuse the action → observe result → stop or
escalate → record to audit trail → update the honest metrics.
```

No subscription recovery, no checkout abandonment, no dashboard, no
Docker/Prometheus/CI until this works end to end. See the build order below.

## Status

Day 2: the vertical slice runs end to end for one payment — diagnose ->
policy -> real Razorpay test-mode action -> observe -> append-only audit
record — via `python -m src.run_vertical_slice`. See
[src/](src/) for the diagnosis engine, policy engine, and Razorpay action
executor. Not yet built: the full-batch runner, Claude-assisted diagnosis
for unfamiliar failure reasons, and the dashboard.

## Stack

- **API**: FastAPI + SQLAlchemy 2.0, Python 3.12
- **Database**: PostgreSQL, migrations via Alembic
- **Reasoning**: Claude API (Anthropic SDK), for ambiguous-case diagnosis only —
  it proposes a root cause and a confidence band; it never decides the action.
- **Payments**: Razorpay test-mode REST API (Orders, Payment Links)
- **Ops**: Docker Compose, pytest — kept minimal until the vertical slice works

## Data model

Three tables:

- **`Customer`** ([app/models/customer.py](app/models/customer.py)) — a merchant's end
  customer, plus compliance/history fields the policy engine reads:
  `dnd_opt_out`, `max_contact_attempts` / `contact_count`, and
  `prior_recovery_attempts` / `prior_recovery_successes` (history from before
  this batch — this system has no other memory of past runs).
- **`FailedPayment`** ([app/models/failed_payment.py](app/models/failed_payment.py)) — the
  event: gateway ids, amount, `failure_code`/`failure_reason`, `raw_payload`
  (the full synthetic event shaped like the real `payment.failed` webhook),
  `retry_count`, and a `status` in `{OPEN, CONFIRMED_RECOVERED,
  SIMULATED_RECOVERED, ESCALATED, UNRESOLVED}` — the same four categories the
  batch report uses, plus OPEN. `stop_reason` explains a non-recovered
  outcome (e.g. `max_attempts_reached`, `dnd_opt_out`,
  `confidence_below_threshold_for_value`) without needing to re-derive it.
- **`RecoveryAttempt`** ([app/models/recovery_attempt.py](app/models/recovery_attempt.py)) —
  one immutable row per diagnose→decide→act→observe cycle. This row *is* the
  audit trail — append-only, never mutated. It holds:
  - **diagnosis**: `diagnosis_root_cause`, `diagnosis_source`
    (RULE_BASED/CLAUDE), `model_reported_confidence` (raw, nullable — never
    read downstream) and `confidence_band` (HIGH/MEDIUM/LOW — the only thing
    the policy engine is allowed to act on)
  - **decision**: `decision_action` (RETRY/PAYMENT_LINK/HUMAN_REVIEW/STAND_DOWN) and
    `decision_factors` (a JSON snapshot of every input used — payment value,
    failure type, confidence band, attempt count, cooldown, contact
    count/limit, compliance status — captured at decision time, not
    reconstructed later)
  - **action**: `action_mode` (REAL/SIMULATED — required on every row, never
    defaulted, so a recovered outcome can never be ambiguous about whether
    it's real) and `action_result` (SUCCEEDED/FAILED/PENDING/NOT_EXECUTED)

## Synthetic dataset

[seed/case_catalog.py](seed/case_catalog.py) defines 15 case templates (6 easy, 9
deliberately hard), expanded into 60 records by
[seed/generate_dataset.py](seed/generate_dataset.py):

```bash
python -m seed.generate_dataset
```

Writes two files:

- **[data/synthetic_failed_payments.json](data/synthetic_failed_payments.json)** — what
  the engine is allowed to see. No expected outcomes, no case labels.
- **[data/ground_truth.json](data/ground_truth.json)** — expected root cause,
  confidence band, decision action, and final status, keyed by
  `external_payment_id`. Used to score the batch later; never fed to the
  engine.

Hard cases cover: conflicting signals, an unfamiliar/unrated failure reason,
a payment already at the retry cap, an opted-out (DND) customer, a customer
whose contact limit is already used up, a high-value payment with an
uncertain diagnosis, a customer with a poor recovery track record, and a
risk-engine block (never auto-retried, regardless of confidence).

### The three demo cases

One instance of three specific templates is flagged
`canonical_demo_case` in the ground-truth file:

- **Case A** (`issuer_timeout_retry_success`) — transient failure → policy
  approves a retry → expected to succeed in test mode.
- **Case B** (`expired_card_payment_link_success`) — non-retryable failure →
  system skips straight to a payment link instead of a pointless retry.
- **Case C** (`high_value_uncertain_refuse`) — ambiguous, high-value failure
  → model-reported confidence (~71%, `representative_model_confidence` in
  the ground truth) falls short of the threshold required for a payment
  this size → system **refuses** automatic action and escalates instead of
  guessing.

## Razorpay test-mode connectivity check

Everything downstream depends on test-mode credentials actually working, so
this is checked before any engine code is written:

```bash
python -m scripts.check_razorpay_keys
```

Calls the Orders and Payment Links APIs directly over HTTPS (Basic Auth) —
not the `razorpay` PyPI package, which pulls in a legacy `pkg_resources`
import current `setuptools` no longer ships. Refuses to run against
anything that isn't a `rzp_test_...` key.

## Running the vertical slice

```bash
python -m src.run_vertical_slice            # canonical Case A (transient failure -> retry)
python -m src.run_vertical_slice --case b     # canonical Case B (non-retryable -> payment link)
python -m src.run_vertical_slice --case c      # canonical Case C (high-value + uncertain -> human review)
python -m src.run_vertical_slice --payment-id pay_xxx   # any specific dataset record
```

Prints `INPUT` / `DIAGNOSIS` / `POLICY` / `ACTION` / `RESULT` / `AUDIT` for
one payment and persists the audit record to a local SQLite file
(`data/recovery_copilot.db`) via the same `Customer` / `FailedPayment` /
`RecoveryAttempt` models the rest of the system uses — independent of the
Postgres-pointed `DATABASE_URL` in `.env`, so this runs without Docker.
Re-running against the same payment id accumulates `RecoveryAttempt` rows on
that case and will eventually hit the retry-cap / cooldown gates for real.

**Honesty constraint, load-bearing:** [src/razorpay_action.py](src/razorpay_action.py)
never reports `ActionResult.SUCCEEDED`. Creating a Razorpay order or payment
link is something the API response genuinely confirms; a *completed*
payment is not — that requires the customer to finish checkout, and this
project will not submit card data server-side to fake that. So a
successfully placed RETRY or PAYMENT_LINK action is `REAL` + `PENDING`,
never `SUCCEEDED`, until a later step adds a real confirmation path (webhook
receipt or status polling).

## Running the API

```bash
cp .env.example .env   # fill in RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET / ANTHROPIC_API_KEY
docker compose up --build -d db
docker compose run --rm api alembic upgrade head
docker compose up --build -d api
curl http://localhost:8000/health
```

## Tests

```bash
pytest -q
```

Runs against an in-memory SQLite database (via a cross-dialect UUID type in
[app/db/types.py](app/db/types.py)), so no Postgres container is required. 51
tests covering: the models, the case catalog (record count, no
answer-leakage into the dataset, the three canonical demo cases, determinism
for a given seed), the diagnosis engine, the policy engine (every gate in
the requirement list, individually and in combination), the Razorpay action
executor (mocked transport — never hits the network, never returns
`SUCCEEDED` regardless of response shape), the vertical-slice pipeline
end-to-end (successful retry, policy refusal, DND refusal, retry-cap
refusal, audit record creation, no fake recovered result), and the health
endpoint.

## Build order

| Step | Focus |
| --- | --- |
| 1 (done) | Synthetic dataset + data model; confirmed Razorpay test-mode keys work |
| 2 (done) | Vertical slice: one failed payment through diagnose → policy → real Razorpay test-mode action → result → append-only audit record, runnable from the CLI |
| 3 | Extend to the full 60-record batch runner; wire in Claude for the failure reasons the rule table doesn't recognize, with HIGH/MEDIUM/LOW confidence banding; add a real payment-completion confirmation path (webhook or polling) so `CONFIRMED_RECOVERED` becomes reachable |
| 4 | Harden the policy engine's refusal path further — tests that try to break it — and decide whether customer track record becomes a policy factor |
| 5 | Minimal UI: batch results by the four outcome categories, audit trail viewer |
| 6 (time permitting) | Docker, structured logging/Prometheus, CI, README polish, additional flows |

## Metrics (batch report, once the engine exists)

Four categories, never blended: **Confirmed recovered** (real test-mode
success), **Simulated recovery** (pipeline ran, modeled outcome, no live
action — only when no safe test-mode equivalent exists), **Escalated**
(deliberately untouched), **Unresolved** (attempted or eligible, didn't
succeed). Plus: revenue-at-risk total, recovery rate, auto-recovery
attempts, policy refusals, stopped-by-safety-rules count, and incorrect
automatic actions (must be 0 — the safety proof). Always the full batch,
never cherry-picked.

## Out of scope for the buildathon

- B2B receivables chaser and Hinglish voice recovery (future work).
- Real production Razorpay credentials — test mode only, by design.
- A polished multi-tenant SaaS shell — this is a single-merchant demo.
