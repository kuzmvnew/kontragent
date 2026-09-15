"""add erknm inspections

Revision ID: b8f4c2d91e7a
Revises: a41c9f2e7b60
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b8f4c2d91e7a"
down_revision: Union[str, Sequence[str], None] = "a41c9f2e7b60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "erknm_inspections",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("period_year", sa.Integer(), nullable=False),
        sa.Column("period_month", sa.Integer(), nullable=False),
        sa.Column("erpid", sa.String(length=64), nullable=False),
        sa.Column("classification", sa.String(length=100), nullable=True),
        sa.Column("creation_source", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=300), nullable=True),
        sa.Column("status_key", sa.String(length=100), nullable=True),
        sa.Column("control_level", sa.String(length=100), nullable=True),
        sa.Column("supervision_name", sa.Text(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("prosecutor_office", sa.Text(), nullable=True),
        sa.Column("kind_control", sa.Text(), nullable=True),
        sa.Column("kind_knm", sa.Text(), nullable=True),
        sa.Column("kno_organization", sa.Text(), nullable=True),
        sa.Column("subject_inn", sa.String(length=12), nullable=True),
        sa.Column("subject_ogrn", sa.String(length=15), nullable=True),
        sa.Column("subject_name", sa.Text(), nullable=True),
        sa.Column("subject_type", sa.String(length=100), nullable=True),
        sa.Column("subject_guid", sa.String(length=64), nullable=True),
        sa.Column("msp_code", sa.String(length=300), nullable=True),
        sa.Column("place", sa.Text(), nullable=True),
        sa.Column("object_address", sa.Text(), nullable=True),
        sa.Column("object_type", sa.Text(), nullable=True),
        sa.Column("object_kind", sa.Text(), nullable=True),
        sa.Column("object_sub_kind", sa.Text(), nullable=True),
        sa.Column("risk_category", sa.Text(), nullable=True),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("warning_caption", sa.Text(), nullable=True),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("inspection_attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("subject_attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("okveds", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("objects", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("inspectors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(["dataset_id"], ["data_sets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_id",
            "erpid",
            name="uq_erknm_inspections_dataset_erpid",
        ),
    )

    for column in (
        "dataset_id",
        "data_date",
        "period_year",
        "period_month",
        "erpid",
        "classification",
        "status",
        "status_key",
        "start_date",
        "end_date",
        "kind_knm",
        "subject_inn",
        "subject_ogrn",
        "subject_type",
        "risk_category",
    ):
        op.create_index(
            f"ix_erknm_inspections_{column}",
            "erknm_inspections",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("erknm_inspections")
