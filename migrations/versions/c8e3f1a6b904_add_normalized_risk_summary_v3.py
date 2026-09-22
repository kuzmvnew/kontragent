"""add isolated normalized Risk/Summary v3 persistence

Revision ID: c8e3f1a6b904
Revises: a7d4e9f2c6b1
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c8e3f1a6b904"
down_revision = "a7d4e9f2c6b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_risk_assessments_v3",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("assessment_id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("subject_scope", sa.String(length=40), nullable=False),
        sa.Column("risk_model_version", sa.String(length=40), nullable=False),
        sa.Column("ruleset_version", sa.String(length=80), nullable=False),
        sa.Column("coverage_policy_version", sa.String(length=80), nullable=False),
        sa.Column(
            "applicability_policy_version", sa.String(length=80), nullable=False
        ),
        sa.Column(
            "source_resolution_policy_version",
            sa.String(length=80),
            nullable=False,
        ),
        sa.Column("freshness_policy_version", sa.String(length=80), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "evidence_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "resolved_checks",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("factors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "coverage_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "mandatory_gate",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "limitations", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assessment_id"),
        sa.UniqueConstraint(
            "company_id",
            "input_hash",
            name="uq_company_risk_assessments_v3_input_identity",
        ),
    )
    op.create_index(
        "ix_company_risk_assessments_v3_company_id",
        "company_risk_assessments_v3",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_company_risk_assessments_v3_company_calculated",
        "company_risk_assessments_v3",
        ["company_id", "calculated_at"],
        unique=False,
    )
    op.create_index(
        "ix_company_risk_assessments_v3_input_hash",
        "company_risk_assessments_v3",
        ["input_hash"],
        unique=False,
    )

    op.create_table(
        "company_summaries_v3",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("summary_id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("risk_assessment_id", sa.String(length=36), nullable=False),
        sa.Column("summary_model_version", sa.String(length=40), nullable=False),
        sa.Column("projection_policy_version", sa.String(length=80), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "structured_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "explainability_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["risk_assessment_id"],
            ["company_risk_assessments_v3.assessment_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("summary_id"),
        sa.UniqueConstraint(
            "risk_assessment_id",
            "summary_model_version",
            "projection_policy_version",
            name="uq_company_summaries_v3_cache_identity",
        ),
    )
    op.create_index(
        "ix_company_summaries_v3_company_id",
        "company_summaries_v3",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_company_summaries_v3_risk_assessment_id",
        "company_summaries_v3",
        ["risk_assessment_id"],
        unique=False,
    )
    op.create_index(
        "ix_company_summaries_v3_company_generated",
        "company_summaries_v3",
        ["company_id", "generated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("company_summaries_v3")
    op.drop_table("company_risk_assessments_v3")
