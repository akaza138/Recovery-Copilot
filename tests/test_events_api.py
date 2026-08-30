import uuid

from app.models.customer import Customer
from app.models.event import EventStatus, EventType, RevenueEvent


def _seed_event(db_session, event_type: EventType, status: EventStatus = EventStatus.OPEN) -> RevenueEvent:
    customer = Customer(
        id=uuid.uuid4(),
        external_customer_id=f"cust_{uuid.uuid4().hex[:8]}",
        name="Test Customer",
        email="test@example.com",
        phone="9876543210",
        max_contact_attempts=3,
        dnd_opt_out=False,
    )
    db_session.add(customer)
    db_session.flush()

    event = RevenueEvent(
        id=uuid.uuid4(),
        external_event_id=f"pay_{uuid.uuid4().hex[:8]}",
        event_type=event_type,
        status=status,
        customer_id=customer.id,
        amount=10000,
        currency="INR",
        raw_payload={"event": "payment.failed"},
    )
    db_session.add(event)
    db_session.commit()
    return event


def test_list_events_empty(client):
    response = client.get("/events")

    assert response.status_code == 200
    body = response.json()
    assert body == {"total": 0, "items": []}


def test_list_events_returns_seeded_event(client, db_session):
    event = _seed_event(db_session, EventType.FAILED_PAYMENT)

    response = client.get("/events")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(event.id)


def test_list_events_filters_by_type(client, db_session):
    _seed_event(db_session, EventType.FAILED_PAYMENT)
    _seed_event(db_session, EventType.ABANDONED_CHECKOUT)

    response = client.get("/events", params={"event_type": "abandoned_checkout"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["event_type"] == "abandoned_checkout"


def test_get_event_not_found(client):
    response = client.get(f"/events/{uuid.uuid4()}")

    assert response.status_code == 404
