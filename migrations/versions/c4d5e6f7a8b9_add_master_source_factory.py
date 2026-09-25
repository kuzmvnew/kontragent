"""add master source factory state and GIRBO history

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("company_managers", sa.Column("source_dataset_id", sa.BigInteger()))
    op.add_column("company_managers", sa.Column("source_data_date", sa.Date()))
    op.add_column("company_managers", sa.Column("source_record_key", sa.String(200)))
    op.add_column("company_managers", sa.Column("observed_at", sa.DateTime(timezone=True)))
    op.create_foreign_key("fk_company_managers_source_dataset", "company_managers", "data_sets", ["source_dataset_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_company_managers_source_dataset_id", "company_managers", ["source_dataset_id"])

    op.create_table(
        "registry_source_checkpoints",
        sa.Column("source_id", sa.String(120), primary_key=True),
        sa.Column("format_version", sa.String(20), nullable=False),
        sa.Column("baseline_accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_full_date", sa.Date()),
        sa.Column("last_delta_date", sa.Date()),
        sa.Column("last_release_identity", sa.String(200)),
        sa.Column("cursor", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "company_registry_changes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", sa.String(120), nullable=False),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("worker_runs.id", ondelete="SET NULL")),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("changed_fields", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("source_data_date", sa.Date(), nullable=False),
        sa.Column("source_record_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("event_type IN ('created','identity_changed','status_changed','address_changed','leader_changed','okved_changed','terminated')", name="ck_company_registry_change_event_type"),
        sa.UniqueConstraint("source_id", "source_record_key", "event_type", name="uq_company_registry_change_source_record_event"),
    )
    for column in ("source_id", "company_id", "run_id", "inn", "event_type", "source_data_date"):
        op.create_index(f"ix_company_registry_changes_{column}", "company_registry_changes", [column])
    op.create_table(
        "master_replay_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_source_id", sa.String(120), nullable=False),
        sa.Column("registry_change_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("company_registry_changes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.Text()),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('pending','scheduled','complete','failed')", name="ck_master_replay_signal_status"),
        sa.UniqueConstraint("company_id", "target_source_id", "registry_change_id", name="uq_master_replay_signal_company_source_change"),
    )
    for column in ("company_id", "target_source_id", "status"):
        op.create_index(f"ix_master_replay_signals_{column}", "master_replay_signals", [column])
    op.create_table(
        "girbo_accounting_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("report_id", sa.String(160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reporting_year", sa.Integer(), nullable=False),
        sa.Column("publication_date", sa.Date()),
        sa.Column("correction_date", sa.Date()),
        sa.Column("source_data_date", sa.Date(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("revenue", sa.Numeric(24, 2)),
        sa.Column("expenses", sa.Numeric(24, 2)),
        sa.Column("profit_loss", sa.Numeric(24, 2)),
        sa.Column("assets", sa.Numeric(24, 2)),
        sa.Column("liabilities", sa.Numeric(24, 2)),
        sa.Column("equity", sa.Numeric(24, 2)),
        sa.Column("statement_values", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("raw_checksum", sa.String(64), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dataset_id", "report_id", "revision", name="uq_girbo_report_revision"),
    )
    for column in ("dataset_id", "company_id", "report_id", "reporting_year", "source_data_date", "is_current", "raw_checksum"):
        op.create_index(f"ix_girbo_accounting_reports_{column}", "girbo_accounting_reports", [column])


def downgrade() -> None:
    op.drop_table("girbo_accounting_reports")
    op.drop_table("master_replay_signals")
    op.drop_table("company_registry_changes")
    op.drop_table("registry_source_checkpoints")
    op.drop_index("ix_company_managers_source_dataset_id", table_name="company_managers")
    op.drop_constraint("fk_company_managers_source_dataset", "company_managers", type_="foreignkey")
    for column in ("observed_at", "source_record_key", "source_data_date", "source_dataset_id"):
        op.drop_column("company_managers", column)
