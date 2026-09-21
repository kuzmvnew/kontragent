"""add immutable Summary Engine summaries

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "company_summaries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("summary_id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("risk_assessment_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("summary_engine_version", sa.String(length=40), nullable=False),
        sa.Column("risk_engine_version", sa.String(length=40), nullable=False),
        sa.Column("ruleset_version", sa.String(length=80), nullable=False),
        sa.Column("deal_context_hash", sa.String(length=64), nullable=False),
        sa.Column("projection_policy_version", sa.String(length=80), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("structured_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("text_blocks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("explainability_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["risk_assessment_id"], ["company_risk_assessments.assessment_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("summary_id"),
        sa.UniqueConstraint(
            "risk_assessment_id", "mode", "summary_engine_version", "deal_context_hash",
            "projection_policy_version", name="uq_company_summaries_cache_key",
        ),
    )
    for name, column in (
        ("ix_company_summaries_summary_id", "summary_id"),
        ("ix_company_summaries_company_id", "company_id"),
        ("ix_company_summaries_risk_assessment_id", "risk_assessment_id"),
        ("ix_company_summaries_mode", "mode"),
        ("ix_company_summaries_summary_engine_version", "summary_engine_version"),
        ("ix_company_summaries_generated_at", "generated_at"),
    ):
        op.create_index(name, "company_summaries", [column], unique=False)


def downgrade() -> None:
    op.drop_table("company_summaries")
