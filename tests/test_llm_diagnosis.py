import json

import httpx

from app.models.recovery_attempt import ConfidenceBand, DiagnosisSource
from src.llm_diagnosis import GROQ_API_BASE, _build_user_message, diagnose_ambiguous_case

FAILURE_SIGNAL = {
    "failure_code": "SERVER_ERROR",
    "failure_reason": "issuer_soft_decline",
    "failure_description": "Issuer said 'try again' but flagged as permanent.",
    "amount": 150000,
    "currency": "INR",
    "retry_count": 0,
}


def _tool_call_response(arguments: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "report_diagnosis", "arguments": json.dumps(arguments)}}
                        ]
                    }
                }
            ]
        },
    )


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url=GROQ_API_BASE, transport=httpx.MockTransport(handler))


def test_successful_diagnosis_returns_llm_source_and_high_band():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openai/v1/chat/completions"
        return _tool_call_response(
            {"root_cause": "issuer_soft_decline_misclassified", "confidence": 0.91, "retryable": True, "evidence": "Issuer text explicitly says to retry."}
        )

    diagnosis = diagnose_ambiguous_case(FAILURE_SIGNAL, client=_client(handler))

    assert diagnosis.source == DiagnosisSource.LLM
    assert diagnosis.root_cause == "issuer_soft_decline_misclassified"
    assert diagnosis.confidence == 0.91
    assert diagnosis.confidence_band == ConfidenceBand.HIGH
    assert diagnosis.retryable is True
    assert diagnosis.never_auto is False  # the LLM can never set this — only the rule table can


def test_low_confidence_from_llm_bands_to_low():
    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_call_response({"root_cause": "unclear", "confidence": 0.35, "retryable": False, "evidence": "Genuinely unclear signal."})

    diagnosis = diagnose_ambiguous_case(FAILURE_SIGNAL, client=_client(handler))

    assert diagnosis.source == DiagnosisSource.LLM
    assert diagnosis.confidence_band == ConfidenceBand.LOW


def test_missing_field_in_tool_response_falls_back():
    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_call_response({"root_cause": "x", "confidence": 0.9})  # missing retryable, evidence

    diagnosis = diagnose_ambiguous_case(FAILURE_SIGNAL, client=_client(handler))

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK
    assert diagnosis.confidence_band == ConfidenceBand.LOW


def test_confidence_out_of_range_falls_back():
    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_call_response({"root_cause": "x", "confidence": 1.5, "retryable": True, "evidence": "e"})

    diagnosis = diagnose_ambiguous_case(FAILURE_SIGNAL, client=_client(handler))

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK


def test_malformed_json_in_tool_arguments_falls_back():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"tool_calls": [{"function": {"name": "report_diagnosis", "arguments": "{not valid json"}}]}}]},
        )

    diagnosis = diagnose_ambiguous_case(FAILURE_SIGNAL, client=_client(handler))

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK


def test_no_tool_call_falls_back():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "I refuse to use the tool."}}]})

    diagnosis = diagnose_ambiguous_case(FAILURE_SIGNAL, client=_client(handler))

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK


def test_http_error_falls_back_not_crashes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    diagnosis = diagnose_ambiguous_case(FAILURE_SIGNAL, client=_client(handler))

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK
    assert diagnosis.confidence_band == ConfidenceBand.LOW
    assert "401" in diagnosis.evidence or "invalid" in diagnosis.evidence.lower() or "StatusError" in diagnosis.evidence or "Error" in diagnosis.evidence


def test_connection_error_falls_back():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    diagnosis = diagnose_ambiguous_case(FAILURE_SIGNAL, client=_client(handler))

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK
    assert "connect" in diagnosis.evidence.lower()


def test_no_api_key_and_no_client_falls_back_without_calling_anything(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    diagnosis = diagnose_ambiguous_case(FAILURE_SIGNAL)

    assert diagnosis.source == DiagnosisSource.LLM_FALLBACK
    assert diagnosis.confidence == 0.0


def test_fallback_confidence_is_never_treated_as_a_real_score():
    """The fallback path uses an explicit sentinel (0.0), not a plausible-
    looking number that could be mistaken for a genuine low estimate."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    diagnosis = diagnose_ambiguous_case(FAILURE_SIGNAL, client=_client(handler))
    assert diagnosis.confidence == 0.0
    assert diagnosis.root_cause == "llm_diagnosis_unavailable"


def test_call_forces_the_diagnosis_tool_choice():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _tool_call_response({"root_cause": "x", "confidence": 0.9, "retryable": True, "evidence": "e"})

    diagnose_ambiguous_case(FAILURE_SIGNAL, client=_client(handler))

    assert captured["body"]["tool_choice"] == {"type": "function", "function": {"name": "report_diagnosis"}}
    assert captured["body"]["tools"][0]["function"]["name"] == "report_diagnosis"


def test_user_message_contains_only_the_failure_signal_no_pii_no_ground_truth():
    message = _build_user_message(FAILURE_SIGNAL)

    for field in ("failure_code", "failure_reason", "failure_description", "amount", "retry_count"):
        assert str(FAILURE_SIGNAL[field]) in message

    # Things that must never be sent to the LLM: customer PII, and anything answer-shaped.
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
        return _tool_call_response({"root_cause": "x", "confidence": 0.9, "retryable": True, "evidence": "e"})

    diagnose_ambiguous_case(FAILURE_SIGNAL, client=_client(handler))

    raw_lower = captured["raw"].lower()
    for forbidden in ("email", "dnd_opt_out", "ground_truth", "expected_action", "canonical_demo_case"):
        assert forbidden not in raw_lower
