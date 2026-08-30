# Recovery Copilot — 5-minute pitch script

Written to be read aloud in ~5 minutes at a deliberate, unhurried pace
(~150 words/minute). Word/time budget per section is noted so you can
rehearse against it — trim or pad to your own delivery speed, not this
estimate.

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

## 0:25–1:10 — Case A: successful recovery (~45s)

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

## 1:10–1:55 — Case B: intelligent intervention (~45s)

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

## 1:55–3:25 — Case C: the refusal (~90s — this is the one to slow down for)

> Now the case that actually matters most.

**[RUN]** `python -m src.run_vertical_slice --case c`

> This payment failed with a generic decline code — no clear reason. Could
> be a network glitch, could be a dead card. Our rule table doesn't
> recognize it, so it goes to an LLM call — genuinely, live, right now, not
> a canned response.

**[POINT: `DIAGNOSIS` — confidence and band]**

> The model comes back below our 85% auto-action threshold — sometimes
> lower, sometimes higher, because it's a real call every single time, not
> a script. This one doesn't clear the bar.

> Normally that alone routes it to human review. But look at the amount —
> this is a ₹1.2 lakh payment. On top of an uncertain diagnosis, high value
> gets an extra check, and the policy engine refuses to auto-act and
> escalates to a human instead.

**[POINT: `POLICY`/`RESULT` — `HUMAN_REVIEW`, `high_value_uncertain_escalation`]**

> I want to say this plainly, because it's the whole thesis of the project:
> **our objective isn't to maximize automated actions. It's to maximize
> *safely* recovered revenue.** A system that guesses on a six-figure
> payment isn't smart — it's reckless. This one knows when to stop.

---

## 3:25–4:10 — The full batch, no cherry-picking (~45s)

> Those were three cases I picked to show you. Here's all 64.

**[RUN]** `python -m src.run_batch`

**[POINT: the metrics table]**

> 64 revenue-at-risk events. 32 auto-recovery attempts, 20 escalated to
> policy refusal, 12 stopped by safety rules — retry caps, cooldowns,
> opt-outs.

**[POINT: the "Incorrect automatic actions" row]**

> And the number that actually proves this is safe: **incorrect automatic
> actions — zero.** Across the entire batch, every single time the system
> acted on its own, it acted correctly. Not on the three cases I chose for
> you — on all 64.

---

## 4:10–4:45 — Close: the honest limitation (~35s)

> One limitation, stated up front, not buried in an appendix: this
> validates the recovery *decision* pipeline in test mode. Production
> recovery rates — how many of these retries and links actually convert to
> a paid transaction — would need evaluation on real merchant data, with
> real customer behavior. What we've proven here is that the
> decision-making underneath it is sound, auditable, and safe. That's the
> foundation everything else gets built on.

## 4:45–5:00 — Sign-off (~10s)

> Recovery Copilot. Thanks.

---

## Figures referenced (from a real run — yours will differ, that's fine)

| Case | Root cause | Confidence | Decision | Real Razorpay reference |
|---|---|---|---|---|
| A | issuer_timeout | 97% (rule) | RETRY | `order_TVzvvVCWFvWvFa` |
| B | expired_card | 98% (rule) | PAYMENT_LINK | `plink_TVzwATwW9OUIG4` (`rzp.io/rzp/v7YkSQ6`) |
| C | issuer_soft_decline | 60% (LLM, varies per call) | HUMAN_REVIEW | none (never reaches Razorpay) |

Batch (full 64 records, simulated action layer): 32 auto-recovery attempts,
20 policy refusals, 12 stopped by safety rules, **0 incorrect automatic
actions**.
