"""add roszdrav W1-004 datasets

Revision ID: a9c4e6f8b201
Revises: f7b2d8a4c1e3
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a9c4e6f8b201"
down_revision: Union[str, Sequence[str], None] = "f7b2d8a4c1e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roszdrav_license_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("record_key", sa.String(64), nullable=False),
        sa.Column("category", sa.String(60), nullable=False),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("ogrn", sa.String(15)),
        sa.Column("license_number", sa.String(120), nullable=False),
        sa.Column("licensee_name", sa.Text()),
        sa.Column("authority_name", sa.Text()),
        sa.Column("activity_type", sa.Text()),
        sa.Column("legal_form", sa.Text()),
        sa.Column("address", sa.Text()),
        sa.Column("work_places", postgresql.JSONB(), nullable=False),
        sa.Column("decision_date", sa.Date()),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date", sa.Date()),
        sa.Column("termination_info", sa.Text()),
        sa.Column("termination_date", sa.Date()),
        sa.Column("suspension_info", sa.Text()),
        sa.Column("cancellation_info", sa.Text()),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["data_sets.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("dataset_id", "record_key", name="uq_roszdrav_license_record"),
    )
    for column in ("dataset_id", "data_date", "category", "inn", "ogrn", "license_number"):
        op.create_index(f"ix_roszdrav_license_entries_{column}", "roszdrav_license_entries", [column])

    op.create_table(
        "roszdrav_unified_license_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("request_date", sa.Date(), nullable=False),
        sa.Column("result_status", sa.String(30), nullable=False),
        sa.Column("is_found", sa.Boolean()),
        sa.Column("records", postgresql.JSONB()),
        sa.Column("http_status", sa.Integer()),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_message", sa.Text()),
        sa.Column("raw_payload", postgresql.JSONB()),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["data_sets.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("dataset_id", "inn", "request_date", name="uq_roszdrav_unified_license_check"),
    )
    for column in ("dataset_id", "inn", "request_date", "result_status", "is_found", "error_code", "checked_at"):
        op.create_index(f"ix_roszdrav_unified_license_checks_{column}", "roszdrav_unified_license_checks", [column])

    op.create_table(
        "roszdrav_medical_device_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("registration_number", sa.String(160), nullable=False),
        sa.Column("request_date", sa.Date(), nullable=False),
        sa.Column("result_status", sa.String(30), nullable=False),
        sa.Column("is_found", sa.Boolean()),
        sa.Column("total_results", sa.Integer()),
        sa.Column("records", postgresql.JSONB()),
        sa.Column("http_status", sa.Integer()),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_message", sa.Text()),
        sa.Column("raw_payload", postgresql.JSONB()),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["data_sets.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("dataset_id", "registration_number", "request_date", name="uq_roszdrav_medical_device_check"),
    )
    for column in ("dataset_id", "registration_number", "request_date", "result_status", "is_found", "error_code", "checked_at"):
        op.create_index(f"ix_roszdrav_medical_device_checks_{column}", "roszdrav_medical_device_checks", [column])

    op.create_table(
        "roszdrav_clinical_organization_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("record_key", sa.String(64), nullable=False),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("included_at", sa.Date()),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("address", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["data_sets.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("dataset_id", "record_key", name="uq_roszdrav_clinical_record"),
    )
    for column in ("dataset_id", "data_date", "inn"):
        op.create_index(f"ix_roszdrav_clinical_organization_entries_{column}", "roszdrav_clinical_organization_entries", [column])


def downgrade() -> None:
    op.drop_table("roszdrav_clinical_organization_entries")
    op.drop_table("roszdrav_medical_device_checks")
    op.drop_table("roszdrav_unified_license_checks")
    op.drop_table("roszdrav_license_entries")
