"""expand W1-006 with NOPRIZ and private NRS storage

Revision ID: e7a8b9c0d1e2
Revises: d6f7a8b9c0d1
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "d6f7a8b9c0d1"
branch_labels = None
depends_on = None


def _check_table(name, unique_name):
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("inn", sa.String(10), nullable=False), sa.Column("request_date", sa.Date(), nullable=False),
        sa.Column("result_status", sa.String(30), nullable=False), sa.Column("is_found", sa.Boolean()),
        sa.Column("record_count", sa.Integer()), sa.Column("public_records", postgresql.JSONB()),
        sa.Column("http_status", sa.Integer()), sa.Column("error_code", sa.String(120)), sa.Column("error_message", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("inn", "request_date", name=unique_name),
    )
    for column in ("inn", "request_date", "result_status", "is_found", "error_code", "checked_at"):
        op.create_index(f"ix_{name}_{column}", name, [column])


def upgrade() -> None:
    _check_table("nopriz_member_checks", "uq_nopriz_member_check_inn_date")
    op.create_table(
        "sro_person_registry_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("source_code", sa.String(50), nullable=False), sa.Column("source_record_id", sa.String(100), nullable=False),
        sa.Column("official_registration_number", sa.String(100), nullable=False), sa.Column("person_name", sa.Text(), nullable=False),
        sa.Column("company_inn", sa.String(10)), sa.Column("relationship_status", sa.String(40), nullable=False),
        sa.Column("professional_status", postgresql.JSONB(), nullable=False), sa.Column("status_date", sa.Date()),
        sa.Column("valid_from", sa.Date()), sa.Column("valid_to", sa.Date()), sa.Column("match_confidence", sa.String(40), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False), sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("public_visibility", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("access_classification", sa.String(40), nullable=False), sa.Column("retention_policy", sa.String(120), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("source_code", "source_record_id", name="uq_sro_person_source_record"),
    )
    for column in ("source_code", "official_registration_number", "company_inn", "public_visibility", "checked_at"):
        op.create_index(f"ix_sro_person_registry_records_{column}", "sro_person_registry_records", [column])


def downgrade() -> None:
    op.drop_table("sro_person_registry_records")
    op.drop_table("nopriz_member_checks")
