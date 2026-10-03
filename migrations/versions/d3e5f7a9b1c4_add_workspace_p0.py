"""add Workspace P0 customer identity and saved-company foundation

Revision ID: d3e5f7a9b1c4
Revises: c2a4f6d8e0b1
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d3e5f7a9b1c4"
down_revision = "c2a4f6d8e0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_customer_user_status"),
        sa.UniqueConstraint("email", name="uq_customer_users_email"),
    )
    op.create_index("ix_customer_users_email", "customer_users", ["email"])
    op.create_index("ix_customer_users_status", "customer_users", ["status"])

    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(250), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active','suspended','closed')", name="ck_workspace_status"),
    )
    op.create_index("ix_workspaces_status", "workspaces", ["status"])

    op.create_table(
        "workspace_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_key", sa.String(80), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "role_key", name="uq_workspace_role_key"),
    )
    op.create_index("ix_workspace_roles_workspace_id", "workspace_roles", ["workspace_id"])

    op.create_table(
        "workspace_role_capabilities",
        sa.Column("role_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspace_roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("capability_key", sa.String(100), primary_key=True),
    )

    op.create_table(
        "workspace_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customer_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspace_roles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('active','suspended','revoked')", name="ck_workspace_membership_status"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership"),
    )
    op.create_index("ix_workspace_memberships_workspace_id", "workspace_memberships", ["workspace_id"])
    op.create_index("ix_workspace_memberships_user_id", "workspace_memberships", ["user_id"])
    op.create_index("ix_workspace_memberships_role_id", "workspace_memberships", ["role_id"])
    op.create_index("ix_workspace_memberships_status", "workspace_memberships", ["status"])

    op.create_table(
        "customer_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customer_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("active_workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_customer_sessions_token_hash"),
    )
    op.create_index("ix_customer_sessions_token_hash", "customer_sessions", ["token_hash"])
    op.create_index("ix_customer_sessions_user_id", "customer_sessions", ["user_id"])
    op.create_index("ix_customer_sessions_active_workspace_id", "customer_sessions", ["active_workspace_id"])
    op.create_index("ix_customer_sessions_expires_at", "customer_sessions", ["expires_at"])
    op.create_index("ix_customer_sessions_revoked_at", "customer_sessions", ["revoked_at"])

    op.create_table(
        "workspace_entitlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capability_key", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("limit_value", sa.Integer(), nullable=True),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("limit_value IS NULL OR limit_value >= 0", name="ck_workspace_entitlement_limit"),
        sa.UniqueConstraint("workspace_id", "capability_key", name="uq_workspace_entitlement"),
    )
    op.create_index("ix_workspace_entitlements_workspace_id", "workspace_entitlements", ["workspace_id"])
    op.create_index("ix_workspace_entitlements_capability_key", "workspace_entitlements", ["capability_key"])

    op.create_table(
        "saved_companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("added_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("customer_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "company_id", name="uq_saved_company"),
    )
    op.create_index("ix_saved_companies_workspace_id", "saved_companies", ["workspace_id"])
    op.create_index("ix_saved_companies_company_id", "saved_companies", ["company_id"])
    op.create_index("ix_saved_companies_added_by", "saved_companies", ["added_by"])
    op.create_index("ix_saved_companies_created_at", "saved_companies", ["created_at"])

    op.create_table(
        "workspace_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customer_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(80), nullable=False),
        sa.Column("target_ref", sa.String(240), nullable=False),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_workspace_audit_events_workspace_id", "workspace_audit_events", ["workspace_id"])
    op.create_index("ix_workspace_audit_events_actor_user_id", "workspace_audit_events", ["actor_user_id"])
    op.create_index("ix_workspace_audit_events_action", "workspace_audit_events", ["action"])
    op.create_index("ix_workspace_audit_events_created_at", "workspace_audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("workspace_audit_events")
    op.drop_table("saved_companies")
    op.drop_table("workspace_entitlements")
    op.drop_table("customer_sessions")
    op.drop_table("workspace_memberships")
    op.drop_table("workspace_role_capabilities")
    op.drop_table("workspace_roles")
    op.drop_table("workspaces")
    op.drop_table("customer_users")
