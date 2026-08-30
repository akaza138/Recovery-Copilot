import httpx
import pytest

from app.models.recovery_attempt import ActionMode, ActionResult
from src.razorpay_action import RazorpayActionClient

TEST_KEY_ID = "rzp_test_fake0000000001"
TEST_KEY_SECRET = "fake_secret"


def _client(handler) -> RazorpayActionClient:
    return RazorpayActionClient(key_id=TEST_KEY_ID, key_secret=TEST_KEY_SECRET, transport=httpx.MockTransport(handler))


def test_execute_retry_creates_real_order_and_reports_pending_not_succeeded():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/orders"
        # Simulate a response that even *looks* like it might mean "paid" — the client must not
        # be fooled into claiming SUCCEEDED off surface-level fields it isn't allowed to trust.
        return httpx.Response(200, json={"id": "order_fake123", "status": "paid", "amount": 10000})

    outcome = _client(handler).execute_retry(amount=10000, currency="INR", receipt="pay_test")

    assert outcome.action_mode == ActionMode.REAL
    assert outcome.action_result == ActionResult.PENDING  # never SUCCEEDED, regardless of what the order status says
    assert outcome.razorpay_reference == "order_fake123"


def test_execute_payment_link_creates_real_link_and_reports_pending():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/payment_links"
        return httpx.Response(200, json={"id": "plink_fake123", "short_url": "https://rzp.io/x/fake", "status": "created"})

    outcome = _client(handler).execute_payment_link(amount=10000, currency="INR", description="test")

    assert outcome.action_mode == ActionMode.REAL
    assert outcome.action_result == ActionResult.PENDING
    assert outcome.razorpay_reference == "plink_fake123"


def test_execute_retry_reports_failed_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"description": "bad auth"}})

    outcome = _client(handler).execute_retry(amount=10000, currency="INR", receipt="pay_test")

    assert outcome.action_mode == ActionMode.REAL  # a real call was made; it just failed
    assert outcome.action_result == ActionResult.FAILED
    assert outcome.razorpay_reference is None


def test_execute_payment_link_reports_failed_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"description": "server error"}})

    outcome = _client(handler).execute_payment_link(amount=10000, currency="INR", description="test")

    assert outcome.action_mode == ActionMode.REAL
    assert outcome.action_result == ActionResult.FAILED


def test_unconfigured_client_simulates_rather_than_pretending():
    client = RazorpayActionClient(key_id="", key_secret="")
    assert client.is_configured is False

    outcome = client.execute_retry(amount=10000, currency="INR", receipt="pay_test")
    assert outcome.action_mode == ActionMode.SIMULATED
    assert outcome.action_result == ActionResult.PENDING
    assert outcome.razorpay_reference is None


def test_live_mode_key_is_never_treated_as_configured():
    """Defense in depth: even if a live key ended up in the environment, this
    client must never treat it as usable."""
    client = RazorpayActionClient(key_id="rzp_live_shouldnotuse", key_secret="x")
    assert client.is_configured is False


@pytest.mark.parametrize("execute", ["execute_retry", "execute_payment_link"])
def test_no_code_path_ever_returns_succeeded(execute):
    """No fake recovered result: across every response shape we can throw at
    it, these methods must never produce ActionResult.SUCCEEDED."""
    responses = [
        httpx.Response(200, json={"id": "x", "status": "created", "short_url": "https://rzp.io/x"}),
        httpx.Response(200, json={"id": "x", "status": "paid", "short_url": "https://rzp.io/x"}),
        httpx.Response(400, json={"error": {"description": "bad request"}}),
        httpx.Response(500, json={"error": {"description": "boom"}}),
    ]

    for response in responses:

        def handler(request: httpx.Request, _response=response) -> httpx.Response:
            return _response

        client = _client(handler)
        method = getattr(client, execute)
        if execute == "execute_retry":
            outcome = method(amount=10000, currency="INR", receipt="pay_test")
        else:
            outcome = method(amount=10000, currency="INR", description="test")

        assert outcome.action_result != ActionResult.SUCCEEDED
