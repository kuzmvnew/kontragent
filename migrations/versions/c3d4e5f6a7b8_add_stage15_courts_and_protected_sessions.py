"""add stage 1.5 courts and protected source sessions

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "general_court_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("request_date", sa.Date(), nullable=False),
        sa.Column("provider_code", sa.String(80), nullable=False),
        sa.Column("result_status", sa.String(30), nullable=False),
        sa.Column("cases", postgresql.JSONB()),
        sa.Column("coverage", postgresql.JSONB()),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_message", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("dataset_id", "inn", "request_date", name="uq_general_court_check"),
    )
    for column in ("company_id", "dataset_id", "inn", "request_date", "provider_code", "result_status", "error_code", "checked_at"):
        op.create_index(f"ix_general_court_checks_{column}", "general_court_checks", [column])

    op.create_table(
        "arbitration_court_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("result_status", sa.String(30), nullable=False),
        sa.Column("loaded_pages", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_pages", sa.Integer()),
        sa.Column("total_count", sa.Integer()),
        sa.Column("cases", postgresql.JSONB()),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_message", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("dataset_id", "inn", "date_from", "date_to", name="uq_arbitration_court_check"),
    )
    for column in ("company_id", "dataset_id", "inn", "result_status", "error_code", "checked_at"):
        op.create_index(f"ix_arbitration_court_checks_{column}", "arbitration_court_checks", [column])

    op.create_table(
        "interactive_protected_source_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("source_code", sa.String(80), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("result", sa.String(80)),
        sa.Column("evidence", postgresql.JSONB()),
        sa.Column("evidence_hash", sa.String(64)),
        sa.Column("browser_metadata", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    for column in ("company_id", "inn", "source_code", "status", "result", "evidence_hash", "created_at", "checked_at"):
        op.create_index(f"ix_interactive_protected_source_sessions_{column}", "interactive_protected_source_sessions", [column])


def downgrade() -> None:
    op.drop_table("interactive_protected_source_sessions")
    op.drop_table("arbitration_court_checks")
    op.drop_table("general_court_checks")
