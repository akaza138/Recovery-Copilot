import uuid

from app.models.customer import Customer
from app.models.event import EventStatus, EventType, RevenueEvent


def _make_customer() -> Customer:
    return Customer(
        id=uuid.uuid4(),
        external_customer_id=f"cust_{uuid.uuid4().hex[:8]}",
        name="Asha Rao",
        email="asha@example.com",
        phone="9876543210",
        max_contact_attempts=3,
        dnd_opt_out=False,
    )


def test_customer_round_trip(db_session):
    customer = _make_customer()
    db_session.add(customer)
    db_session.commit()

    fetched = db_session.get(Customer, customer.id)
    assert fetched is not None
    assert fetched.email == "asha@example.com"
    assert fetched.dnd_opt_out is False


def test_revenue_event_defaults_and_relationship(db_session):
    customer = _make_customer()
    db_session.add(customer)
    db_session.flush()

    event = RevenueEvent(
        id=uuid.uuid4(),
        external_event_id="pay_abc123",
        event_type=EventType.FAILED_PAYMENT,
        customer_id=customer.id,
        amount=50000,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="card_declined",
        error_description="Your card was declined by the issuing bank.",
        raw_payload={"event": "payment.failed"},
    )
    db_session.add(event)
    db_session.commit()

    fetched = db_session.get(RevenueEvent, event.id)
    assert fetched.status == EventStatus.OPEN  # column default applies
    assert fetched.retry_count == 0
    assert fetched.customer.id == customer.id
    assert fetched in customer.events
