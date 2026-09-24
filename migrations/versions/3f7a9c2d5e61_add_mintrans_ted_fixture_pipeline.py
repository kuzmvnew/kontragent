"""add Mintrans TED fixture pipeline storage

Revision ID: 3f7a9c2d5e61
Revises: c8e3f1a6b904
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "3f7a9c2d5e61"
down_revision = "c8e3f1a6b904"
branch_labels = None
depends_on = None


def _indexes(table: str, columns: tuple[str, ...]) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column], unique=False)


def upgrade() -> None:
    op.create_table(
        "mintrans_ted_raw_artifacts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("original_file_name", sa.String(length=500), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "source_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "size_bytes > 0", name="ck_mintrans_ted_raw_artifact_size"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["data_sets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_id",
            "sha256",
            name="uq_mintrans_ted_raw_artifact_checksum",
        ),
    )
    _indexes("mintrans_ted_raw_artifacts", ("dataset_id", "sha256"))

    op.create_table(
        "mintrans_ted_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=True),
        sa.Column("source_row_number", sa.BigInteger(), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column("ogrn", sa.String(length=15), nullable=True),
        sa.Column("registry_number", sa.String(length=200), nullable=False),
        sa.Column("included_at", sa.Date(), nullable=False),
        sa.Column("row_hash", sa.String(length=64), nullable=False),
        sa.Column("validation_state", sa.String(length=30), nullable=False),
        sa.Column("match_state", sa.String(length=30), nullable=False),
        sa.Column("match_method", sa.String(length=30), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "match_state IN ('matched', 'unmatched', 'conflict_identity')",
            name="ck_mintrans_ted_entry_match_state",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["mintrans_ted_raw_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["data_sets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_id", "row_hash", name="uq_mintrans_ted_entry_row_hash"
        ),
    )
    _indexes(
        "mintrans_ted_entries",
        (
            "dataset_id",
            "artifact_id",
            "company_id",
            "inn",
            "ogrn",
            "registry_number",
            "included_at",
            "row_hash",
            "match_state",
        ),
    )

    op.create_table(
        "mintrans_ted_quarantine_rows",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column("source_row_number", sa.BigInteger(), nullable=False),
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
            ["artifact_id"], ["mintrans_ted_raw_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["data_sets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "artifact_id",
            "source_row_number",
            "raw_hash",
            name="uq_mintrans_ted_quarantine_row",
        ),
    )
    _indexes(
        "mintrans_ted_quarantine_rows", ("dataset_id", "artifact_id", "raw_hash")
    )

    op.create_table(
        "transport_forwarding_registry_listings",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("source_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("fact_code", sa.String(length=120), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["data_sets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_entry_id"], ["mintrans_ted_entries.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_entry_id",
            name="uq_transport_forwarding_registry_listing_entry",
        ),
    )
    _indexes(
        "transport_forwarding_registry_listings",
        (
            "company_id",
            "dataset_id",
            "source_entry_id",
            "fact_code",
            "effective_from",
            "observed_at",
        ),
    )


def downgrade() -> None:
    op.drop_table("transport_forwarding_registry_listings")
    op.drop_table("mintrans_ted_quarantine_rows")
    op.drop_table("mintrans_ted_entries")
    op.drop_table("mintrans_ted_raw_artifacts")
