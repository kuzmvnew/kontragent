"""add cbr warning list entries

Revision ID: d4c9b1a2e7f0
Revises: b8f4c2d91e7a
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d4c9b1a2e7f0"
down_revision: Union[str, Sequence[str], None] = "b8f4c2d91e7a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cbr_warning_list_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("cbr_id", sa.String(length=80), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=True),
        sa.Column("update_date", sa.Date(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column(
            "sites",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "signs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "regions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("additional_info", sa.Text(), nullable=True),
        sa.Column("liquidation_status", sa.String(length=100), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("org_type", sa.String(length=200), nullable=True),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["data_sets.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_id",
            "cbr_id",
            name="uq_cbr_warning_list_dataset_cbr_id",
        ),
    )

    for column in (
        "dataset_id",
        "data_date",
        "cbr_id",
        "inn",
        "entry_date",
        "update_date",
    ):
        op.create_index(
            f"ix_cbr_warning_list_entries_{column}",
            "cbr_warning_list_entries",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("cbr_warning_list_entries")
