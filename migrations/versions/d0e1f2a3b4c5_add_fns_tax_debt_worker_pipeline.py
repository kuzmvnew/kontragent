"""add FNS tax debt worker pipeline

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


FACT_CODE = "tax.debt.amount_as_of_date"


def _indexes(table: str, columns: tuple[str, ...]) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column], unique=False)


def upgrade() -> None:
    op.create_table(
        "fns_tax_debt_raw_artifacts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("first_worker_run_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_reference", sa.Text(), nullable=False),
        sa.Column("original_file_name", sa.String(length=500), nullable=False),
        sa.Column("media_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("source_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "size_bytes > 0", name="ck_fns_tax_debt_raw_artifact_size"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["data_sets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_id", "sha256", name="uq_fns_tax_debt_raw_artifact_checksum"
        ),
    )
    _indexes(
        "fns_tax_debt_raw_artifacts",
        ("dataset_id", "first_worker_run_id", "sha256", "source_as_of", "retrieved_at"),
    )

    op.create_table(
        "fns_tax_debt_normalized_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=True),
        sa.Column("source_member", sa.String(length=500), nullable=False),
        sa.Column("source_ordinal", sa.BigInteger(), nullable=False),
        sa.Column("inn", sa.String(length=10), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("source_document_id", sa.String(length=255), nullable=True),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("total_arrears", sa.Numeric(20, 2), nullable=False),
        sa.Column("total_penalties", sa.Numeric(20, 2), nullable=False),
        sa.Column("total_fines", sa.Numeric(20, 2), nullable=False),
        sa.Column("total_debt", sa.Numeric(20, 2), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("normalization_version", sa.String(length=40), nullable=False),
        sa.Column(
            "validation_state",
            sa.String(length=30),
            server_default="valid",
            nullable=False,
        ),
        sa.Column("match_state", sa.String(length=30), nullable=False),
        sa.Column("match_method", sa.String(length=30), nullable=True),
        sa.Column(
            "limitation_states",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "match_state IN ('matched', 'unmatched', 'conflict')",
            name="ck_fns_tax_debt_normalized_match_state",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["fns_tax_debt_raw_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dataset_id"], ["data_sets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "artifact_id",
            "record_hash",
            name="uq_fns_tax_debt_normalized_artifact_record",
        ),
    )
    _indexes(
        "fns_tax_debt_normalized_records",
        (
            "dataset_id",
            "artifact_id",
            "company_id",
            "inn",
            "source_document_id",
            "data_date",
            "record_hash",
            "match_state",
        ),
    )

    op.create_table(
        "fns_tax_debt_quarantine_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column("source_member", sa.String(length=500), nullable=False),
        sa.Column("source_ordinal", sa.BigInteger(), nullable=False),
        sa.Column("raw_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "reason_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["fns_tax_debt_raw_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["dataset_id"], ["data_sets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "artifact_id",
            "source_member",
            "source_ordinal",
            "raw_hash",
            name="uq_fns_tax_debt_quarantine_record",
        ),
    )
    _indexes(
        "fns_tax_debt_quarantine_records",
        ("dataset_id", "artifact_id", "raw_hash"),
    )

    op.add_column(
        "company_tax_debt_snapshots",
        sa.Column("normalized_record_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "company_tax_debt_snapshots",
        sa.Column(
            "fact_code",
            sa.String(length=120),
            server_default=FACT_CODE,
            nullable=False,
        ),
    )
    op.add_column(
        "company_tax_debt_snapshots",
        sa.Column("source_reference", sa.Text(), nullable=True),
    )
    op.add_column(
        "company_tax_debt_snapshots",
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "company_tax_debt_snapshots",
        sa.Column(
            "limitation_states",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "company_tax_debt_snapshots",
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_company_tax_debt_fact_code",
        "company_tax_debt_snapshots",
        f"fact_code = '{FACT_CODE}'",
    )
    op.create_foreign_key(
        "fk_company_tax_debt_normalized_record",
        "company_tax_debt_snapshots",
        "fns_tax_debt_normalized_records",
        ["normalized_record_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_company_tax_debt_snapshots_normalized_record_id",
        "company_tax_debt_snapshots",
        ["normalized_record_id"],
        unique=True,
    )
    op.create_index(
        "ix_company_tax_debt_snapshots_fact_code",
        "company_tax_debt_snapshots",
        ["fact_code"],
        unique=False,
    )
    op.create_index(
        "ix_company_tax_debt_snapshots_retrieved_at",
        "company_tax_debt_snapshots",
        ["retrieved_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_company_tax_debt_snapshots_retrieved_at",
        table_name="company_tax_debt_snapshots",
    )
    op.drop_index(
        "ix_company_tax_debt_snapshots_fact_code",
        table_name="company_tax_debt_snapshots",
    )
    op.drop_index(
        "ix_company_tax_debt_snapshots_normalized_record_id",
        table_name="company_tax_debt_snapshots",
    )
    op.drop_constraint(
        "fk_company_tax_debt_normalized_record",
        "company_tax_debt_snapshots",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_company_tax_debt_fact_code",
        "company_tax_debt_snapshots",
        type_="check",
    )
    for column in (
        "retrieved_at",
        "limitation_states",
        "provenance",
        "source_reference",
        "fact_code",
        "normalized_record_id",
    ):
        op.drop_column("company_tax_debt_snapshots", column)

    for table, columns in (
        (
            "fns_tax_debt_quarantine_records",
            ("raw_hash", "artifact_id", "dataset_id"),
        ),
        (
            "fns_tax_debt_normalized_records",
            (
                "match_state",
                "record_hash",
                "data_date",
                "source_document_id",
                "inn",
                "company_id",
                "artifact_id",
                "dataset_id",
            ),
        ),
        (
            "fns_tax_debt_raw_artifacts",
            ("retrieved_at", "source_as_of", "sha256", "first_worker_run_id", "dataset_id"),
        ),
    ):
        for column in columns:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)
