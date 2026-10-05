"""add Monitoring P0 subscriptions, snapshots, events and workspace feed

Revision ID: e4f6a8b0c2d3
Revises: d3e5f7a9b1c4
Create Date: 2026-10-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e4f6a8b0c2d3"
down_revision = "d3e5f7a9b1c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_monitoring_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            sa.BigInteger(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("company_view_revision", sa.String(80), nullable=True),
        sa.Column("risk_ref", sa.String(80), nullable=True),
        sa.Column("summary_ref", sa.String(80), nullable=True),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column("last_known_business_facts", postgresql.JSONB(), nullable=False),
        sa.Column("privacy_blocked_coordinates", postgresql.JSONB(), nullable=False),
        sa.Column("fact_count", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_company_monitoring_snapshots_company_id",
        "company_monitoring_snapshots",
        ["company_id"],
    )
    op.create_index(
        "ix_company_monitoring_snapshots_fingerprint",
        "company_monitoring_snapshots",
        ["fingerprint"],
    )
    op.create_index(
        "ix_company_monitoring_snapshots_company_captured",
        "company_monitoring_snapshots",
        ["company_id", "captured_at"],
    )
    op.create_index(
        "ix_company_monitoring_snapshots_company_revision",
        "company_monitoring_snapshots",
        ["company_id", "company_view_revision"],
    )

    op.create_table(
        "monitoring_subscriptions",
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
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customer_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "baseline_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("company_monitoring_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','PAUSED')",
            name="ck_monitoring_subscription_status",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "company_id",
            name="uq_monitoring_subscription_workspace_company",
        ),
        sa.UniqueConstraint(
            "id",
            "workspace_id",
            name="uq_monitoring_subscription_workspace_scope",
        ),
    )
    for column in (
        "workspace_id",
        "company_id",
        "created_by_user_id",
        "status",
        "baseline_snapshot_id",
    ):
        op.create_index(
            f"ix_monitoring_subscriptions_{column}",
            "monitoring_subscriptions",
            [column],
        )
    op.create_index(
        "ix_monitoring_subscriptions_workspace_status",
        "monitoring_subscriptions",
        ["workspace_id", "status"],
    )
    op.create_index(
        "ix_monitoring_subscriptions_company_status",
        "monitoring_subscriptions",
        ["company_id", "status"],
    )

    op.create_table(
        "monitoring_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_ref", sa.String(42), nullable=False),
        sa.Column(
            "company_id",
            sa.BigInteger(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fact_ref", sa.String(41), nullable=True),
        sa.Column("origin", sa.String(30), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("change_kind", sa.String(30), nullable=False),
        sa.Column("section_key", sa.String(80), nullable=False),
        sa.Column("field_key", sa.String(120), nullable=False),
        sa.Column("period_identity", sa.String(80), server_default="", nullable=False),
        sa.Column("item_identity", sa.String(300), server_default="", nullable=False),
        sa.Column("source_code", sa.String(100), nullable=True),
        sa.Column("old_value", postgresql.JSONB(), nullable=True),
        sa.Column("new_value", postgresql.JSONB(), nullable=True),
        sa.Column("old_state", sa.String(40), nullable=True),
        sa.Column("new_state", sa.String(40), nullable=True),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("severity_policy_version", sa.String(80), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_as_of", sa.Date(), nullable=True),
        sa.Column("evidence_refs", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("user_visible", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "origin IN ('SOURCE_CHANGE','RULESET_CHANGE','COVERAGE_CHANGE','DEAL_CONTEXT_CHANGE')",
            name="ck_monitoring_event_origin",
        ),
        sa.CheckConstraint(
            "change_kind IN ('FACT_ADDED','FACT_CHANGED','FACT_REMOVED','STATE_CHANGED')",
            name="ck_monitoring_event_change_kind",
        ),
        sa.CheckConstraint(
            "severity IN ('INFO','LOW','MEDIUM','HIGH')",
            name="ck_monitoring_event_severity",
        ),
        sa.UniqueConstraint("event_ref", name="uq_monitoring_events_event_ref"),
        sa.UniqueConstraint("dedupe_key", name="uq_monitoring_events_dedupe_key"),
    )
    for column in (
        "company_id",
        "origin",
        "event_type",
        "severity",
        "detected_at",
        "user_visible",
    ):
        op.create_index(
            f"ix_monitoring_events_{column}",
            "monitoring_events",
            [column],
        )
    op.create_index(
        "ix_monitoring_events_company_detected",
        "monitoring_events",
        ["company_id", "detected_at"],
    )

    op.create_table(
        "workspace_feed_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("monitoring_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "subscription_id",
            "event_id",
            name="uq_workspace_feed_subscription_event",
        ),
        sa.ForeignKeyConstraint(
            ("subscription_id", "workspace_id"),
            ("monitoring_subscriptions.id", "monitoring_subscriptions.workspace_id"),
            name="fk_workspace_feed_subscription_scope",
            ondelete="CASCADE",
        ),
    )
    for column in ("workspace_id", "subscription_id", "event_id"):
        op.create_index(
            f"ix_workspace_feed_entries_{column}",
            "workspace_feed_entries",
            [column],
        )
    op.create_index(
        "ix_workspace_feed_workspace_created",
        "workspace_feed_entries",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_workspace_feed_workspace_unread",
        "workspace_feed_entries",
        ["workspace_id", "created_at"],
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.execute(
        """
        CREATE FUNCTION reject_monitoring_history_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Monitoring history content is immutable: %', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table in ("company_monitoring_snapshots", "monitoring_events"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable "
            f"BEFORE UPDATE ON {table} FOR EACH ROW "
            "EXECUTE FUNCTION reject_monitoring_history_update()"
        )


def downgrade() -> None:
    for table in ("monitoring_events", "company_monitoring_snapshots"):
        op.execute(f"DROP TRIGGER trg_{table}_immutable ON {table}")
    op.execute("DROP FUNCTION reject_monitoring_history_update()")
    op.drop_table("workspace_feed_entries")
    op.drop_table("monitoring_events")
    op.drop_table("monitoring_subscriptions")
    op.drop_table("company_monitoring_snapshots")
