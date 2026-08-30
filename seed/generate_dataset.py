"""Generates the synthetic revenue-at-risk batch: customers plus 50+ events
spread across failed payments, failed subscription mandates, and abandoned
checkouts, each carrying a Razorpay-shaped webhook payload.

Usage:
    python -m seed.generate_dataset [--customers 20] [--events 60] [--reset]
"""

import argparse
import random
import uuid

from faker import Faker

from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.customer import Customer
from app.models.event import EventType, RevenueEvent
from seed.payloads import (
    FAILED_MANDATE_PROFILES,
    FAILED_PAYMENT_PROFILES,
    build_abandoned_checkout_payload,
    build_failed_mandate_payload,
    build_failed_payment_payload,
)

CURRENCY = "INR"
AMOUNT_RANGE = (5_000, 25_00_000)  # paise: ₹50 to ₹25,000


def _make_customers(fake: Faker, count: int) -> list[Customer]:
    customers = []
    for _ in range(count):
        customers.append(
            Customer(
                id=uuid.uuid4(),
                external_customer_id=f"cust_{uuid.uuid4().hex[:14]}",
                name=fake.name(),
                email=fake.email(),
                phone=fake.msisdn()[:10],
                max_contact_attempts=random.choice([2, 3, 3, 4, 5]),
                dnd_opt_out=random.random() < 0.15,
            )
        )
    return customers


def _make_event(fake: Faker, customer: Customer, event_type: EventType) -> RevenueEvent:
    amount = random.randint(*AMOUNT_RANGE)

    if event_type == EventType.FAILED_PAYMENT:
        profile = random.choice(FAILED_PAYMENT_PROFILES)
        payload = build_failed_payment_payload(
            amount=amount, currency=CURRENCY, email=customer.email, contact=customer.phone, profile=profile
        )
        payment = payload["payload"]["payment"]["entity"]
        external_id = payment["id"]
        retry_count = random.choice([0, 0, 0, 1, 2])

    elif event_type == EventType.FAILED_MANDATE:
        profile = random.choice(FAILED_MANDATE_PROFILES)
        billing_cycle = random.randint(1, 12)
        payload = build_failed_mandate_payload(
            amount=amount,
            currency=CURRENCY,
            email=customer.email,
            contact=customer.phone,
            profile=profile,
            billing_cycle=billing_cycle,
        )
        payment = payload["payload"]["payment"]["entity"]
        external_id = payment["id"]
        retry_count = random.choice([0, 0, 1, 1, 2])

    else:  # ABANDONED_CHECKOUT
        expire_after = random.choice([900, 1800, 3600, 86400])  # 15m, 30m, 1h, 24h
        payload = build_abandoned_checkout_payload(
            amount=amount, currency=CURRENCY, email=customer.email, contact=customer.phone, expire_after_seconds=expire_after
        )
        plink = payload["payload"]["payment_link"]["entity"]
        external_id = plink["id"]
        profile = None
        retry_count = 0

    return RevenueEvent(
        id=uuid.uuid4(),
        external_event_id=external_id,
        event_type=event_type,
        customer_id=customer.id,
        amount=amount,
        currency=CURRENCY,
        error_code=profile.error_code if profile else None,
        error_reason=profile.error_reason if profile else "checkout_abandoned",
        error_description=profile.error_description if profile else "Payment link expired before the customer completed checkout.",
        retry_count=retry_count,
        raw_payload=payload,
    )


def generate(*, num_customers: int, num_events: int, reset: bool) -> None:
    Base.metadata.create_all(bind=engine)  # convenience for local/dev runs; Alembic remains the source of truth for schema.

    fake = Faker()
    Faker.seed(42)
    random.seed(42)

    db = SessionLocal()
    try:
        if reset:
            db.query(RevenueEvent).delete()
            db.query(Customer).delete()
            db.commit()

        customers = _make_customers(fake, num_customers)
        db.add_all(customers)
        db.flush()

        # Roughly even thirds across the three in-scope event types, biased
        # slightly toward failed payments since that's the deepest path.
        weights = {
            EventType.FAILED_PAYMENT: 0.4,
            EventType.FAILED_MANDATE: 0.3,
            EventType.ABANDONED_CHECKOUT: 0.3,
        }
        event_types = random.choices(
            population=list(weights.keys()), weights=list(weights.values()), k=num_events
        )

        events = [_make_event(fake, random.choice(customers), event_type) for event_type in event_types]
        db.add_all(events)
        db.commit()

        print(f"Seeded {len(customers)} customers and {len(events)} events:")
        for event_type in EventType:
            n = sum(1 for e in events if e.event_type == event_type)
            print(f"  {event_type.value}: {n}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customers", type=int, default=20)
    parser.add_argument("--events", type=int, default=60)
    parser.add_argument("--reset", action="store_true", help="Delete existing events/customers before seeding.")
    args = parser.parse_args()

    generate(num_customers=args.customers, num_events=args.events, reset=args.reset)


if __name__ == "__main__":
    main()
