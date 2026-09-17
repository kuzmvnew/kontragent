"""add normalized company public facts

Revision ID: b2c3d4e5f6a7
Revises: a1c2d3e4f5b6
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1c2d3e4f5b6"
branch_labels = None
depends_on = None


FACT_TYPES = (
    "website", "phone", "email", "registered_address", "factual_address", "postal_address",
    "mass_address", "mass_director", "mass_founder", "public_bank_details", "related_company",
)


def upgrade() -> None:
    op.create_table(
        "company_public_facts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("data_sets.id", ondelete="SET NULL")),
        sa.Column("fact_type", sa.String(60), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.Column("source_code", sa.String(80), nullable=False),
        sa.Column("source_identifier", sa.String(255), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("publication_date", sa.Date()),
        sa.Column("effective_from", sa.Date()),
        sa.Column("effective_to", sa.Date()),
        sa.Column("currentness", sa.String(30), nullable=False),
        sa.Column("confidence", sa.String(30), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("fact_type IN (" + ", ".join(f"'{value}'" for value in FACT_TYPES) + ")", name="ck_company_public_fact_type"),
        sa.UniqueConstraint("company_id", "fact_type", "value_hash", "source_code", "source_identifier", name="uq_company_public_fact_evidence"),
    )
    for column in ("company_id", "dataset_id", "fact_type", "value_hash", "source_code", "source_identifier", "publication_date", "effective_from", "effective_to", "currentness", "confidence", "observed_at"):
        op.create_index(f"ix_company_public_facts_{column}", "company_public_facts", [column])


def downgrade() -> None:
    op.drop_table("company_public_facts")
