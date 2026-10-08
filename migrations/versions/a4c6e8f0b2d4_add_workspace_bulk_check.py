"""add durable Workspace Bulk Check

Revision ID: a4c6e8f0b2d4
Revises: f3b7c9d1e5a2
Create Date: 2026-10-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a4c6e8f0b2d4"
down_revision = "f3b7c9d1e5a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_bulk_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customer_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("schema_version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("original_filename", sa.String(240), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("public_release_id", sa.String(120), nullable=True),
        sa.Column("public_schema_version", sa.String(80), nullable=True),
        sa.Column("public_result_date", sa.Date(), nullable=True),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unique_valid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ready_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("not_resolved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("not_ready_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancelled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.UniqueConstraint("workspace_id", "id", name="uq_workspace_bulk_job_scope"),
        sa.CheckConstraint("schema_version = 'workspace-bulk-check-v1'", name="ck_workspace_bulk_job_schema_version"),
        sa.CheckConstraint("status IN ('READY','RUNNING','COMPLETED','COMPLETED_WITH_ERRORS','CANCELLED','FAILED')", name="ck_workspace_bulk_job_status"),
        sa.CheckConstraint("input_sha256 ~ '^[0-9a-f]{64}$'", name="ck_workspace_bulk_job_input_sha256"),
        sa.CheckConstraint(
            "total_rows >= 0 AND unique_valid_count >= 0 AND invalid_count >= 0 AND duplicate_count >= 0 "
            "AND processed_count >= 0 AND ready_count >= 0 AND not_resolved_count >= 0 "
            "AND not_ready_count >= 0 AND failed_count >= 0 AND cancelled_count >= 0",
            name="ck_workspace_bulk_job_nonnegative_counts",
        ),
    )
    for column in ("workspace_id", "created_by_user_id", "status", "public_release_id", "created_at"):
        op.create_index(f"ix_workspace_bulk_jobs_{column}", "workspace_bulk_jobs", [column])
    op.create_index("ix_workspace_bulk_jobs_workspace_created", "workspace_bulk_jobs", ["workspace_id", "created_at"])
    op.create_index("ix_workspace_bulk_jobs_workspace_status", "workspace_bulk_jobs", ["workspace_id", "status"])

    op.create_table(
        "workspace_bulk_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw_inn", sa.Text(), nullable=False),
        sa.Column("normalized_inn", sa.String(32), nullable=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("duplicate_of_row", sa.Integer(), nullable=True),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("result_payload", postgresql.JSONB(), nullable=True),
        sa.Column("result_sha256", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ("workspace_id", "job_id"),
            ("workspace_bulk_jobs.workspace_id", "workspace_bulk_jobs.id"),
            name="fk_workspace_bulk_item_job_scope",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("job_id", "row_number", name="uq_workspace_bulk_item_row"),
        sa.CheckConstraint("row_number > 0", name="ck_workspace_bulk_item_row_number"),
        sa.CheckConstraint("status IN ('PENDING','INVALID_INN','DUPLICATE','READY','NOT_RESOLVED','NOT_READY','PROCESSING_ERROR','CANCELLED')", name="ck_workspace_bulk_item_status"),
        sa.CheckConstraint("result_sha256 IS NULL OR result_sha256 ~ '^[0-9a-f]{64}$'", name="ck_workspace_bulk_item_result_sha256"),
        sa.CheckConstraint(
            "status::text = 'READY'::text AND result_payload IS NOT NULL AND result_sha256 IS NOT NULL "
            "OR status::text <> 'READY'::text AND result_payload IS NULL AND result_sha256 IS NULL",
            name="ck_workspace_bulk_item_result_state",
        ),
        sa.CheckConstraint("duplicate_of_row IS NULL OR duplicate_of_row < row_number", name="ck_workspace_bulk_item_duplicate_row"),
    )
    op.create_index("ix_workspace_bulk_items_job_row", "workspace_bulk_items", ["job_id", "row_number"])
    op.create_index("ix_workspace_bulk_items_workspace_job", "workspace_bulk_items", ["workspace_id", "job_id"])
    op.create_index("ix_workspace_bulk_items_job_status", "workspace_bulk_items", ["job_id", "status"])
    op.create_index("ix_workspace_bulk_items_normalized_inn", "workspace_bulk_items", ["normalized_inn"])
    op.create_index("ix_workspace_bulk_items_company_id", "workspace_bulk_items", ["company_id"])

    op.execute(
        """
        INSERT INTO workspace_role_capabilities (role_id, capability_key)
        SELECT role.id, capability.capability_key
        FROM workspace_roles AS role
        CROSS JOIN (VALUES ('bulk.view'), ('bulk.create'), ('bulk.manage'), ('bulk.export')) AS capability(capability_key)
        ON CONFLICT (role_id, capability_key) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO workspace_entitlements (
            id, workspace_id, entitlement_key, enabled, limit_value, policy_version, updated_at
        )
        SELECT gen_random_uuid(), workspace.id, 'bulk_check.enabled', TRUE, 1000,
               'workspace-bulk-check-v1', now()
        FROM workspaces AS workspace
        ON CONFLICT (workspace_id, entitlement_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM workspace_role_capabilities WHERE capability_key IN ('bulk.view','bulk.create','bulk.manage','bulk.export')")
    op.execute("DELETE FROM workspace_entitlements WHERE entitlement_key = 'bulk_check.enabled'")
    op.drop_table("workspace_bulk_items")
    op.drop_table("workspace_bulk_jobs")
