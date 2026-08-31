"""LLM-backed diagnosis for failure reasons the deterministic rule table
doesn't recognize (src/diagnosis.py dispatches here when RULE_TABLE has no
entry for a payment's failure_reason).

Two providers, selected automatically (select_provider(), below):
Anthropic/Claude if ANTHROPIC_API_KEY is configured (preferred default —
Claude's forced tool-calling has proven more reliable in practice), Groq's
OpenAI-compatible API otherwise (used throughout early development; kept
working as the fallback provider). Both speak the exact same diagnosis
contract (root_cause / confidence / retryable / evidence, recorded as
DiagnosisSource.LLM) — nothing about that contract, the confidence banding,
or the LLM_FALLBACK behavior below differs by provider.

Scope boundary, load-bearing: this module ONLY diagnoses. It returns a root
cause, a raw confidence number, and evidence — nothing else. It cannot
choose an action, cannot see or influence policy inputs (DND, contact
limits, retry counts, cooldowns), and never calls Razorpay. The policy
engine (src/policy.py) remains the sole decision authority regardless of
what this module reports, or which provider produced it. The LLM is
advisory, not authoritative.

Sends ONLY the failure signal (code / reason / description / amount /
currency / retry count on this payment) — never customer PII (name, email,
phone), never ground truth, never a scenario/case label, never any other
answer derived from ground truth.

`model_reported_confidence` is a raw, uncalibrated number from the model.
It is never treated as a calibrated probability; src/diagnosis.py's
confidence_band() converts it into HIGH/MEDIUM/LOW before anything
downstream (i.e. the policy engine) may use it.

Reliability: a single call to either provider can fail to produce a usable
structured response (Groq's forced tool-calling in particular has an
observed ~40-60% single-call success rate for some models — see the README's
Known limitations for the measured number). A bounded retry (2 attempts
total, one short backoff) applies ONLY to that failure shape — the model
responded but didn't produce a valid tool call, or the response body was
malformed — since that's usually a one-off hiccup worth one more try. A
transport error (timeout, connection failure) or a genuine API error
(bad credentials, unknown model, rate limit, server error) is NOT retried;
it falls back immediately, because retrying those doesn't fix anything and
just adds latency.

On any unresolved failure — missing API key, transport/API error, or a
response that still fails structural validation after the retry — this
module returns an explicit LLM_FALLBACK diagnosis at LOW confidence. It
never lets an LLM failure look like a successful diagnosis. HTTP errors
carry the response body (not just the status line) into the fallback
evidence, since both providers' error bodies say exactly what went wrong
(bad model id, malformed tool schema, invalid credentials, etc.) — the
status line alone ("Client error '400 Bad Request'") is not actionable.

Model ids are configurable via ANTHROPIC_MODEL / GROQ_MODEL env vars
(defaults below are both confirmed to support forced tool-calling).
"""

import json
import os
import time
from typing import Any

import httpx

from app.models.recovery_attempt import ConfidenceBand, DiagnosisSource
from src.diagnosis import Diagnosis, confidence_band

GROQ_API_BASE = "https://api.groq.com/openai/v1"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
DEFAULT_ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

REQUEST_TIMEOUT_SECONDS = 20.0
MAX_ATTEMPTS_ON_RETRYABLE_ERROR = 2  # total attempts (1 initial + 1 retry), not "2 retries"
RETRY_BACKOFF_SECONDS = 0.5

FALLBACK_ROOT_CAUSE = "llm_diagnosis_unavailable"
FALLBACK_CONFIDENCE = 0.0  # explicit zero, not a real estimate — forced to LOW band regardless of threshold

# JSON-schema "properties", shared verbatim between providers — only the surrounding tool-definition
# envelope differs (Groq/OpenAI-style "function.parameters" vs Anthropic's "input_schema").
_DIAGNOSIS_FIELDS: dict = {
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
}
_REQUIRED_FIELDS = ["root_cause", "confidence", "retryable", "evidence"]

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
    """The LLM call or its response can't be trusted. Not retried — either a
    genuine API/transport error, or a retry already happened and failed
    again."""


class RetryableLLMDiagnosisError(LLMDiagnosisError):
    """The model responded but didn't produce a usable structured result
    this turn (no tool call, malformed arguments, or a provider-reported
    tool-use failure) — worth exactly one bounded retry, since this is
    typically a one-off model hiccup rather than a systemic problem."""


def select_provider() -> str:
    """'anthropic' if ANTHROPIC_API_KEY is configured (the preferred
    default), else 'groq' if GROQ_API_KEY is configured, else 'none' (forces
    an immediate, explicit fallback rather than a pointless network call)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    return "none"


def _build_user_message(failure_signal: dict) -> str:
    return (
        "Diagnose this failed payment:\n"
        f"- failure_code: {failure_signal['failure_code']}\n"
        f"- failure_reason: {failure_signal['failure_reason']}\n"
        f"- failure_description: {failure_signal['failure_description']}\n"
        f"- amount: {failure_signal['amount']} {failure_signal['currency']}\n"
        f"- prior_retry_attempts_on_this_payment: {failure_signal['retry_count']}\n"
    )


def _groq_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "report_diagnosis",
            "description": "Report a structured diagnosis for one failed payment.",
            "parameters": {"type": "object", "properties": _DIAGNOSIS_FIELDS, "required": _REQUIRED_FIELDS},
        },
    }


def _anthropic_tool_schema() -> dict:
    return {
        "name": "report_diagnosis",
        "description": "Report a structured diagnosis for one failed payment.",
        "input_schema": {"type": "object", "properties": _DIAGNOSIS_FIELDS, "required": _REQUIRED_FIELDS},
    }


def _raise_for_status_with_body(response: httpx.Response, *, retryable_test) -> None:
    """Shared HTTP-error handling: surfaces the response BODY (not just the
    status line) in the error, and classifies it as retryable or not via the
    provider-specific `retryable_test(parsed_body_or_None) -> bool`."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body_text = response.text
        try:
            parsed = response.json()
        except Exception:  # noqa: BLE001 — body might not be JSON at all; that's fine, just can't classify further
            parsed = None
        message = f"{exc}. Response body: {body_text}"
        if retryable_test(parsed):
            raise RetryableLLMDiagnosisError(message) from exc
        raise LLMDiagnosisError(message) from exc


def _call_groq(failure_signal: dict, *, client: Any, model: str) -> dict:
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
            "tools": [_groq_tool_schema()],
            "tool_choice": {"type": "function", "function": {"name": "report_diagnosis"}},
        },
    )
    _raise_for_status_with_body(
        response, retryable_test=lambda parsed: bool(parsed) and parsed.get("error", {}).get("code") == "tool_use_failed"
    )
    body = response.json()

    tool_calls = body["choices"][0]["message"].get("tool_calls") or []
    for call in tool_calls:
        if call.get("function", {}).get("name") == "report_diagnosis":
            arguments = call["function"]["arguments"]
            try:
                return json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError as exc:
                raise RetryableLLMDiagnosisError(f"malformed tool-call arguments JSON: {exc}") from exc

    raise RetryableLLMDiagnosisError("Groq response did not include a report_diagnosis tool call.")


def _call_anthropic(failure_signal: dict, *, client: Any, model: str) -> dict:
    response = client.post(
        "/messages",
        json={
            "model": model,
            "max_tokens": 512,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _build_user_message(failure_signal)}],
            "tools": [_anthropic_tool_schema()],
            "tool_choice": {"type": "tool", "name": "report_diagnosis"},
        },
    )
    # Anthropic's forced tool-calling is reliable enough in practice that we don't guess at a
    # provider-specific "the model refused" error code the way Groq's tool_use_failed is handled —
    # any HTTP error here falls back immediately. A 200 OK with no usable tool_use block (below) is
    # still retried, since that's the provider-agnostic core of "malformed response".
    _raise_for_status_with_body(response, retryable_test=lambda parsed: False)
    body = response.json()

    for block in body.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "report_diagnosis":
            input_value = block.get("input")
            if not isinstance(input_value, dict):
                raise RetryableLLMDiagnosisError(f"tool_use block had a non-object input: {input_value!r}")
            return input_value

    raise RetryableLLMDiagnosisError("Claude response did not include a report_diagnosis tool_use block.")


def _call_llm(failure_signal: dict, *, client: Any, model: str, provider: str) -> dict:
    if provider == "anthropic":
        return _call_anthropic(failure_signal, client=client, model=model)
    return _call_groq(failure_signal, client=client, model=model)


def _validate(raw: Any) -> tuple[str, float, bool, str]:
    if not isinstance(raw, dict):
        raise RetryableLLMDiagnosisError(f"Expected a dict from the tool call, got {type(raw).__name__}")

    root_cause = raw.get("root_cause")
    confidence = raw.get("confidence")
    retryable = raw.get("retryable")
    evidence = raw.get("evidence")

    if not isinstance(root_cause, str) or not root_cause.strip():
        raise RetryableLLMDiagnosisError(f"Missing or empty root_cause: {root_cause!r}")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not (0.0 <= float(confidence) <= 1.0):
        raise RetryableLLMDiagnosisError(f"confidence must be a number in [0, 1], got {confidence!r}")
    if not isinstance(retryable, bool):
        raise RetryableLLMDiagnosisError(f"retryable must be a boolean, got {retryable!r}")
    if not isinstance(evidence, str) or not evidence.strip():
        raise RetryableLLMDiagnosisError(f"Missing or empty evidence: {evidence!r}")

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


def _default_model_for(provider: str) -> str:
    return DEFAULT_ANTHROPIC_MODEL if provider == "anthropic" else DEFAULT_GROQ_MODEL


def _api_key_env_for(provider: str) -> str:
    return "ANTHROPIC_API_KEY" if provider == "anthropic" else "GROQ_API_KEY"


def _build_client(provider: str, api_key: str) -> httpx.Client:
    if provider == "anthropic":
        return httpx.Client(
            base_url=ANTHROPIC_API_BASE,
            headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    return httpx.Client(base_url=GROQ_API_BASE, headers={"Authorization": f"Bearer {api_key}"}, timeout=REQUEST_TIMEOUT_SECONDS)


def diagnose_ambiguous_case(
    failure_signal: dict,
    *,
    client: Any | None = None,
    model: str | None = None,
    provider: str | None = None,
    sleep_fn=time.sleep,
) -> Diagnosis:
    """Attempts an LLM-backed diagnosis for a failure reason the rule table
    doesn't recognize. Always returns a Diagnosis — on any unresolved
    failure it returns an explicit LLM_FALLBACK diagnosis at LOW confidence
    rather than silently treating the failure as a successful diagnosis.

    `client` is an injection seam for tests (an httpx.Client, typically
    built with a MockTransport); production code leaves it None and a real
    httpx.Client is built for the selected provider. `provider` defaults to
    select_provider()'s auto-detection when not explicitly given — tests
    that inject a client should pass `provider` explicitly rather than rely
    on ambient environment state. `sleep_fn` is an injection seam so tests
    don't wait through the real retry backoff.
    """
    provider = provider or select_provider()
    if provider == "none":
        return _fallback("neither ANTHROPIC_API_KEY nor GROQ_API_KEY is configured")

    resolved_model = model or _default_model_for(provider)
    api_key = os.environ.get(_api_key_env_for(provider), "")
    if client is None and not api_key:
        return _fallback(f"{_api_key_env_for(provider)} not configured")

    owns_client = client is None
    active_client = client or _build_client(provider, api_key)

    try:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS_ON_RETRYABLE_ERROR + 1):
            try:
                raw = _call_llm(failure_signal, client=active_client, model=resolved_model, provider=provider)
                root_cause, confidence, retryable, evidence = _validate(raw)
            except RetryableLLMDiagnosisError as exc:
                last_error = exc
                if attempt < MAX_ATTEMPTS_ON_RETRYABLE_ERROR:
                    sleep_fn(RETRY_BACKOFF_SECONDS)
                    continue
                return _fallback(f"{type(exc).__name__} after {attempt} attempts: {exc}")
            except Exception as exc:  # noqa: BLE001 — non-retryable: transport error, non-tool_use_failed HTTP
                # error, or any other unexpected failure. This is the one place an external API's
                # failure must never be allowed to propagate as a crash or masquerade as success.
                return _fallback(f"{type(exc).__name__}: {exc}")
            else:
                return Diagnosis(
                    root_cause=root_cause,
                    confidence=confidence,
                    confidence_band=confidence_band(confidence),
                    evidence=f"LLM diagnosis ({provider}): {evidence}",
                    source=DiagnosisSource.LLM,
                    retryable=retryable,
                    never_auto=False,  # risk-block-level safety gates stay rule-table-only; see RULE_TABLE in diagnosis.py
                )
        # Unreachable in practice (the loop always returns), but keeps the function's return type
        # honest if MAX_ATTEMPTS_ON_RETRYABLE_ERROR were ever set to 0.
        return _fallback(f"exhausted retries: {last_error}")
    finally:
        if owns_client:
            active_client.close()
