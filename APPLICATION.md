# Application form answers — Recovery Copilot

Pulled from the README rather than written fresh, so the form can't drift
from what the repo actually shows. Repo URL is filled in below; update the
video link once the pitch is recorded.

## Project name

Recovery Copilot

## Track

Track 03 — AI Revenue Recovery

## What it solves

An AI-assisted revenue recovery engine for failed Razorpay payments. It is
**not an autonomous agent**: deterministic rules do the obvious diagnosis
work, an LLM call is reserved for genuinely ambiguous cases, and a
deterministic policy engine — never the model — makes the final call on
whether to act. The goal is not to maximize automated actions; it's to
maximize *safely* recovered revenue, and to visibly refuse to act when
confidence is too low on a high-value transaction.

Validated end to end on a 64-record synthetic batch: 32 auto-recovery
attempts, 20 policy refusals, 12 stopped by safety rules, and **0 incorrect
automatic actions** across the full batch, not a cherry-picked subset.

## GitHub repo URL

https://github.com/akaza138/sentinel

Pushed and verified (`git ls-remote` confirms `origin/master` matches local
HEAD). *Visibility not confirmed from here — double-check the repo is set
to Public in GitHub's settings before submitting, since that wasn't
verifiable without a browser/API session to GitHub itself.*

## 5-minute pitch video

*Not yet recorded — script is ready at [PITCH.md](PITCH.md), timed to ~5
minutes with real command output. `<fill in video link once recorded>`.*

## What broke, and how you got out

Wiring the LLM diagnosis step to Groq's API surfaced a real reliability
issue: forced tool-calling (the mechanism used to get structured
`{root_cause, confidence, retryable, evidence}` output back from the model)
failed roughly 40–60% of the time across verification runs, with Groq
returning a `tool_use_failed` error — the model simply didn't call the
required tool that turn. It was found by actually running the ambiguous
cases against the real API repeatedly, not by reading docs; a single-call
smoke test looked fine and the failure rate only became visible at batch
scale. Rather than retrying or masking it, the existing fallback path
(`LLM_FALLBACK`, forcing LOW confidence) handled every one of these
failures automatically and correctly — the policy engine's downstream
behavior never depends on the LLM succeeding, so a bad tool-call never
produced a bad decision, only a more conservative one. No production code
changed as a result; the batch report's `llm_fallback_rate` now surfaces
the real number instead of hiding it, and it's called out explicitly in the
README's Known Limitations.

*(A second, independently investigated issue — whether a real Razorpay
test-mode payment could be completed end-to-end, not just created — is
written up in full in the README's "Why Confirmed Recovered is ₹0"
section, including a headless-browser attempt that hit a content block.
Both are true "what broke" stories; this one was chosen for the form
because it's the shorter, cleaner account — the Razorpay investigation
needed the fuller README treatment to do it justice.)*

## Note on build timeline

The commit history is a genuine day-by-day account of what was built and
in what order (see the README's Status section), but all six commits carry
the same calendar date — this was built across one continuous, intensive
session rather than a literal week, even though the work is structured and
labeled "Day 1" through "Day 6." Said plainly here rather than left for a
reviewer to notice from `git log` and wonder about.
