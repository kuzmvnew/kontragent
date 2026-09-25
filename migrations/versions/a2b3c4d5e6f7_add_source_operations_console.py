"""add source operations console metadata

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_change_summaries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=120), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("matched_companies", sa.BigInteger(), nullable=True),
        sa.Column("new_facts", sa.BigInteger(), nullable=True),
        sa.Column("changed_facts", sa.BigInteger(), nullable=True),
        sa.Column("removed_or_expired_facts", sa.BigInteger(), nullable=True),
        sa.Column("unchanged_facts", sa.BigInteger(), nullable=True),
        sa.Column("replayed_facts", sa.BigInteger(), nullable=True),
        sa.Column("quarantined_records", sa.BigInteger(), nullable=True),
        sa.Column("source_records", sa.BigInteger(), nullable=True),
        sa.Column("source_data_date", sa.Date(), nullable=True),
        sa.Column("previous_source_data_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_source_change_summaries_created_at", "source_change_summaries", ["created_at"])
    op.create_index("ix_source_change_summaries_run_id", "source_change_summaries", ["run_id"])
    op.create_index("ix_source_change_summaries_source_id", "source_change_summaries", ["source_id"])

    op.create_table(
        "admin_action_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("source_id", sa.String(length=120), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("previous_state", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("new_state", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=80), server_default=sa.text("'local_owner'"), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_action_audit_action", "admin_action_audit", ["action"])
    op.create_index("ix_admin_action_audit_job_id", "admin_action_audit", ["job_id"])
    op.create_index("ix_admin_action_audit_result", "admin_action_audit", ["result"])
    op.create_index("ix_admin_action_audit_source_id", "admin_action_audit", ["source_id"])
    op.create_index("ix_admin_action_audit_timestamp", "admin_action_audit", ["timestamp"])


def downgrade() -> None:
    op.drop_table("admin_action_audit")
    op.drop_table("source_change_summaries")
