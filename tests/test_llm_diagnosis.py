import json

import httpx

from app.models.recovery_attempt import ConfidenceBand, DiagnosisSource
from src.llm_diagnosis import (
    ANTHROPIC_API_BASE,
    GROQ_API_BASE,
    _build_user_message,
    diagnose_ambiguous_case,
    select_provider,
)

FAILURE_SIGNAL = {
    "failure_code": "SERVER_ERROR",
    "failure_reason": "issuer_soft_decline",
    "failure_description": "Issuer said 'try again' but flagged as permanent.",
    "amount": 150000,
    "currency": "INR",
    "retry_count": 0,
}

_NO_SLEEP = lambda *_: None  # noqa: E731 — tests must never actually wait through the retry backoff


def _groq_tool_call_response(arguments: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"tool_calls": [{"function": {"name": "report_diagnosis", "arguments": json.dumps(arguments)}}]}}]},
    )


def _anthropic_tool_call_response(arguments: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"content": [{"type": "tool_use", "name": "report_diagnosis", "input": arguments}]},
    )


def _groq_client(handler) -> httpx.Client:
    return httpx.Client(base_url=GROQ_API_BASE, transport=httpx.MockTransport(handler))


def _anthropic_client(handler) -> httpx.Client:
    return httpx.Client(base_url=ANTHROPIC_API_BASE, transport=httpx.MockTransport(handler))


def _diagnose_groq(handler, **kwargs):
    return diagnose_ambiguous_case(FAILURE_SIGNAL, client=_groq_client(handler), provider="groq", sleep_fn=_NO_SLEEP, **kwargs)


def _diagnose_anthropic(handler, **kwargs):
    return diagnose_ambiguous_case(FAILURE_SIGNAL, client=_anthropic_client(handler), provider="anthropic", sleep_fn=_NO_SLEEP, **kwargs)


# --------------------------------------------------------------------------
# Provider selection
# --------------------------------------------------------------------------


def test_select_provider_prefers_anthropic_when_both_configured(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    assert select_provider() == "anthropic"


def test_select_provider_falls_back_to_groq_when_only_groq_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake")
    assert select_provider() == "groq"


def test_select_provider_returns_none_when_neither_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert select_provider() == "none"


def test_no_api_key_and_no_client_falls_back_without_calling_anything(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    diagnosis = diagnose_ambiguous_case(FAILURE_SIGNAL)

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK
    assert diagnosis.confidence == 0.0
    assert "neither" in diagnosis.evidence.lower()


# --------------------------------------------------------------------------
# Groq provider — success, validation, and error paths
# --------------------------------------------------------------------------


def test_groq_successful_diagnosis_returns_llm_source_and_high_band():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openai/v1/chat/completions"
        return _groq_tool_call_response(
            {"root_cause": "issuer_soft_decline_misclassified", "confidence": 0.91, "retryable": True, "evidence": "Issuer text explicitly says to retry."}
        )

    diagnosis = _diagnose_groq(handler)

    assert diagnosis.source == DiagnosisSource.LLM
    assert diagnosis.root_cause == "issuer_soft_decline_misclassified"
    assert diagnosis.confidence == 0.91
    assert diagnosis.confidence_band == ConfidenceBand.HIGH
    assert diagnosis.retryable is True
    assert diagnosis.never_auto is False  # the LLM can never set this — only the rule table can


def test_groq_low_confidence_bands_to_low():
    def handler(request: httpx.Request) -> httpx.Response:
        return _groq_tool_call_response({"root_cause": "unclear", "confidence": 0.35, "retryable": False, "evidence": "Genuinely unclear signal."})

    diagnosis = _diagnose_groq(handler)

    assert diagnosis.source == DiagnosisSource.LLM
    assert diagnosis.confidence_band == ConfidenceBand.LOW


def test_groq_http_error_falls_back_not_crashes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    diagnosis = _diagnose_groq(handler)

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK
    assert diagnosis.confidence_band == ConfidenceBand.LOW
    assert "401" in diagnosis.evidence
    assert "invalid api key" in diagnosis.evidence  # the response BODY, not just the status line


def test_groq_http_error_evidence_surfaces_the_response_body_not_just_the_status_line():
    """The status line alone ("Client error '400 Bad Request'") gives no
    diagnostic information — Groq's response body (which says exactly what
    was wrong: bad model id, malformed tool schema, etc.) makes it into the
    fallback evidence."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "The model `bogus-model` does not exist or you do not have access to it.", "code": "model_not_found"}},
        )

    diagnosis = _diagnose_groq(handler)

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK
    assert "does not exist or you do not have access to it" in diagnosis.evidence
    assert "model_not_found" in diagnosis.evidence


def test_groq_connection_error_falls_back():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    diagnosis = _diagnose_groq(handler)

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK
    assert "connect" in diagnosis.evidence.lower()


def test_fallback_confidence_is_never_treated_as_a_real_score():
    """The fallback path uses an explicit sentinel (0.0), not a plausible-
    looking number that could be mistaken for a genuine low estimate."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    diagnosis = _diagnose_groq(handler)
    assert diagnosis.confidence == 0.0
    assert diagnosis.root_cause == "llm_diagnosis_unavailable"


def test_groq_call_forces_the_diagnosis_tool_choice():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _groq_tool_call_response({"root_cause": "x", "confidence": 0.9, "retryable": True, "evidence": "e"})

    _diagnose_groq(handler)

    assert captured["body"]["tool_choice"] == {"type": "function", "function": {"name": "report_diagnosis"}}
    assert captured["body"]["tools"][0]["function"]["name"] == "report_diagnosis"


# --------------------------------------------------------------------------
# Anthropic provider — success, validation, and error paths
# --------------------------------------------------------------------------


def test_anthropic_successful_diagnosis_returns_llm_source():
    # Note: the injected test client (unlike the real _build_client()) doesn't set x-api-key itself —
    # that header's construction is exercised separately in test_build_client_sets_provider_headers.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        return _anthropic_tool_call_response({"root_cause": "issuer_soft_decline_misclassified", "confidence": 0.93, "retryable": True, "evidence": "Explicit retry instruction."})

    diagnosis = _diagnose_anthropic(handler)

    assert diagnosis.source == DiagnosisSource.LLM
    assert diagnosis.confidence == 0.93
    assert diagnosis.confidence_band == ConfidenceBand.HIGH
    assert diagnosis.retryable is True


def test_anthropic_call_forces_the_diagnosis_tool_choice():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _anthropic_tool_call_response({"root_cause": "x", "confidence": 0.9, "retryable": True, "evidence": "e"})

    _diagnose_anthropic(handler)

    assert captured["body"]["tool_choice"] == {"type": "tool", "name": "report_diagnosis"}
    assert captured["body"]["tools"][0]["name"] == "report_diagnosis"
    assert captured["body"]["tools"][0]["input_schema"]["required"] == ["root_cause", "confidence", "retryable", "evidence"]


def test_anthropic_no_tool_use_block_falls_back():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "I decline to use a tool."}]})

    diagnosis = _diagnose_anthropic(handler)

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK


def test_anthropic_http_error_falls_back_with_response_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"type": "authentication_error", "message": "invalid x-api-key"}})

    diagnosis = _diagnose_anthropic(handler)

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK
    assert "invalid x-api-key" in diagnosis.evidence


# --------------------------------------------------------------------------
# Bounded retry — ONLY for tool_use_failed / malformed-response cases
# --------------------------------------------------------------------------


def test_tool_use_failed_retries_once_then_succeeds():
    """The exact failure shape observed in practice: Groq returns HTTP 400
    with code=tool_use_failed on the first call. The bounded retry should
    recover a genuine LLM diagnosis on the second attempt rather than
    falling back after only one try."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(400, json={"error": {"message": "model did not call a tool", "code": "tool_use_failed"}})
        return _groq_tool_call_response({"root_cause": "recovered_on_retry", "confidence": 0.88, "retryable": True, "evidence": "e"})

    diagnosis = _diagnose_groq(handler)

    assert call_count["n"] == 2
    assert diagnosis.source == DiagnosisSource.LLM
    assert diagnosis.root_cause == "recovered_on_retry"


def test_tool_use_failed_persisting_falls_back_after_exactly_two_attempts():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(400, json={"error": {"message": "model did not call a tool", "code": "tool_use_failed"}})

    diagnosis = _diagnose_groq(handler)

    assert call_count["n"] == 2  # bounded: exactly 2 attempts total, not unlimited
    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK


def test_no_tool_call_is_retried_and_bounded():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "I refuse to use the tool."}}]})

    diagnosis = _diagnose_groq(handler)

    assert call_count["n"] == 2
    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK


def test_malformed_json_in_tool_arguments_is_retried():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"function": {"name": "report_diagnosis", "arguments": "{not valid json"}}]}}]})

    diagnosis = _diagnose_groq(handler)

    assert call_count["n"] == 2
    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK


def test_validation_failure_missing_field_is_retried():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return _groq_tool_call_response({"root_cause": "x", "confidence": 0.9})  # missing retryable, evidence

    diagnosis = _diagnose_groq(handler)

    assert call_count["n"] == 2
    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK
    assert diagnosis.confidence_band == ConfidenceBand.LOW


def test_confidence_out_of_range_is_retried_then_falls_back():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return _groq_tool_call_response({"root_cause": "x", "confidence": 1.5, "retryable": True, "evidence": "e"})

    diagnosis = _diagnose_groq(handler)

    assert call_count["n"] == 2
    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK


def test_validation_failure_recovers_on_retry_if_second_attempt_is_valid():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _groq_tool_call_response({"root_cause": "x", "confidence": 0.9})  # missing fields — invalid
        return _groq_tool_call_response({"root_cause": "valid_on_second_try", "confidence": 0.87, "retryable": False, "evidence": "e"})

    diagnosis = _diagnose_groq(handler)

    assert call_count["n"] == 2
    assert diagnosis.source == DiagnosisSource.LLM
    assert diagnosis.root_cause == "valid_on_second_try"


def test_generic_http_error_does_not_retry():
    """A non-tool_use_failed HTTP error (bad credentials, unknown model,
    server error, ...) is NOT retried — retrying doesn't fix a credentials
    problem, it just adds latency before the (inevitable) fallback."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    diagnosis = _diagnose_groq(handler)

    assert call_count["n"] == 1  # no retry attempted
    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK


def test_transport_error_does_not_retry():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        raise httpx.ConnectError("connection refused")

    diagnosis = _diagnose_groq(handler)

    assert call_count["n"] == 1
    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK


def test_retry_uses_the_injected_sleep_fn_with_the_configured_backoff():
    from src.llm_diagnosis import RETRY_BACKOFF_SECONDS

    sleep_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "x", "code": "tool_use_failed"}})

    diagnose_ambiguous_case(
        FAILURE_SIGNAL, client=_groq_client(handler), provider="groq", sleep_fn=lambda seconds: sleep_calls.append(seconds)
    )

    assert sleep_calls == [RETRY_BACKOFF_SECONDS]  # exactly one backoff, between the two attempts


def test_build_client_sets_provider_headers():
    from src.llm_diagnosis import _build_client

    anthropic_client = _build_client("anthropic", "sk-ant-fake")
    assert anthropic_client.headers["x-api-key"] == "sk-ant-fake"
    assert anthropic_client.headers["anthropic-version"]
    anthropic_client.close()

    groq_client = _build_client("groq", "gsk-fake")
    assert groq_client.headers["authorization"] == "Bearer gsk-fake"
    groq_client.close()


def test_anthropic_no_tool_use_block_is_also_retried_and_bounded():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"content": [{"type": "text", "text": "no tool"}]})

    diagnosis = _diagnose_anthropic(handler)

    assert call_count["n"] == 2
    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK


# --------------------------------------------------------------------------
# Privacy: only the failure signal, never PII or ground truth
# --------------------------------------------------------------------------


def test_user_message_contains_only_the_failure_signal_no_pii_no_ground_truth():
    message = _build_user_message(FAILURE_SIGNAL)

    for field in ("failure_code", "failure_reason", "failure_description", "amount", "retry_count"):
        assert str(FAILURE_SIGNAL[field]) in message

    for forbidden in ("email", "phone", "name", "dnd_opt_out", "ground_truth", "expected_action", "case_label", "canonical_demo_case"):
        assert forbidden not in message.lower()


def test_prompt_sent_to_llm_contains_only_whitelisted_signal_keys():
    """Belt-and-suspenders: even if a caller accidentally passed extra keys
    on the failure_signal dict, the outgoing message is built from a fixed
    set of named fields, not a dump of the whole dict."""
    leaky_signal = {**FAILURE_SIGNAL, "customer_email": "leak@example.com", "ground_truth_label": "case_c"}
    message = _build_user_message(leaky_signal)
    assert "leak@example.com" not in message
    assert "case_c" not in message


def test_request_never_leaks_pii_or_ground_truth_over_the_wire():
    """End-to-end check at the transport layer: inspect the actual bytes
    sent, not just the helper function's output."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw"] = request.content.decode("utf-8")
        return _groq_tool_call_response({"root_cause": "x", "confidence": 0.9, "retryable": True, "evidence": "e"})

    _diagnose_groq(handler)

    raw_lower = captured["raw"].lower()
    for forbidden in ("email", "dnd_opt_out", "ground_truth", "expected_action", "canonical_demo_case"):
        assert forbidden not in raw_lower
