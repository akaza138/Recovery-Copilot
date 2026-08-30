"""initial schema: customers, revenue_events, audit_log_entries

Revision ID: 0001
Revises:
Create Date: 2026-08-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_customer_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("max_contact_attempts", sa.Integer(), nullable=False),
        sa.Column("dnd_opt_out", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_customers_external_customer_id", "customers", ["external_customer_id"], unique=True)

    event_type_enum = postgresql.ENUM(
        "failed_payment", "failed_mandate", "abandoned_checkout", name="event_type"
    )
    event_status_enum = postgresql.ENUM(
        "open", "recovered", "exhausted", "escalated", name="event_status"
    )
    event_type_enum.create(op.get_bind(), checkfirst=True)
    event_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "revenue_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", event_type_enum, nullable=False),
        sa.Column("status", event_status_enum, nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_reason", sa.String(length=64), nullable=True),
        sa.Column("error_description", sa.String(length=255), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_revenue_events_external_event_id", "revenue_events", ["external_event_id"], unique=True)
    op.create_index("ix_revenue_events_event_type", "revenue_events", ["event_type"])
    op.create_index("ix_revenue_events_status", "revenue_events", ["status"])

    op.create_table(
        "audit_log_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("revenue_events.id"), nullable=False),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("reasoning", sa.String(length=1000), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("extra_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_entries_event_id", "audit_log_entries", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_entries_event_id", table_name="audit_log_entries")
    op.drop_table("audit_log_entries")

    op.drop_index("ix_revenue_events_status", table_name="revenue_events")
    op.drop_index("ix_revenue_events_event_type", table_name="revenue_events")
    op.drop_index("ix_revenue_events_external_event_id", table_name="revenue_events")
    op.drop_table("revenue_events")

    postgresql.ENUM(name="event_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="event_type").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_customers_external_customer_id", table_name="customers")
    op.drop_table("customers")
