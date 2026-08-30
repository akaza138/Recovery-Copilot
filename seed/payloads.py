"""Builders for synthetic events shaped like real Razorpay webhook payloads.

Keeping the shape faithful to what Razorpay actually sends means the
diagnosis engine can be written and tested against these payloads today and
pointed at a real webhook receiver later with no reshaping in between.

Reference shapes: https://razorpay.com/docs/webhooks/payloads/payments/
                   https://razorpay.com/docs/webhooks/payloads/subscriptions/
                   https://razorpay.com/docs/webhooks/payloads/payment-links/
"""

import random
import time
import uuid
from dataclasses import dataclass


def _rid(prefix: str) -> str:
    """A Razorpay-style id, e.g. pay_N4Kdd7oPZKh6xu."""
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


@dataclass(frozen=True)
class ErrorProfile:
    error_code: str
    error_reason: str
    error_description: str
    error_source: str
    error_step: str


# --- Failed payment reasons ------------------------------------------------
# The five causes the recovery engine is scoped to diagnose and act on today.
FAILED_PAYMENT_PROFILES: list[ErrorProfile] = [
    ErrorProfile(
        error_code="BAD_REQUEST_ERROR",
        error_reason="card_declined",
        error_description="Your card was declined by the issuing bank. Try another card.",
        error_source="bank",
        error_step="payment_authorization",
    ),
    ErrorProfile(
        error_code="GATEWAY_ERROR",
        error_reason="insufficient_funds",
        error_description="Payment failed as the account has insufficient funds.",
        error_source="bank",
        error_step="payment_authorization",
    ),
    ErrorProfile(
        error_code="GATEWAY_ERROR",
        error_reason="issuer_timeout",
        error_description="The bank did not respond in time. This is usually a temporary issue.",
        error_source="issuer",
        error_step="payment_authentication",
    ),
    ErrorProfile(
        error_code="SERVER_ERROR",
        error_reason="payment_blocked_risk",
        error_description="Payment was blocked by Razorpay's risk engine as a precautionary measure.",
        error_source="business",
        error_step="payment_authorization",
    ),
    ErrorProfile(
        error_code="GATEWAY_ERROR",
        error_reason="mandate_expired",
        error_description="The e-mandate used for this payment has expired.",
        error_source="bank",
        error_step="payment_authorization",
    ),
]

# --- Failed subscription mandate reasons -----------------------------------
FAILED_MANDATE_PROFILES: list[ErrorProfile] = [
    ErrorProfile(
        error_code="GATEWAY_ERROR",
        error_reason="insufficient_funds",
        error_description="The customer's account did not have sufficient balance for the mandate charge.",
        error_source="bank",
        error_step="payment_authorization",
    ),
    ErrorProfile(
        error_code="BAD_REQUEST_ERROR",
        error_reason="mandate_not_approved",
        error_description="The customer has not approved the auto-debit mandate with their bank.",
        error_source="customer",
        error_step="payment_authorization",
    ),
    ErrorProfile(
        error_code="GATEWAY_ERROR",
        error_reason="account_closed",
        error_description="The bank account linked to this mandate has been closed.",
        error_source="bank",
        error_step="payment_authorization",
    ),
    ErrorProfile(
        error_code="BAD_REQUEST_ERROR",
        error_reason="mandate_revoked",
        error_description="The customer revoked the auto-debit mandate with their bank.",
        error_source="customer",
        error_step="payment_authorization",
    ),
]


def build_failed_payment_payload(*, amount: int, currency: str, email: str, contact: str, profile: ErrorProfile) -> dict:
    now = int(time.time())
    payment_id = _rid("pay")
    order_id = _rid("order")

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
                    "method": random.choice(["card", "netbanking", "upi"]),
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


def build_failed_mandate_payload(
    *, amount: int, currency: str, email: str, contact: str, profile: ErrorProfile, billing_cycle: int
) -> dict:
    now = int(time.time())
    subscription_id = _rid("sub")
    payment_id = _rid("pay")

    return {
        "entity": "event",
        "event": "subscription.charge.failed",
        "contains": ["subscription", "payment"],
        "payload": {
            "subscription": {
                "entity": {
                    "id": subscription_id,
                    "entity": "subscription",
                    "status": "active",
                    "current_start": now - billing_cycle * 30 * 86400,
                    "current_end": now,
                    "charge_at": now,
                    "paid_count": billing_cycle,
                }
            },
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "currency": currency,
                    "status": "failed",
                    "method": "emandate",
                    "email": email,
                    "contact": contact,
                    "error_code": profile.error_code,
                    "error_description": profile.error_description,
                    "error_source": profile.error_source,
                    "error_step": profile.error_step,
                    "error_reason": profile.error_reason,
                    "created_at": now,
                }
            },
        },
        "created_at": now,
    }


def build_abandoned_checkout_payload(*, amount: int, currency: str, email: str, contact: str, expire_after_seconds: int) -> dict:
    """Razorpay has no native "cart abandoned" webhook. This mirrors the real
    `payment_link.expired` event, which is the closest first-class signal:
    a payment link was created and never paid before it expired."""
    now = int(time.time())
    plink_id = _rid("plink")

    return {
        "entity": "event",
        "event": "payment_link.expired",
        "contains": ["payment_link"],
        "payload": {
            "payment_link": {
                "entity": {
                    "id": plink_id,
                    "entity": "payment_link",
                    "amount": amount,
                    "currency": currency,
                    "status": "expired",
                    "customer": {"email": email, "contact": contact},
                    "created_at": now - expire_after_seconds,
                    "expire_by": now,
                    "expired_at": now,
                }
            }
        },
        "created_at": now,
    }
