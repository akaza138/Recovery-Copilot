"""initial schema: customers, failed_payments, recovery_attempts

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
        sa.Column("dnd_opt_out", sa.Boolean(), nullable=False),
        sa.Column("max_contact_attempts", sa.Integer(), nullable=False),
        sa.Column("contact_count", sa.Integer(), nullable=False),
        sa.Column("prior_recovery_attempts", sa.Integer(), nullable=False),
        sa.Column("prior_recovery_successes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_customers_external_customer_id", "customers", ["external_customer_id"], unique=True)

    failed_payment_status_enum = postgresql.ENUM(
        "open", "confirmed_recovered", "simulated_recovered", "escalated", "unresolved",
        name="failed_payment_status",
    )
    failed_payment_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "failed_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_payment_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=False),
        sa.Column("failure_reason", sa.String(length=64), nullable=False),
        sa.Column("failure_description", sa.String(length=255), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("status", failed_payment_status_enum, nullable=False),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_failed_payments_external_payment_id", "failed_payments", ["external_payment_id"], unique=True)
    op.create_index("ix_failed_payments_status", "failed_payments", ["status"])

    diagnosis_source_enum = postgresql.ENUM("rule_based", "claude", name="diagnosis_source")
    confidence_band_enum = postgresql.ENUM("high", "medium", "low", name="confidence_band")
    decision_action_enum = postgresql.ENUM("retry", "payment_link", "escalate", "refuse", name="decision_action")
    action_mode_enum = postgresql.ENUM("real", "simulated", name="action_mode")
    action_result_enum = postgresql.ENUM("succeeded", "failed", "pending", "not_executed", name="action_result")
    for enum_type in (diagnosis_source_enum, confidence_band_enum, decision_action_enum, action_mode_enum, action_result_enum):
        enum_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "recovery_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("failed_payment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("failed_payments.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("diagnosis_root_cause", sa.String(length=64), nullable=False),
        sa.Column("diagnosis_source", diagnosis_source_enum, nullable=False),
        sa.Column("model_reported_confidence", sa.Float(), nullable=True),
        sa.Column("confidence_band", confidence_band_enum, nullable=False),
        sa.Column("diagnosis_reasoning", sa.String(length=1000), nullable=False),
        sa.Column("decision_action", decision_action_enum, nullable=False),
        sa.Column("decision_factors", sa.JSON(), nullable=False),
        sa.Column("action_mode", action_mode_enum, nullable=False),
        sa.Column("action_result", action_result_enum, nullable=False),
        sa.Column("razorpay_reference", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_recovery_attempts_failed_payment_id", "recovery_attempts", ["failed_payment_id"])


def downgrade() -> None:
    op.drop_index("ix_recovery_attempts_failed_payment_id", table_name="recovery_attempts")
    op.drop_table("recovery_attempts")
    for enum_name in ("action_result", "action_mode", "decision_action", "confidence_band", "diagnosis_source"):
        postgresql.ENUM(name=enum_name).drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_failed_payments_status", table_name="failed_payments")
    op.drop_index("ix_failed_payments_external_payment_id", table_name="failed_payments")
    op.drop_table("failed_payments")
    postgresql.ENUM(name="failed_payment_status").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_customers_external_customer_id", table_name="customers")
    op.drop_table("customers")
