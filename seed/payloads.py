"""Builders for synthetic failed-payment events shaped like a real Razorpay
`payment.failed` webhook payload, plus the failure-reason catalog the
synthetic dataset draws from.

Keeping the payload shape faithful to what Razorpay actually sends means the
diagnosis engine can be written and tested against these payloads today and
pointed at a real webhook receiver later with no reshaping in between.

Reference: https://razorpay.com/docs/webhooks/payloads/payments/

Reasons are split into two pools:
  - KNOWN_PROFILES: reasons the Day-3 rules engine is expected to recognize
    outright (clear transient vs. non-retryable vs. compliance-blocked).
  - AMBIGUOUS_PROFILES: reasons deliberately vague, conflicting, or absent
    from any rule table, so the batch forces genuine Claude-diagnosis cases
    rather than every record being rule-resolvable.
This split lives here (not in case_catalog) because it's a property of the
failure taxonomy itself, independent of which customer/history it's paired
with.
"""

import random
import time
import uuid
from dataclasses import dataclass


def rid(prefix: str) -> str:
    """A Razorpay-style id, e.g. pay_N4Kdd7oPZKh6xu.

    Drawn from the module-level `random` (not uuid.uuid4, which ignores
    random.seed()) so the whole synthetic batch is reproducible for a given
    seed — the same seed must always produce the same case catalog.
    """
    return f"{prefix}_{uuid.UUID(int=random.getrandbits(128)).hex[:14]}"


@dataclass(frozen=True)
class ErrorProfile:
    key: str  # stable case-catalog identifier, independent of the exact wording below
    error_code: str
    error_reason: str
    error_description: str
    error_source: str
    error_step: str
    retryable_hint: str  # "retryable" | "non_retryable" | "never_auto" | "ambiguous" — for case_catalog/ground-truth use only, never read by engine code


KNOWN_PROFILES: dict[str, ErrorProfile] = {
    p.key: p
    for p in [
        ErrorProfile(
            key="issuer_timeout",
            error_code="GATEWAY_ERROR",
            error_reason="issuer_timeout",
            error_description="The bank did not respond in time. This is usually a temporary issue.",
            error_source="issuer",
            error_step="payment_authentication",
            retryable_hint="retryable",
        ),
        ErrorProfile(
            key="insufficient_funds",
            error_code="GATEWAY_ERROR",
            error_reason="insufficient_funds",
            error_description="Payment failed as the account has insufficient funds.",
            error_source="bank",
            error_step="payment_authorization",
            retryable_hint="retryable",
        ),
        ErrorProfile(
            key="incorrect_otp",
            error_code="BAD_REQUEST_ERROR",
            error_reason="incorrect_otp",
            error_description="The customer entered an incorrect OTP during authentication.",
            error_source="customer",
            error_step="payment_authentication",
            retryable_hint="retryable",
        ),
        ErrorProfile(
            key="expired_card",
            error_code="BAD_REQUEST_ERROR",
            error_reason="expired_card",
            error_description="The card has expired and cannot be charged again.",
            error_source="customer",
            error_step="payment_authorization",
            retryable_hint="non_retryable",
        ),
        ErrorProfile(
            key="invalid_cvv",
            error_code="BAD_REQUEST_ERROR",
            error_reason="invalid_cvv",
            error_description="The CVV entered does not match the card on file.",
            error_source="customer",
            error_step="payment_authorization",
            retryable_hint="non_retryable",
        ),
        ErrorProfile(
            key="card_declined_do_not_honor",
            error_code="GATEWAY_ERROR",
            error_reason="card_declined_do_not_honor",
            error_description="The issuing bank declined the transaction with a permanent hold code (do not honor).",
            error_source="bank",
            error_step="payment_authorization",
            retryable_hint="non_retryable",
        ),
        ErrorProfile(
            key="risk_blocked",
            error_code="SERVER_ERROR",
            error_reason="payment_blocked_risk",
            error_description="Payment was blocked by Razorpay's risk engine as a precautionary measure.",
            error_source="business",
            error_step="payment_authorization",
            retryable_hint="never_auto",
        ),
    ]
}

AMBIGUOUS_PROFILES: dict[str, ErrorProfile] = {
    p.key: p
    for p in [
        ErrorProfile(
            key="gateway_declined_unspecified",
            error_code="GATEWAY_ERROR",
            error_reason="declined",
            error_description="The payment was declined by the gateway.",
            error_source="gateway",
            error_step="payment_authorization",
            retryable_hint="ambiguous",
        ),
        ErrorProfile(
            key="3ds_authentication_abandoned",
            error_code="BAD_REQUEST_ERROR",
            error_reason="authentication_abandoned",
            error_description="The customer did not complete 3-D Secure authentication before the session expired.",
            error_source="customer",
            error_step="payment_authentication",
            retryable_hint="ambiguous",
        ),
        ErrorProfile(
            key="conflicting_soft_decline",
            error_code="SERVER_ERROR",
            error_reason="issuer_soft_decline",
            error_description="Issuer response: 'please try again' (soft decline), but flagged under a permanent-failure error class.",
            error_source="issuer",
            error_step="payment_authorization",
            retryable_hint="ambiguous",
        ),
        ErrorProfile(
            key="unknown_error",
            error_code="SERVER_ERROR",
            error_reason="unknown",
            error_description="An unexpected error occurred while processing the payment.",
            error_source="gateway",
            error_step="payment_authorization",
            retryable_hint="ambiguous",
        ),
    ]
}


def build_failed_payment_payload(
    *, amount: int, currency: str, email: str, contact: str, profile: ErrorProfile
) -> dict:
    now = int(time.time())
    payment_id = rid("pay")
    order_id = rid("order")

    return {
        "entity": "event",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "currency": currency,
                    "status": "failed",
                    "order_id": order_id,
                    "method": "card",
                    "amount_refunded": 0,
                    "captured": False,
                    "email": email,
                    "contact": contact,
                    "error_code": profile.error_code,
                    "error_description": profile.error_description,
                    "error_source": profile.error_source,
                    "error_step": profile.error_step,
                    "error_reason": profile.error_reason,
                    "created_at": now,
                }
            }
        },
        "created_at": now,
    }
