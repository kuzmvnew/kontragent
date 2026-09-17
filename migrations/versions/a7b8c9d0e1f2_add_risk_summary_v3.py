"""add immutable Risk/Coverage/Summary v3 persistence

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "company_risk_assessments_v3",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("assessment_id", sa.String(36), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("risk_engine_version", sa.String(40), nullable=False),
        sa.Column("coverage_engine_version", sa.String(40), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("risk_score", sa.Integer(), nullable=False),
        sa.Column("risk_label", sa.String(80), nullable=False),
        sa.Column("normalized_results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("assessment_id"),
    )
    for name, column in (("assessment_id","assessment_id"),("company_id","company_id"),("input_hash","input_hash"),("risk_engine_version","risk_engine_version"),("calculated_at","calculated_at"),("risk_score","risk_score")):
        op.create_index(f"ix_company_risk_assessments_v3_{name}", "company_risk_assessments_v3", [column])
    op.create_table(
        "company_summaries_v3",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("summary_id", sa.String(36), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("risk_assessment_id", sa.String(36), nullable=False),
        sa.Column("summary_engine_version", sa.String(40), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("structured_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["risk_assessment_id"], ["company_risk_assessments_v3.assessment_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("summary_id"), sa.UniqueConstraint("risk_assessment_id"),
    )
    for name, column in (("summary_id","summary_id"),("company_id","company_id"),("risk_assessment_id","risk_assessment_id"),("summary_engine_version","summary_engine_version"),("generated_at","generated_at")):
        op.create_index(f"ix_company_summaries_v3_{name}", "company_summaries_v3", [column])


def downgrade() -> None:
    op.drop_table("company_summaries_v3")
    op.drop_table("company_risk_assessments_v3")
