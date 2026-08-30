"""One-shot script to confirm Razorpay test-mode credentials actually work,
against the two APIs the recovery engine depends on: Orders and Payment
Links. This is a Day-1 checkpoint — everything downstream depends on this
working, so it's de-risked before any engine code is written.

Calls the REST API directly (Basic Auth over HTTPS) rather than the
`razorpay` PyPI package, which pulls in a legacy `pkg_resources` import that
current `setuptools` no longer ships.

Refuses to run against anything that isn't a test-mode key (`rzp_test_...`)
as a hard safety check: this project must never be able to touch live mode.

Usage:
    python -m scripts.check_razorpay_keys
"""

import os
import sys

import httpx
from dotenv import load_dotenv

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"


def main() -> int:
    load_dotenv()

    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")

    if not key_id or not key_secret:
        print("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set. Fill them into .env and re-run.", file=sys.stderr)
        return 1

    if not key_id.startswith("rzp_test_"):
        print(
            f"Refusing to run: key_id '{key_id}' is not a test-mode key (must start with 'rzp_test_'). "
            "This project must never touch live mode.",
            file=sys.stderr,
        )
        return 1

    auth = (key_id, key_secret)

    with httpx.Client(base_url=RAZORPAY_API_BASE, auth=auth, timeout=15.0) as client:
        print("Checking Orders API...")
        try:
            response = client.post("/orders", json={"amount": 100, "currency": "INR", "payment_capture": 1})
            response.raise_for_status()
            order = response.json()
            print(f"  OK — created test-mode order {order['id']} (amount={order['amount']} {order['currency']})")
        except httpx.HTTPError as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)
            if isinstance(exc, httpx.HTTPStatusError):
                print(f"  Response: {exc.response.text}", file=sys.stderr)
            return 1

        print("Checking Payment Links API...")
        try:
            response = client.post(
                "/payment_links",
                json={
                    "amount": 100,
                    "currency": "INR",
                    "description": "Recovery Copilot test-mode connectivity check",
                },
            )
            response.raise_for_status()
            link = response.json()
            print(f"  OK — created test-mode payment link {link['id']} ({link['short_url']})")
        except httpx.HTTPError as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)
            if isinstance(exc, httpx.HTTPStatusError):
                print(f"  Response: {exc.response.text}", file=sys.stderr)
            return 1

    print("\nBoth APIs reachable in test mode. Keys are good to build against.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
