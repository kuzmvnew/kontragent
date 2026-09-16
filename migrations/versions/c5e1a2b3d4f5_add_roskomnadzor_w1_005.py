"""add Roskomnadzor W1-005 datasets

Revision ID: c5e1a2b3d4f5
Revises: a9c4e6f8b201
Create Date: 2026-09-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c5e1a2b3d4f5"
down_revision: Union[str, Sequence[str], None] = "a9c4e6f8b201"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roskomnadzor_company_facts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("record_key", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("inn", sa.String(10), nullable=False),
        sa.Column("ogrn", sa.String(13)),
        sa.Column("external_number", sa.String(200), nullable=False),
        sa.Column("name", sa.Text()), sa.Column("status", sa.Text()),
        sa.Column("issued_at", sa.Date()), sa.Column("valid_until", sa.Date()),
        sa.Column("public_details", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["data_sets.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("dataset_id", "record_key", name="uq_rkn_company_fact"),
    )
    for column in ("dataset_id", "data_date", "channel", "inn", "ogrn", "external_number"):
        op.create_index(f"ix_roskomnadzor_company_facts_{column}", "roskomnadzor_company_facts", [column])
    op.create_table(
        "roskomnadzor_private_person_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False), sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("record_key", sa.String(64), nullable=False), sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("identifier_kind", sa.String(30), nullable=False), sa.Column("identifier_hash", sa.String(64), nullable=False),
        sa.Column("private_payload", postgresql.JSONB(), nullable=False), sa.Column("is_published", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["data_sets.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("dataset_id", "record_key", name="uq_rkn_private_person_record"),
    )
    for column in ("dataset_id", "data_date", "channel", "identifier_hash"):
        op.create_index(f"ix_roskomnadzor_private_person_records_{column}", "roskomnadzor_private_person_records", [column])
    op.create_table(
        "roskomnadzor_pd_operator_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True), sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("inn", sa.String(10), nullable=False), sa.Column("request_date", sa.Date(), nullable=False),
        sa.Column("result_status", sa.String(30), nullable=False), sa.Column("is_found", sa.Boolean()),
        sa.Column("record_count", sa.Integer()), sa.Column("public_records", postgresql.JSONB()),
        sa.Column("http_status", sa.Integer()), sa.Column("error_code", sa.String(120)), sa.Column("error_message", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["data_sets.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("dataset_id", "inn", "request_date", name="uq_rkn_pd_operator_check"),
    )
    for column in ("dataset_id", "inn", "request_date", "result_status", "is_found", "error_code", "checked_at"):
        op.create_index(f"ix_roskomnadzor_pd_operator_checks_{column}", "roskomnadzor_pd_operator_checks", [column])


def downgrade() -> None:
    op.drop_table("roskomnadzor_pd_operator_checks")
    op.drop_table("roskomnadzor_private_person_records")
    op.drop_table("roskomnadzor_company_facts")
