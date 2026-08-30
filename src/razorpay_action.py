"""Executes the chosen recovery action against Razorpay's test-mode REST API
(Orders / Payment Links) directly over HTTPS — same approach as
scripts/check_razorpay_keys.py, not the razorpay PyPI package (which pulls
in a legacy pkg_resources import current setuptools no longer ships).

Honesty rule, load-bearing for the whole system: this module never reports
ActionResult.SUCCEEDED. Creating an order or a payment link is something we
can genuinely confirm via the API response — but neither confirms a
*completed* payment. That requires the customer to finish checkout, and this
project must never submit card data server-side to fake that (out of PCI
scope, and a prohibited action in its own right). So a successfully placed
action is reported PENDING, never SUCCEEDED, until a later build-order step
adds a real confirmation path (webhook receipt or status polling).
"""

import os
from dataclasses import dataclass

import httpx

from app.models.recovery_attempt import ActionMode, ActionResult

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"


@dataclass(frozen=True)
class ActionOutcome:
    action_mode: ActionMode
    action_result: ActionResult
    razorpay_reference: str | None
    evidence: str


class RazorpayActionClient:
    """Thin wrapper around the two Razorpay test-mode APIs this system uses.

    `transport` lets tests inject an httpx.MockTransport so the real request
    shape and response handling are exercised without a network call.
    """

    def __init__(
        self,
        *,
        key_id: str | None = None,
        key_secret: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.key_id = key_id if key_id is not None else os.environ.get("RAZORPAY_KEY_ID", "")
        self.key_secret = key_secret if key_secret is not None else os.environ.get("RAZORPAY_KEY_SECRET", "")
        self._transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(self.key_id) and bool(self.key_secret) and self.key_id.startswith("rzp_test_")

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=RAZORPAY_API_BASE,
            auth=(self.key_id, self.key_secret),
            timeout=15.0,
            transport=self._transport,
        )

    def execute_retry(self, *, amount: int, currency: str, receipt: str) -> ActionOutcome:
        if not self.is_configured:
            return ActionOutcome(
                action_mode=ActionMode.SIMULATED,
                action_result=ActionResult.PENDING,
                razorpay_reference=None,
                evidence="No test-mode Razorpay credentials configured; retry simulated rather than executed.",
            )

        try:
            with self._client() as client:
                response = client.post(
                    "/orders",
                    json={"amount": amount, "currency": currency, "payment_capture": 1, "receipt": receipt},
                )
                response.raise_for_status()
                order = response.json()
        except httpx.HTTPError as exc:
            return ActionOutcome(
                action_mode=ActionMode.REAL,
                action_result=ActionResult.FAILED,
                razorpay_reference=None,
                evidence=f"Razorpay Orders API call failed: {exc}",
            )

        return ActionOutcome(
            action_mode=ActionMode.REAL,
            action_result=ActionResult.PENDING,
            razorpay_reference=order["id"],
            evidence=(
                f"Created real test-mode order {order['id']} (status={order.get('status')}) as the retry attempt. "
                "Order creation is confirmed by Razorpay; actual payment completion is not — this project does not "
                "submit card data server-side, so completion can only be confirmed later via webhook/status polling."
            ),
        )

    def execute_payment_link(self, *, amount: int, currency: str, description: str) -> ActionOutcome:
        if not self.is_configured:
            return ActionOutcome(
                action_mode=ActionMode.SIMULATED,
                action_result=ActionResult.PENDING,
                razorpay_reference=None,
                evidence="No test-mode Razorpay credentials configured; payment link simulated rather than created.",
            )

        try:
            with self._client() as client:
                response = client.post(
                    "/payment_links",
                    json={"amount": amount, "currency": currency, "description": description},
                )
                response.raise_for_status()
                link = response.json()
        except httpx.HTTPError as exc:
            return ActionOutcome(
                action_mode=ActionMode.REAL,
                action_result=ActionResult.FAILED,
                razorpay_reference=None,
                evidence=f"Razorpay Payment Links API call failed: {exc}",
            )

        return ActionOutcome(
            action_mode=ActionMode.REAL,
            action_result=ActionResult.PENDING,
            razorpay_reference=link["id"],
            evidence=(
                f"Created real test-mode payment link {link['id']} ({link.get('short_url')}). "
                "Link creation is confirmed by Razorpay; whether the customer completes it is not yet known."
            ),
        )
