"""add persisted semantic fact identity

Revision ID: d1e2f3a4b5c6
Revises: c2a4f6d8e0b1
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d1e2f3a4b5c6"
down_revision = "c2a4f6d8e0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_semantic_facts",
        sa.Column("fact_ref", sa.String(length=41), nullable=False),
        sa.Column("item_ref", sa.String(length=41), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("section_key", sa.String(length=80), nullable=False),
        sa.Column("field_key", sa.String(length=120), nullable=False),
        sa.Column("period_identity", sa.String(length=80), server_default="", nullable=False),
        sa.Column("item_identity", sa.Text(), server_default="", nullable=False),
        sa.Column("selected_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "alternative_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "evidence_history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("rights", sa.String(length=30), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('FOUND','NOT_FOUND','NOT_APPLICABLE','NOT_CHECKED',"
            "'SOURCE_UNAVAILABLE','TIMEOUT','PARSING_ERROR','STALE_DATA','UNKNOWN','PARTIAL',"
            "'CONFLICTING_EVIDENCE')",
            name="ck_company_semantic_fact_state",
        ),
        sa.CheckConstraint(
            "rights IN ('PUBLIC','AUTHENTICATED_ONLY','INTERNAL_ONLY')",
            name="ck_company_semantic_fact_rights",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("fact_ref"),
        sa.UniqueConstraint(
            "company_id",
            "section_key",
            "field_key",
            "period_identity",
            "item_identity",
            name="uq_company_semantic_fact_coordinate",
        ),
    )
    op.create_index("ix_company_semantic_facts_company_id", "company_semantic_facts", ["company_id"])
    op.create_index("ix_company_semantic_facts_item_ref", "company_semantic_facts", ["item_ref"])
    op.create_index("ix_company_semantic_facts_section_key", "company_semantic_facts", ["section_key"])
    op.create_index("ix_company_semantic_facts_field_key", "company_semantic_facts", ["field_key"])
    op.create_index("ix_company_semantic_facts_state", "company_semantic_facts", ["state"])
    op.create_index("ix_company_semantic_facts_rights", "company_semantic_facts", ["rights"])
    op.create_index("ix_company_semantic_facts_is_current", "company_semantic_facts", ["is_current"])


def downgrade() -> None:
    op.drop_table("company_semantic_facts")
