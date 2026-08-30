# Recovery Copilot

An AI-assisted revenue recovery engine for failed Razorpay payments. It is
**not an autonomous agent**: deterministic rules do the obvious diagnosis
work, an LLM call is reserved for genuinely ambiguous cases, and a
deterministic policy engine — never the model — makes the final call on
whether to act. The goal is not to maximize automated actions; it's to
maximize *safely* recovered revenue, and to visibly refuse to act when
confidence is too low on a high-value transaction.

Built for Track 03 (AI Revenue Recovery), Razorpay AI Buildathon (submission
deadline September 5, 2026).

## Architecture, in one paragraph

Rules handle known failure modes. An LLM handles genuinely ambiguous
diagnosis. The policy engine makes the final recovery decision. The action
layer executes only policy-approved actions. **The LLM is advisory, not
authoritative** — it can only ever propose a root cause, a raw confidence
number, and evidence; the policy engine converts that into a HIGH/MEDIUM/LOW
band and is the only component allowed to decide RETRY / PAYMENT_LINK /
HUMAN_REVIEW / STAND_DOWN. The LLM never sees policy inputs (DND, retry
counts, contact limits, cooldowns) and never calls Razorpay.

## The one hero flow

```
Failed payment → diagnose why → decide if recovery is worth attempting →
check policy → execute or refuse the action → observe result → stop or
escalate → record to audit trail → update the honest metrics.
```

No subscription recovery, no checkout abandonment, no dashboard, no
Docker/Prometheus/CI until this works end to end. See the build order below.

## Status

Day 3: full-batch evaluation. `python -m src.run_batch` runs the entire
68-record synthetic dataset through diagnose → policy → action → observe →
audit and reports honest, non-cherry-picked metrics. Rule-based diagnosis
and LLM-backed diagnosis (for failure reasons the rule table doesn't
recognize) are both wired for real. Not yet built: a payment-completion
confirmation path (webhook/polling — see Known limitations), the dashboard.

## Stack

- **API**: FastAPI + SQLAlchemy 2.0, Python 3.12
- **Database**: PostgreSQL, migrations via Alembic (production target);
  SQLite for the CLIs and tests (see [app/db/types.py](app/db/types.py))
- **Reasoning**: an LLM, for ambiguous-case diagnosis only — it proposes a
  root cause and a confidence band; it never decides the action. Currently
  wired to Groq's OpenAI-compatible chat completions API (the working
  credential available at build time — see
  [src/llm_diagnosis.py](src/llm_diagnosis.py)'s docstring). The contract
  (`DiagnosisSource.LLM`, structured root_cause/confidence/retryable/evidence)
  is provider-agnostic; swapping to Anthropic/Claude directly is a contained
  change to that one module.
- **Payments**: Razorpay test-mode REST API (Orders, Payment Links), called
  directly over HTTPS
- **Ops**: pytest only. Docker Compose exists from Day 1 but isn't required
  for anything built so far — every CLI runs against local SQLite.

## Data model

Three tables:

- **`Customer`** ([app/models/customer.py](app/models/customer.py)) — a merchant's end
  customer, plus compliance/history fields the policy engine reads:
  `dnd_opt_out`, `max_contact_attempts` / `contact_count`, and
  `prior_recovery_attempts` / `prior_recovery_successes` (history from before
  this batch — this system has no other memory of past runs; see the
  serial-failure policy factor below).
- **`FailedPayment`** ([app/models/failed_payment.py](app/models/failed_payment.py)) — the
  event: gateway ids, amount, `failure_code`/`failure_reason`, `raw_payload`
  (the full synthetic event shaped like the real `payment.failed` webhook),
  `retry_count`, and a `status` in `{OPEN, CONFIRMED_RECOVERED,
  SIMULATED_RECOVERED, ESCALATED, UNRESOLVED}`. `stop_reason` explains a
  non-OPEN outcome (e.g. `max_attempts_reached`, `dnd_opt_out`,
  `serial_recovery_failure_history`) without needing to re-derive it. A
  `cooldown_not_elapsed` stand-down deliberately leaves `status` at `OPEN` —
  it's a temporary pacing gate, not a terminal outcome.
- **`RecoveryAttempt`** ([app/models/recovery_attempt.py](app/models/recovery_attempt.py)) —
  one immutable row per diagnose→decide→act→observe cycle. This row *is* the
  audit trail — append-only, never mutated. It holds:
  - **diagnosis**: `diagnosis_root_cause`, `diagnosis_source` (`RULE` /
    `LLM` / `LLM_FALLBACK`), `model_reported_confidence` (raw, nullable —
    only ever set for `LLM`, never read downstream) and `confidence_band`
    (HIGH/MEDIUM/LOW — the only thing the policy engine is allowed to act on)
  - **decision**: `decision_action` (RETRY/PAYMENT_LINK/HUMAN_REVIEW/STAND_DOWN) and
    `decision_factors` (a JSON snapshot of every input used — payment value,
    failure type, confidence band, attempt count, cooldown, contact
    count/limit, compliance status, prior recovery attempts — captured at
    decision time, not reconstructed later)
  - **action**: `action_mode` (REAL/SIMULATED — required on every row, never
    defaulted, so a recovered outcome can never be ambiguous about whether
    it's real) and `action_result` (SUCCEEDED/FAILED/PENDING/NOT_EXECUTED)

## Diagnosis engine

[src/diagnosis.py](src/diagnosis.py): a fixed rule table (`issuer_timeout`,
`insufficient_funds`, `incorrect_otp`, `expired_card`, `invalid_cvv`,
`card_declined_do_not_honor`, `payment_blocked_risk`,
`authentication_abandoned`) maps a known `failure_reason` straight to a root
cause, a confidence, and a retryability flag. Anything the table doesn't
recognize is handed to [src/llm_diagnosis.py](src/llm_diagnosis.py), which sends
**only** the failure signal (code / reason / description / amount / currency
/ retry count) — never customer PII, never ground truth, never a scenario
label — and gets back a structured `{root_cause, confidence, retryable,
evidence}` via forced tool-calling. Confidence bands: **HIGH ≥ 0.85, MEDIUM
0.60–0.8499, LOW < 0.60** (`src/diagnosis.py`, configurable).

On any LLM failure — no API key, network/timeout error, an API error, or a
response that fails structural validation — `diagnose_ambiguous_case`
returns an explicit `LLM_FALLBACK` diagnosis at LOW confidence (a sentinel
`confidence=0.0`, never a plausible-looking guess) rather than letting the
failure crash the pipeline or masquerade as a successful diagnosis.

## Policy engine

[src/policy.py](src/policy.py) is the only place in the system that decides an
action. `PolicyInput` is deliberately the entire interface it can see — no
raw confidence float, no ORM objects, nothing beyond the bounded set of
factors below. Gates, in order:

1. **Risk block → HUMAN_REVIEW**, regardless of confidence (rule-table-only;
   the LLM can never set this).
2. **DND/opt-out → STAND_DOWN**, before anything else.
3. **Confidence/value table**: HIGH → auto RETRY or PAYMENT_LINK (by
   retryability); not-HIGH + high payment value → HUMAN_REVIEW
   (`high_value_uncertain_escalation`, regardless of MEDIUM vs LOW); MEDIUM
   (normal value) → HUMAN_REVIEW; LOW (normal value) → STAND_DOWN.
4. **Serial-failure history**: a candidate RETRY/PAYMENT_LINK is overridden
   to HUMAN_REVIEW if the customer has ≥ `serial_failure_attempt_threshold`
   (default **2**, configurable) prior recovery attempts — resolves the
   Day-2 gap where this expectation and the implementation disagreed. Note:
   `Customer.prior_recovery_attempts` is a pre-aggregated count assumed
   already scoped to the lookback window; there's no dated attempt log to
   filter by date in this build (documented, not hidden).
5. **Retry cap / cooldown** (RETRY only): `max_attempts` (default 3) and
   `cooldown_seconds` (default 30 min) since the last attempt.
6. **Contact limit** (PAYMENT_LINK only): `max_contact_attempts`.

## Action layer

[src/razorpay_action.py](src/razorpay_action.py) (`RazorpayActionClient`, the
REAL executor) calls the Orders/Payment Links APIs directly over HTTPS.
**It never reports `SUCCEEDED`.** Creating an order or a payment link is
something the API response genuinely confirms; a *completed* payment is not
— that requires the customer to finish checkout, and this project will
never submit card data server-side to fake that (out of PCI scope, and a
prohibited action in its own right). A successfully placed action is
`REAL` + `PENDING`, always.

[src/simulated_action.py](src/simulated_action.py) (`SimulatedActionExecutor`,
the default batch executor) makes no network call at all and models the
approved action as succeeding — `SIMULATED` + `SUCCEEDED`, an explicit,
labeled, *modeled* number for evaluation, never confused with a confirmed
recovery.

HUMAN_REVIEW and STAND_DOWN decisions never reach either executor — see
`_execute_action` in [src/pipeline.py](src/pipeline.py).

## Synthetic dataset

[seed/case_catalog.py](seed/case_catalog.py) defines 17 case templates (7 easy,
10 deliberately hard), expanded into **68 records** by
[seed/generate_dataset.py](seed/generate_dataset.py):

```bash
python -m seed.generate_dataset
```

Writes two files:

- **[data/synthetic_failed_payments.json](data/synthetic_failed_payments.json)** — what
  the engine is allowed to see. No expected outcomes, no case labels.
- **[data/ground_truth.json](data/ground_truth.json)** — expected root cause,
  confidence band, decision action, and final status, keyed by
  `external_payment_id`. Used to score the batch; never fed to the engine.

Hard cases cover: two flavors of genuinely conflicting/unfamiliar signal, a
payment mid-cap (2 of 3 retries used) vs. one already at the cap, a payment
inside its cooldown window, an opted-out (DND) customer, a customer whose
contact limit is used up, a high-value payment with an uncertain diagnosis,
a customer with a serial-failure history, and a risk-engine block (never
auto-retried, regardless of confidence).

**One template moved categories mid-build**: `authentication_abandoned` (a
customer dropping 3-D Secure authentication) was originally modeled as
ambiguous. Real LLM verification (see Known limitations) consistently and
confidently diagnosed it as retryable — correctly: this is a well-understood,
commonly-retryable e-commerce pattern (distraction, OTP timeout), not
genuine ambiguity. It's now in the rule table and the easy set.

### The three demo cases

One instance of three specific templates is flagged `canonical_demo_case`
in the ground-truth file:

- **Case A** (`issuer_timeout_retry_success`) — transient failure → policy
  approves a retry.
- **Case B** (`expired_card_payment_link_success`) — non-retryable failure →
  system skips straight to a payment link instead of a pointless retry.
- **Case C** (`high_value_uncertain_escalation`) — ambiguous, high-value
  failure → confidence isn't HIGH → policy refuses automatic action and
  **escalates to human review** rather than guessing, regardless of the
  exact MEDIUM/LOW band.

## Razorpay test-mode connectivity check

```bash
python -m scripts.check_razorpay_keys
```

Calls the Orders and Payment Links APIs directly over HTTPS (Basic Auth) —
not the `razorpay` PyPI package, which pulls in a legacy `pkg_resources`
import current `setuptools` no longer ships. Refuses to run against
anything that isn't a `rzp_test_...` key.

## Running the vertical slice

One payment, full pipeline, printed stage by stage:

```bash
python -m src.run_vertical_slice            # canonical Case A (transient failure -> retry)
python -m src.run_vertical_slice --case b     # canonical Case B (non-retryable -> payment link)
python -m src.run_vertical_slice --case c      # canonical Case C (high-value + uncertain -> human review)
python -m src.run_vertical_slice --payment-id pay_xxx   # any specific dataset record
```

Prints `INPUT` / `DIAGNOSIS` / `POLICY` / `ACTION` / `RESULT` / `AUDIT` and
persists to a local SQLite file (`data/recovery_copilot.db`, independent of
`DATABASE_URL`, so this runs without Docker). Re-running against the same
payment id accumulates `RecoveryAttempt` rows and will eventually hit the
retry-cap / cooldown gates for real.

## Running the batch

```bash
python -m src.run_batch                    # full 68-record batch, simulated action/result layer
python -m src.run_batch --execute-real       # real Razorpay test-mode calls for approved auto-actions
python -m src.run_batch --only-ambiguous      # only records the rule table can't classify (LLM path)
python -m src.run_batch --limit 10             # cap how many records get processed
```

Diagnosis always runs for real (rules, then the LLM for anything the rules
don't recognize) regardless of `--execute-real` — that flag controls **only**
the action/result layer. Default mode makes zero network calls for actions
(fast, free, fully reproducible: resets its own SQLite DB, `data/batch_run.db`,
every run). `--execute-real` executes RETRY/PAYMENT_LINK against Razorpay
test mode for real; HUMAN_REVIEW/STAND_DOWN never call Razorpay in either
mode (a structural guarantee, not a runtime check — see `_execute_action`).

Prints the required metrics table plus an evaluation section, and writes
`data/batch_report.json` (per-record detail + full metrics) as the evidence
artifact for the run.

### Metric definitions

- **Confirmed recovered** — ₹ amount where `action_mode=REAL` **and**
  `action_result=SUCCEEDED`. Structurally always ₹0 today: the REAL
  executor never returns `SUCCEEDED` (see Action layer above). An
  API-created order or payment link is never counted as recovered revenue.
- **Simulated recovery** — a separate, always-labeled bucket: `SIMULATED` +
  `SUCCEEDED` (modeled, no live action). Reported, never blended into
  Confirmed recovered.
- **Auto-recovery attempts** — RETRY or PAYMENT_LINK actually attempted.
- **Successful recoveries** — Confirmed + Simulated recovered, combined
  count (for the required table row only).
- **Policy refusals (escalated)** — HUMAN_REVIEW decisions, plus
  low-confidence STAND_DOWN (`confidence_below_action_threshold`): a
  human-judgment-shaped refusal to auto-act.
- **Stopped by safety rules** — `max_attempts_reached`, `cooldown_not_elapsed`,
  `dnd_opt_out`, `contact_limit_reached`: mechanical stopping rules and
  compliance gates, grouped together (matching the original spec's own
  "Stopping rules ... Compliance" pairing). These three buckets are
  mutually exclusive and sum to the total record count — see
  [src/batch_metrics.py](src/batch_metrics.py)'s module docstring for the exact
  classification.
- **Incorrect automatic actions** — every RETRY/PAYMENT_LINK decision whose
  action doesn't match `ground_truth.expected_action` for that record.
  Target: **0**. If it's not, the report prints a loud banner listing every
  offending record rather than averaging it away.
- **Rule-based diagnosis accuracy** — 100% by construction (deterministic
  table lookup); reported, not claimed as evidence of anything. **LLM
  diagnosis accuracy is explicitly NOT evaluated** against
  `ground_truth.expected_root_cause` — that field is freeform illustrative
  text authored before any real LLM call, not a verified gold label, and
  exact-string comparison would be a misleading metric. Only the resulting
  *policy action* is scored (that's what incorrect-automatic-actions
  depends on).

## Running the API

```bash
cp .env.example .env   # fill in RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET / GROQ_API_KEY
docker compose up --build -d db
docker compose run --rm api alembic upgrade head
docker compose up --build -d api
curl http://localhost:8000/health
```

## Tests

```bash
pytest -q
```

Runs against an in-memory SQLite database, so no Postgres container and no
external API is required — Groq and Razorpay are both mocked at the
transport layer (`httpx.MockTransport`) in every automated test; only the
manual verification runs below hit real APIs. 88 tests covering: the
models, the case catalog, the diagnosis engine (rule table + LLM dispatch),
the policy engine (every gate individually and in combination), the LLM
diagnosis module (successful/low-confidence/malformed/HTTP-error/
connection-error paths, tool-choice forcing, no-PII-no-ground-truth checks
at both the helper and transport level), the Razorpay action executor
(never returns `SUCCEEDED` under any mocked response shape), the
vertical-slice pipeline end-to-end (successful retry, policy refusal, DND
refusal, retry-cap refusal, audit record creation, no fake recovered
result), the batch metrics computation, the batch runner (full-dataset
processing, four-way categorization, `--execute-real` never touching
Razorpay for HUMAN_REVIEW/STAND_DOWN, `--only-ambiguous`/`--limit`
filtering, ground truth never reaching the LLM), and the health endpoint.

## Build order

| Step | Focus |
| --- | --- |
| 1 (done) | Synthetic dataset + data model; confirmed Razorpay test-mode keys work |
| 2 (done) | Vertical slice: one failed payment through diagnose → policy → real Razorpay test-mode action → result → append-only audit record, runnable from the CLI |
| 3 (done) | Full 68-record batch runner; real LLM diagnosis for the rule table's blind spots; serial-failure policy factor; honest batch metrics |
| 4 | A real payment-completion confirmation path (webhook or polling) so `CONFIRMED_RECOVERED` becomes reachable; further hardening of the refusal path |
| 5 | Minimal UI: batch results by outcome category, audit trail viewer |
| 6 (time permitting) | Docker, structured logging/Prometheus, CI, additional flows |

## Known limitations

- **No payment-completion confirmation path yet.** Even `--execute-real`
  only proves an order/link was *created* — actual payment completion needs
  a webhook receiver or status-polling loop (build-order step 4). Until
  then, Confirmed Recovered is honestly ₹0 for the whole batch, by design.
- **Groq's forced tool-calling is not 100% reliable.** In verification runs,
  the ambiguous-case LLM calls succeeded roughly 40–60% of the time; the
  rest hit Groq's `tool_use_failed` (the model didn't call the tool that
  turn) and correctly fell back to `LLM_FALLBACK` at LOW confidence. This
  never compromises safety — a fallback is always the most conservative
  outcome — but it does mean a given batch run's rule/LLM/fallback split
  will vary run to run. No retry logic was added for this (kept in scope);
  see the batch report's `llm_fallback_rate`.
- **Currently wired to Groq, not Anthropic/Claude**, because that was the
  working credential available at build time — see the Stack section above.
  The diagnosis contract is provider-agnostic; swapping is a contained
  change to `src/llm_diagnosis.py` for anyone with an Anthropic key.
- **Serial-failure lookback isn't date-filtered.** `PolicyConfig.serial_failure_lookback_days`
  documents the intended window, but `Customer.prior_recovery_attempts` is a
  pre-aggregated count with no dated attempt log to filter by date in this
  build — see the field's docstring in `src/policy.py`.
- **LLM diagnosis accuracy isn't scored.** See "Rule-based diagnosis
  accuracy" above — ground truth's root-cause labels for ambiguous cases are
  illustrative, not gold-standard.

## Out of scope for the buildathon

- B2B receivables chaser and Hinglish voice recovery (future work).
- Real production Razorpay credentials — test mode only, by design.
- A polished multi-tenant SaaS shell — this is a single-merchant demo.
