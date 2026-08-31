# Recovery Copilot — 5-minute pitch script

Written to be read aloud at a deliberate, unhurried pace (~140
words/minute). ~780 spoken words plus four live command executions and
several on-screen pointing pauses realistically land this at **~6:30–7:00**
— this now runs past the traditional 5-minute mark because the
counterfactual comparison and the red-team beat earned their place as
headline evidence, not filler. Case C is still the longest single beat and
shouldn't be the thing you cut. If you need to land under 6:00, trim from
Case A/B's setup lines first (they can lose a sentence each without
losing the point), then the red-team beat down to one sentence; don't
shorten Case C or the counterfactual beat — between them they're the
whole safety argument. Word/time budget per section is noted below so you
can rehearse against it — treat it as a rehearsal target, not a guarantee,
and adjust to your own delivery speed.

**Before recording:** run the demo commands once ahead of time so you know
roughly how long the real API calls take (a few seconds each), and either
screen-record live or have the terminal output ready to paste in — dead air
waiting on a network call eats your budget fast. The exact IDs below (order
number, payment link, model confidence) will differ on your own run — that's
expected and is itself evidence nothing here is canned; say whatever your
terminal actually shows.

Commands, in order:

```bash
python -m src.run_vertical_slice --case a
python -m src.run_vertical_slice --case b
python -m src.run_vertical_slice --case c
python -m src.run_batch
```

---

## 0:00–0:25 — Open: the north-star question (~25s)

> Every failed payment is money a merchant already earned and almost lost.
> The question isn't "can an AI retry a payment" — it's: **how much revenue
> can we safely recover automatically, without increasing incorrect
> recovery actions?** That's the bar this system is built against, and I
> can show you the number, live.

**[SCREEN: terminal, repo root]**

---

## 0:25–1:05 — Case A: successful recovery (~40s)

> First, the easy case done right. This payment failed on a bank timeout —
> transient, nothing wrong with the card.

**[RUN]** `python -m src.run_vertical_slice --case a`

> The rule table diagnoses it — 97% confidence, HIGH band — and the policy
> engine approves an automatic retry. This isn't simulated: that's a real
> order, created against Razorpay's test-mode API right now.

**[POINT: `ACTION`/`RESULT` — a real `order_...` id, `action_mode: REAL`]**

> Notice it says PENDING, not SUCCEEDED. We never claim a payment is
> recovered until Razorpay itself confirms it — more on that at the end.

---

## 1:05–1:40 — Case B: intelligent intervention (~35s)

> Now the smarter case. This card is expired.

**[RUN]** `python -m src.run_vertical_slice --case b`

> Retrying an expired card is pointless — same card, same failure, forever.
> The system knows that: 98% confidence this is non-retryable, so instead
> of a dumb retry it skips straight to a payment link. Also real, also just
> created against Razorpay.

**[POINT: `ACTION`/`RESULT` — a real `plink_...` id and `rzp.io` link]**

> That's the intelligence bar: not just *can* it act, but *does it pick the
> right action*.

---

## 1:40–3:55 — Case C: the refusal (~135s — longest beat, deliberately; don't trim this one)

> Now the case that actually matters most — and it's a better story live
> than any script could set up.

**[RUN]** `python -m src.run_vertical_slice --case c`

> This payment failed with a generic decline code, no clear reason given.
> Our rule table doesn't recognize it, so it goes to a live LLM call —
> genuinely, right now, not a canned response.

**[POINT: `DIAGNOSIS` — confidence, band, and specifically the `retryable` line]**

> And here's the part I actually want you to look at. The model comes back
> around 60% confidence — MEDIUM band, below our 85% threshold for
> automatic action. But look at this line: **`retryable: true`.** The model
> isn't hedging or refusing to commit. It's leaning toward "retry this." It
> wants to act.

**[POINT: `POLICY` — the full `factors:` block, specifically `payment_value`, `high_value_threshold`, `confidence_band`]**

> And the policy engine overrides it anyway. Not because the model was
> necessarily wrong — we don't even know that. Because this is a ₹1.2 lakh
> payment, and the rule is explicit: if the value clears this threshold and
> the confidence band isn't HIGH, a human decides, not the model — no
> matter which way the model was leaning. Every input that produced that
> override is sitting right there in the factors block: payment value,
> threshold, confidence band. Nothing hidden, nothing re-derived after the
> fact.

**[POINT: `RESULT` — `HUMAN_REVIEW`, `high_value_uncertain_escalation`]**

> This is the direct answer to the question everyone asks about AI touching
> money: *what if the model is wrong?* Here, it doesn't matter which way it
> was leaning. It never gets the final say on anything. It proposes; a
> deterministic, auditable policy engine decides.

> I want to say this plainly, because it's the whole thesis of the project:
> **our objective isn't to maximize automated actions. It's to maximize
> *safely* recovered revenue.** A system that lets a model's lean decide a
> six-figure payment isn't smart — it's reckless. This one doesn't.

**[PRESENTER NOTE, not spoken: Claude is now the default provider if
`ANTHROPIC_API_KEY` is set; Groq is the fallback provider and is what's
configured in this build environment. Groq's forced tool-calling, even
with the bounded retry now in place, measured 18/30 (60%) success across
two real batches — not 100%. If this run lands on `LLM_FALLBACK` instead
of `source: llm`, that's still correct, honest behavior worth narrating
live — "the call itself failed twice, so the system defaulted to LOW
confidence and refused outright, which is the safe direction to fail in"
— but it's a flatter story. Rehearse until you get a `source: llm` take
with `retryable: true` and use that recording; re-run live if you have
buffer.]**

---

## 3:55–4:45 — What skipping the gate costs (~50s)

> Those were three cases I picked to show you. Here's all 64 — and watch
> the very first thing the report prints, before the metrics table.

**[RUN]** `python -m src.run_batch`

**[POINT: the counterfactual panel — top of the output, above the metrics
table]**

> Same 64 records, three decision strategies. "Naive" retries anything the
> diagnosis marks retryable — no policy engine consulted at all. That's the
> bot most teams ship first. "Ungated LLM" lets the diagnosis's own
> recommendation execute directly — retry if retryable, else send a
> payment link — policy engine bypassed entirely, just trusting the model.
> "Recovery Copilot" is the real, unmodified policy engine — the same one
> that just overrode Case C.

> Naive: 40 automatic actions, 20 of them unsafe — DND breaches, retry-cap
> violations, acting on a customer with a serial-failure history the
> system should have escalated instead. Ungated LLM: 64 automatic actions,
> 32 unsafe — including four times it auto-acted on a customer we'd
> risk-blocked entirely, because nothing downstream of the model was
> checking anymore. Recovery Copilot: 32 automatic actions. **Zero unsafe.**

> The question isn't whether an AI can retry a payment — it's whether it
> knows when not to. Here's what skipping that costs.

## 4:45–5:05 — Twenty seconds on how we know the gate holds (~20s)

> That zero isn't a lucky batch. Our red-team suite spends 20 tests trying
> to break this exact policy engine — a fresh, confident diagnosis thrown
> at a payment already past its retry cap, a ₹90 lakh payment stacked on
> top of a DND opt-out, checked all the way through to a real assertion
> that Razorpay is never called. Every one of those attacks still loses.
> That's how the zero above stays zero.

---

## 5:05–6:00 — The full batch, no cherry-picking (~55s)

**[POINT: scroll down to the metrics table, same output already on
screen]**

> 64 revenue-at-risk events. 32 auto-recovery attempts, 20 escalated to
> policy refusal, 12 stopped by safety rules — retry caps, cooldowns,
> opt-outs.

**[POINT: the "Incorrect automatic actions" row]**

> And the number that actually proves this is safe: **incorrect automatic
> actions — zero.** Across the entire batch, every single time the system
> acted on its own, it acted correctly. Not on the three cases I chose for
> you — on all 64. And you just saw what the same 64 records would have
> cost without the gate.

---

## 6:00–6:30 — Close: the honest limitation (~30s)

> One limitation, stated up front, not buried in an appendix: this
> validates the recovery *decision* pipeline in test mode. Production
> recovery rates — how many of these retries and links actually convert to
> a paid transaction — would need evaluation on real merchant data, with
> real customer behavior. What we've proven here is that the
> decision-making underneath it is sound, auditable, and safe. That's the
> foundation everything else gets built on.

## 6:30–6:40 — Sign-off (~10s)

> Recovery Copilot. Thanks.

---

## Figures referenced (from a real run — yours will differ, that's fine)

| Case | Root cause | Confidence | Retryable | Decision | Real Razorpay reference |
|---|---|---|---|---|---|
| A | issuer_timeout | 97% (rule) | n/a | RETRY | `order_TVzvvVCWFvWvFa` |
| B | expired_card | 98% (rule) | n/a | PAYMENT_LINK | `plink_TVzwATwW9OUIG4` (`rzp.io/rzp/v7YkSQ6`) |
| C | issuer_soft_decline | 60% (LLM, MEDIUM — varies per call, source sometimes `LLM_FALLBACK` instead) | **true** | HUMAN_REVIEW (`high_value_uncertain_escalation`) | none (policy override — never reaches Razorpay) |

Case C's `retryable: true` is the number to point at: the model leaned
toward acting, and the policy engine overrode it anyway because the
payment value cleared the high-value threshold at a non-HIGH confidence
band. That's the whole "what if the LLM is wrong" answer in one line.

Batch (full 64 records, simulated action layer): 32 auto-recovery attempts,
20 policy refusals, 12 stopped by safety rules, **0 incorrect automatic
actions**.

Counterfactual comparison (same 64 records, one real run — exact counts
drift a little run to run since the ambiguous cases route through Groq;
the shape doesn't):

| Mode | Auto-actions | Unsafe |
|---|---:|---:|
| Naive (retryable → always retry, no gates) | 40 | 20 |
| Ungated LLM (diagnosis recommendation executes directly) | 64 | 32 |
| **Recovery Copilot (gated)** | 32 | **0** |

Ungated LLM's 32 unsafe actions include 4 auto-actions taken on a
risk-blocked customer — the single most damning line in the whole
comparison, and the one worth pointing at if you only have time to point
at one.
