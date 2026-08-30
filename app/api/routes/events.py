import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.event import EventStatus, EventType, RevenueEvent
from app.schemas.audit_log import AuditLogEntryRead
from app.schemas.event import RevenueEventList, RevenueEventRead

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=RevenueEventList)
def list_events(
    event_type: EventType | None = None,
    status: EventStatus | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> RevenueEventList:
    stmt = select(RevenueEvent)
    if event_type is not None:
        stmt = stmt.where(RevenueEvent.event_type == event_type)
    if status is not None:
        stmt = stmt.where(RevenueEvent.status == status)

    total = len(db.execute(stmt).scalars().all())
    items = db.execute(stmt.order_by(RevenueEvent.created_at.desc()).limit(limit).offset(offset)).scalars().all()

    return RevenueEventList(total=total, items=list(items))


@router.get("/{event_id}", response_model=RevenueEventRead)
def get_event(event_id: uuid.UUID, db: Session = Depends(get_db)) -> RevenueEvent:
    event = db.get(RevenueEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    return event


@router.get("/{event_id}/audit-log", response_model=list[AuditLogEntryRead])
def get_event_audit_log(event_id: uuid.UUID, db: Session = Depends(get_db)) -> list:
    event = db.get(RevenueEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    return event.audit_entries
