"""add isolated NOSTROY W1-006 on-demand cache

Revision ID: d6f7a8b9c0d1
Revises: c5e1a2b3d4f5
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "c5e1a2b3d4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nostroy_member_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("inn", sa.String(10), nullable=False),
        sa.Column("request_date", sa.Date(), nullable=False),
        sa.Column("result_status", sa.String(30), nullable=False),
        sa.Column("is_found", sa.Boolean()),
        sa.Column("record_count", sa.Integer()),
        sa.Column("public_records", postgresql.JSONB()),
        sa.Column("http_status", sa.Integer()),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_message", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("inn", "request_date", name="uq_nostroy_member_check_inn_date"),
    )
    for column in ("inn", "request_date", "result_status", "is_found", "error_code", "checked_at"):
        op.create_index(f"ix_nostroy_member_checks_{column}", "nostroy_member_checks", [column])


def downgrade() -> None:
    op.drop_table("nostroy_member_checks")
