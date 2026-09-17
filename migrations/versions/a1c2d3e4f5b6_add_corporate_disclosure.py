"""add corporate disclosure checks

Revision ID: a1c2d3e4f5b6
Revises: f8b9c0d1e2f3
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1c2d3e4f5b6"
down_revision: Union[str, Sequence[str], None] = "f8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corporate_disclosure_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inn", sa.String(10), nullable=False),
        sa.Column("request_date", sa.Date(), nullable=False),
        sa.Column("result_status", sa.String(30), nullable=False),
        sa.Column("is_found", sa.Boolean()),
        sa.Column("profile", postgresql.JSONB()),
        sa.Column("documents", postgresql.JSONB()),
        sa.Column("document_count", sa.Integer()),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_message", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("dataset_id", "inn", "request_date", name="uq_corporate_disclosure_check"),
    )
    for column in ("company_id", "dataset_id", "inn", "request_date", "result_status", "is_found", "error_code", "checked_at"):
        op.create_index(f"ix_corporate_disclosure_checks_{column}", "corporate_disclosure_checks", [column])


def downgrade() -> None:
    op.drop_table("corporate_disclosure_checks")
