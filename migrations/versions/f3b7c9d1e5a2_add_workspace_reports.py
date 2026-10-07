"""add immutable Workspace reports and report product gates

Revision ID: f3b7c9d1e5a2
Revises: e4f6a8b0c2d3
Create Date: 2026-10-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f3b7c9d1e5a2"
down_revision = "e4f6a8b0c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            sa.BigInteger(),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "generated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customer_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("report_type", sa.String(80), nullable=False),
        sa.Column("schema_version", sa.String(80), nullable=False),
        sa.Column("subject_inn", sa.String(10), nullable=False),
        sa.Column("subject_name", sa.String(1000), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("company_view_contract_version", sa.String(80), nullable=False),
        sa.Column(
            "company_view_generated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("risk_ref", sa.String(240), nullable=True),
        sa.Column("summary_ref", sa.String(240), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "report_type IN ('COMPANY_CHECK_V1')",
            name="ck_workspace_report_type",
        ),
        sa.CheckConstraint(
            "schema_version IN ('workspace-report-v1')",
            name="ck_workspace_report_schema_version",
        ),
        sa.CheckConstraint(
            "subject_inn ~ '^[0-9]{10}$'",
            name="ck_workspace_report_subject_inn",
        ),
        sa.CheckConstraint(
            "snapshot_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_workspace_report_snapshot_sha256",
        ),
    )
    for column in (
        "workspace_id",
        "company_id",
        "generated_by_user_id",
        "subject_inn",
        "generated_at",
    ):
        op.create_index(
            f"ix_workspace_reports_{column}",
            "workspace_reports",
            [column],
        )
    op.create_index(
        "ix_workspace_reports_workspace_generated",
        "workspace_reports",
        ["workspace_id", "generated_at"],
    )
    op.create_index(
        "ix_workspace_reports_company_generated",
        "workspace_reports",
        ["company_id", "generated_at"],
    )

    op.execute(
        """
        CREATE FUNCTION prevent_workspace_report_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.company_id IS NULL
               AND OLD.company_id IS NOT NULL
               AND (to_jsonb(NEW) - 'company_id') =
                   (to_jsonb(OLD) - 'company_id') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'workspace reports are immutable'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_workspace_reports_immutable
        BEFORE UPDATE ON workspace_reports
        FOR EACH ROW EXECUTE FUNCTION prevent_workspace_report_update()
        """
    )

    op.execute(
        """
        INSERT INTO workspace_role_capabilities (role_id, capability_key)
        SELECT role.id, capability.capability_key
        FROM workspace_roles AS role
        CROSS JOIN (
            VALUES ('report.view'), ('report.generate'), ('report.export')
        ) AS capability(capability_key)
        ON CONFLICT (role_id, capability_key) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO workspace_entitlements (
            id, workspace_id, entitlement_key, enabled, limit_value,
            policy_version, updated_at
        )
        SELECT
            gen_random_uuid(), workspace.id, 'reports.enabled', TRUE, NULL,
            'workspace-reports-v1', now()
        FROM workspaces AS workspace
        ON CONFLICT (workspace_id, entitlement_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM workspace_role_capabilities "
        "WHERE capability_key IN ('report.view', 'report.generate', 'report.export')"
    )
    op.execute(
        "DELETE FROM workspace_entitlements "
        "WHERE entitlement_key = 'reports.enabled'"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_workspace_reports_immutable "
        "ON workspace_reports"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_workspace_report_update()")
    op.drop_table("workspace_reports")
