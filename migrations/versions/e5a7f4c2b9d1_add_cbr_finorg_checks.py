"""add cbr finorg checks

Revision ID: e5a7f4c2b9d1
Revises: d4c9b1a2e7f0
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e5a7f4c2b9d1"
down_revision: Union[str, Sequence[str], None] = "d4c9b1a2e7f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cbr_finorg_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=False),
        sa.Column("request_date", sa.Date(), nullable=False),
        sa.Column("result_status", sa.String(length=30), nullable=False),
        sa.Column("is_participant", sa.Boolean(), nullable=True),
        sa.Column("cbr_id", sa.BigInteger(), nullable=True),
        sa.Column("ogrn", sa.String(length=15), nullable=True),
        sa.Column("short_name", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=True),
        sa.Column(
            "fo_types",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "licenses",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "payment_systems",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "mfo_history",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "websites",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("phones", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("regnum", sa.String(length=80), nullable=True),
        sa.Column("bic", sa.String(length=40), nullable=True),
        sa.Column("is_sro_member", sa.Boolean(), nullable=True),
        sa.Column("has_branches", sa.Boolean(), nullable=True),
        sa.Column("registration_date", sa.Date(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "checked_at",
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
            "inn",
            "request_date",
            name="uq_cbr_finorg_dataset_inn_request_date",
        ),
    )

    for column in (
        "dataset_id",
        "inn",
        "request_date",
        "result_status",
        "is_participant",
        "cbr_id",
        "ogrn",
        "status",
        "error_code",
        "checked_at",
    ):
        op.create_index(
            f"ix_cbr_finorg_checks_{column}",
            "cbr_finorg_checks",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("cbr_finorg_checks")
