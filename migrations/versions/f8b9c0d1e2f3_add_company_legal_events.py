"""add normalized company legal events

Revision ID: f8b9c0d1e2f3
Revises: e7a8b9c0d1e2
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "e7a8b9c0d1e2"
branch_labels = None
depends_on = None


EVENT_TYPES = (
    "bankruptcy_intent",
    "bankruptcy_application_filed",
    "bankruptcy_application_accepted",
    "bankruptcy_observation",
    "bankruptcy_financial_rehabilitation",
    "bankruptcy_external_administration",
    "bankruptcy_restructuring",
    "bankruptcy_asset_realisation",
    "bankruptcy_estate",
    "bankruptcy_procedure_terminated",
    "bankruptcy_procedure_completed",
    "liquidation_decision",
    "liquidation_in_process",
    "planned_exclusion",
    "actual_exclusion",
)


def upgrade() -> None:
    op.create_table(
        "company_legal_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("data_sets.id", ondelete="SET NULL")),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("publication_date", sa.Date()),
        sa.Column("status", sa.String(80), nullable=False),
        sa.Column("source_code", sa.String(80), nullable=False),
        sa.Column("source_identifier", sa.String(255), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("raw_event_type", sa.Text()),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "event_type IN (" + ", ".join(f"'{value}'" for value in EVENT_TYPES) + ")",
            name="ck_company_legal_event_type",
        ),
        sa.UniqueConstraint(
            "source_code", "source_identifier", "event_type", "company_id",
            name="uq_company_legal_event_source",
        ),
    )
    for column in (
        "company_id", "dataset_id", "event_type", "event_date",
        "publication_date", "status", "source_code", "source_identifier",
        "checked_at", "retrieved_at",
    ):
        op.create_index(f"ix_company_legal_events_{column}", "company_legal_events", [column])


def downgrade() -> None:
    op.drop_table("company_legal_events")
