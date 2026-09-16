"""add fns sme support entries

Revision ID: f7b2d8a4c1e3
Revises: e5a7f4c2b9d1
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f7b2d8a4c1e3"
down_revision: Union[str, Sequence[str], None] = "e5a7f4c2b9d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fns_sme_support_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("ingestion_run_id", sa.BigInteger(), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("source_record_key", sa.String(length=220), nullable=False),
        sa.Column("source_document_id", sa.String(length=100), nullable=False),
        sa.Column("provider_inn", sa.String(length=10), nullable=True),
        sa.Column("recipient_inn", sa.String(length=12), nullable=False),
        sa.Column("recipient_kind", sa.String(length=40), nullable=False),
        sa.Column("recipient_ogrn", sa.String(length=15), nullable=True),
        sa.Column("information_date", sa.Date(), nullable=False),
        sa.Column("support_until", sa.Date(), nullable=False),
        sa.Column("decision_date", sa.Date(), nullable=False),
        sa.Column("termination_date", sa.Date(), nullable=True),
        sa.Column("violation_code", sa.String(length=1), nullable=True),
        sa.Column("support_form_code", sa.String(length=10), nullable=True),
        sa.Column("support_form_name", sa.Text(), nullable=True),
        sa.Column("support_type_code", sa.String(length=10), nullable=True),
        sa.Column("support_type_name", sa.Text(), nullable=True),
        sa.Column(
            "amounts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "violations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "regulatory_document_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["data_sets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"], ["ingestion_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ingestion_run_id",
            "source_record_key",
            name="uq_fns_sme_support_run_record_key",
        ),
    )

    op.create_index(
        "ix_fns_sme_support_run_inn_decision",
        "fns_sme_support_entries",
        ["ingestion_run_id", "recipient_inn", "decision_date"],
        unique=False,
    )
    op.create_index(
        "ix_fns_sme_support_dataset_run",
        "fns_sme_support_entries",
        ["dataset_id", "ingestion_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("fns_sme_support_entries")
