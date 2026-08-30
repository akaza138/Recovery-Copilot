"""LLM-backed diagnosis for failure reasons the deterministic rule table
doesn't recognize (src/diagnosis.py dispatches here when RULE_TABLE has no
entry for a payment's failure_reason).

Currently wired to Groq's OpenAI-compatible chat completions API — the
working LLM credential available at build time (GROQ_API_KEY). The
diagnosis contract (root_cause / confidence / retryable / evidence,
recorded as DiagnosisSource.LLM) is provider-agnostic and doesn't name Groq
anywhere outside this module; swapping to Anthropic/Claude directly is a
contained change to _call_llm() below, not a system-wide one.

Scope boundary, load-bearing: this module ONLY diagnoses. It returns a root
cause, a raw confidence number, and evidence — nothing else. It cannot
choose an action, cannot see or influence policy inputs (DND, contact
limits, retry counts, cooldowns), and never calls Razorpay. The policy
engine (src/policy.py) remains the sole decision authority regardless of
what this module reports. The LLM is advisory, not authoritative.

Sends ONLY the failure signal (code / reason / description / amount /
currency / retry count on this payment) — never customer PII (name, email,
phone), never ground truth, never a scenario/case label, never any other
answer derived from ground truth.

`model_reported_confidence` is a raw, uncalibrated number from the model.
It is never treated as a calibrated probability; src/diagnosis.py's
confidence_band() converts it into HIGH/MEDIUM/LOW before anything
downstream (i.e. the policy engine) may use it.

On any failure — missing API key, network/timeout error, an API error, or a
response that fails structural validation — this module returns an explicit
LLM_FALLBACK diagnosis at LOW confidence. It never lets an LLM failure look
like a successful diagnosis. HTTP errors carry the response body (not just
the status line) into the fallback evidence, since Groq's body says exactly
what went wrong (bad model id, malformed tool schema, etc.) — the status
line alone ("Client error '400 Bad Request'") is not actionable.

Model id is configurable via the GROQ_MODEL env var (default
"openai/gpt-oss-20b", confirmed to support forced tool-calling — see Known
Limitations in the README for its ~40-60% observed reliability on this
call shape, handled entirely by the fallback path above).
"""

import json
import os
from typing import Any

import httpx

from app.models.recovery_attempt import ConfidenceBand, DiagnosisSource
from src.diagnosis import Diagnosis, confidence_band

GROQ_API_BASE = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
REQUEST_TIMEOUT_SECONDS = 20.0

FALLBACK_ROOT_CAUSE = "llm_diagnosis_unavailable"
FALLBACK_CONFIDENCE = 0.0  # explicit zero, not a real estimate — forced to LOW band regardless of threshold

_DIAGNOSIS_TOOL = {
    "type": "function",
    "function": {
        "name": "report_diagnosis",
        "description": "Report a structured diagnosis for one failed payment.",
        "parameters": {
            "type": "object",
            "properties": {
                "root_cause": {
                    "type": "string",
                    "description": (
                        "A short, snake_case label for the most likely root cause "
                        "(e.g. 'issuer_soft_decline', 'customer_dropped_authentication')."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "description": (
                        "Your genuine confidence in this diagnosis, from 0.0 (pure guess) to 1.0 (certain). "
                        "A genuinely ambiguous case should score low — do not round up to sound more useful."
                    ),
                },
                "retryable": {
                    "type": "boolean",
                    "description": (
                        "Whether this class of failure typically resolves if the exact same payment method is "
                        "retried as-is, as opposed to requiring the customer to take a different action (a new "
                        "card, a new payment method, fixing something on their end)."
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": "One or two sentences on what in the failure signal supports this diagnosis.",
                },
            },
            "required": ["root_cause", "confidence", "retryable", "evidence"],
        },
    },
}

SYSTEM_PROMPT = (
    "You are a payment-failure diagnosis assistant for a Razorpay revenue-recovery system. "
    "You are given the raw failure signal for ONE failed payment (gateway error code, error reason, "
    "error description, amount, and how many times it has already been retried) that a deterministic "
    "rule table could not classify. Diagnose the most likely root cause and report your genuine "
    "confidence. This is a DIAGNOSIS ONLY. You do not decide what happens next: a separate, "
    "deterministic policy engine makes every action decision (retry, send a payment link, route to a "
    "human, or take no action) and enforces all compliance and safety rules (opt-outs, retry caps, "
    "cooldowns, contact limits). Nothing you say is treated as an instruction to act, and nothing you "
    "say can bypass any of those rules. Report your answer using the report_diagnosis tool only."
)


class LLMDiagnosisError(Exception):
    """Raised internally when the LLM call or its response can't be trusted; always caught by diagnose_ambiguous_case."""


def _build_user_message(failure_signal: dict) -> str:
    return (
        "Diagnose this failed payment:\n"
        f"- failure_code: {failure_signal['failure_code']}\n"
        f"- failure_reason: {failure_signal['failure_reason']}\n"
        f"- failure_description: {failure_signal['failure_description']}\n"
        f"- amount: {failure_signal['amount']} {failure_signal['currency']}\n"
        f"- prior_retry_attempts_on_this_payment: {failure_signal['retry_count']}\n"
    )


def _call_llm(failure_signal: dict, *, client: Any, model: str) -> dict:
    """`client` is an httpx.Client (real or test-injected via a MockTransport)."""
    response = client.post(
        "/chat/completions",
        json={
            "model": model,
            "temperature": 0,
            "max_tokens": 512,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_message(failure_signal)},
            ],
            "tools": [_DIAGNOSIS_TOOL],
            "tool_choice": {"type": "function", "function": {"name": "report_diagnosis"}},
        },
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # httpx's own message is just the status line ("Client error '400 Bad Request' for url
        # '...'") — no diagnostic content. Groq's response body says exactly what was wrong
        # (bad model id, malformed tool schema, etc.), so it has to be surfaced here or every
        # HTTP error looks identical and unfixable from the fallback evidence text alone.
        raise LLMDiagnosisError(f"{exc}. Response body: {response.text}") from exc
    body = response.json()

    tool_calls = body["choices"][0]["message"].get("tool_calls") or []
    for call in tool_calls:
        if call.get("function", {}).get("name") == "report_diagnosis":
            arguments = call["function"]["arguments"]
            return json.loads(arguments) if isinstance(arguments, str) else arguments

    raise LLMDiagnosisError("LLM response did not include a report_diagnosis tool call.")


def _validate(raw: Any) -> tuple[str, float, bool, str]:
    if not isinstance(raw, dict):
        raise LLMDiagnosisError(f"Expected a dict from the tool call, got {type(raw).__name__}")

    root_cause = raw.get("root_cause")
    confidence = raw.get("confidence")
    retryable = raw.get("retryable")
    evidence = raw.get("evidence")

    if not isinstance(root_cause, str) or not root_cause.strip():
        raise LLMDiagnosisError(f"Missing or empty root_cause: {root_cause!r}")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not (0.0 <= float(confidence) <= 1.0):
        raise LLMDiagnosisError(f"confidence must be a number in [0, 1], got {confidence!r}")
    if not isinstance(retryable, bool):
        raise LLMDiagnosisError(f"retryable must be a boolean, got {retryable!r}")
    if not isinstance(evidence, str) or not evidence.strip():
        raise LLMDiagnosisError(f"Missing or empty evidence: {evidence!r}")

    return root_cause.strip(), float(confidence), retryable, evidence.strip()


def _fallback(reason: str) -> Diagnosis:
    return Diagnosis(
        root_cause=FALLBACK_ROOT_CAUSE,
        confidence=FALLBACK_CONFIDENCE,
        confidence_band=ConfidenceBand.LOW,
        evidence=f"LLM diagnosis unavailable: {reason}. Falling back safely rather than guessing.",
        source=DiagnosisSource.LLM_FALLBACK,
        retryable=False,
        never_auto=False,
    )


def diagnose_ambiguous_case(failure_signal: dict, *, client: Any | None = None, model: str = DEFAULT_MODEL) -> Diagnosis:
    """Attempts an LLM-backed diagnosis for a failure reason the rule table
    doesn't recognize. Always returns a Diagnosis — on any failure it
    returns an explicit LLM_FALLBACK diagnosis at LOW confidence rather than
    silently treating the failure as a successful diagnosis.

    `client` is an injection seam for tests (an httpx.Client, typically
    built with a MockTransport); production code leaves it None and a real
    httpx.Client is built from GROQ_API_KEY.
    """
    api_key = os.environ.get("GROQ_API_KEY", "")
    if client is None and not api_key:
        return _fallback("GROQ_API_KEY not configured")

    owns_client = client is None
    active_client = client or httpx.Client(
        base_url=GROQ_API_BASE, headers={"Authorization": f"Bearer {api_key}"}, timeout=REQUEST_TIMEOUT_SECONDS
    )

    try:
        raw = _call_llm(failure_signal, client=active_client, model=model)
        root_cause, confidence, retryable, evidence = _validate(raw)
    except Exception as exc:  # noqa: BLE001 — this is the one place an external API's failure must
        # never be allowed to propagate as a crash or masquerade as a valid diagnosis; see module docstring.
        return _fallback(f"{type(exc).__name__}: {exc}")
    finally:
        if owns_client:
            active_client.close()

    return Diagnosis(
        root_cause=root_cause,
        confidence=confidence,
        confidence_band=confidence_band(confidence),
        evidence=f"LLM diagnosis: {evidence}",
        source=DiagnosisSource.LLM,
        retryable=retryable,
        never_auto=False,  # risk-block-level safety gates stay rule-table-only; see RULE_TABLE in diagnosis.py
    )
