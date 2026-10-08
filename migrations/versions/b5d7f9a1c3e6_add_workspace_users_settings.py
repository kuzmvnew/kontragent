"""add Workspace invitations and administration capabilities

Revision ID: b5d7f9a1c3e6
Revises: a4c6e8f0b2d4
Create Date: 2026-10-08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b5d7f9a1c3e6"
down_revision = "a4c6e8f0b2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "invited_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customer_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "accepted_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customer_users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ("workspace_id", "role_id"),
            ("workspace_roles.workspace_id", "workspace_roles.id"),
            name="fk_workspace_invitation_role_scope",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("token_hash", name="uq_workspace_invitations_token_hash"),
        sa.CheckConstraint(
            "status IN ('PENDING','ACCEPTED','REVOKED')",
            name="ck_workspace_invitation_status",
        ),
        sa.CheckConstraint(
            "email::text = lower(btrim(email::text))",
            name="ck_workspace_invitation_email_normalized",
        ),
        sa.CheckConstraint(
            "token_hash::text ~ '^[0-9a-f]{64}$'::text",
            name="ck_workspace_invitation_token_hash",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND accepted_at IS NULL AND accepted_by_user_id IS NULL "
            "AND revoked_at IS NULL) OR "
            "(status = 'ACCEPTED' AND accepted_at IS NOT NULL AND accepted_by_user_id IS NOT NULL "
            "AND revoked_at IS NULL) OR "
            "(status = 'REVOKED' AND accepted_at IS NULL AND accepted_by_user_id IS NULL "
            "AND revoked_at IS NOT NULL)",
            name="ck_workspace_invitation_lifecycle",
        ),
    )
    for column in (
        "workspace_id",
        "email",
        "role_id",
        "invited_by_user_id",
        "token_hash",
        "status",
        "expires_at",
        "accepted_by_user_id",
    ):
        op.create_index(
            f"ix_workspace_invitations_{column}",
            "workspace_invitations",
            [column],
        )
    op.create_index(
        "ix_workspace_invitations_workspace_created",
        "workspace_invitations",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "uq_workspace_invitation_pending_email",
        "workspace_invitations",
        ["workspace_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.execute(
        """
        INSERT INTO workspace_role_capabilities (role_id, capability_key)
        SELECT role.id, capability.capability_key
        FROM workspace_roles AS role
        CROSS JOIN (
            VALUES ('workspace.members.invite'), ('workspace.settings.manage')
        ) AS capability(capability_key)
        WHERE role.role_key IN ('OWNER', 'ADMIN')
        ON CONFLICT (role_id, capability_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM workspace_role_capabilities "
        "WHERE capability_key IN ('workspace.members.invite','workspace.settings.manage')"
    )
    op.drop_table("workspace_invitations")
