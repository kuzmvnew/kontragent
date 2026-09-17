"""add immutable Risk Engine assessments

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "company_risk_assessments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("assessment_id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("risk_engine_version", sa.String(length=40), nullable=False),
        sa.Column("ruleset_version", sa.String(length=80), nullable=False),
        sa.Column("ruleset_hash", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("deal_context_hash", sa.String(length=64), nullable=False),
        sa.Column("change_origin", sa.String(length=40), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_snapshot_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("signals", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("section_assessments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("completeness", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("overall_status", sa.String(length=40), nullable=False),
        sa.Column("limitations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assessment_id"),
    )
    for name, column in (
        ("ix_company_risk_assessments_assessment_id", "assessment_id"),
        ("ix_company_risk_assessments_company_id", "company_id"),
        ("ix_company_risk_assessments_risk_engine_version", "risk_engine_version"),
        ("ix_company_risk_assessments_ruleset_version", "ruleset_version"),
        ("ix_company_risk_assessments_ruleset_hash", "ruleset_hash"),
        ("ix_company_risk_assessments_input_hash", "input_hash"),
        ("ix_company_risk_assessments_deal_context_hash", "deal_context_hash"),
        ("ix_company_risk_assessments_change_origin", "change_origin"),
        ("ix_company_risk_assessments_calculated_at", "calculated_at"),
        ("ix_company_risk_assessments_overall_status", "overall_status"),
    ):
        op.create_index(name, "company_risk_assessments", [column], unique=False)


def downgrade() -> None:
    op.drop_table("company_risk_assessments")

