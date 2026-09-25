"""add incident autopilot persistence

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b3c4d5e6f7a8"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_automation_policies",
        sa.Column("source_id", sa.String(120), primary_key=True),
        sa.Column("dataset_code", sa.String(120), nullable=False, unique=True),
        sa.Column("auto_heal_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("auto_code_repair_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("remediation_level", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default=sa.text("3600")),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("remediation_level BETWEEN 0 AND 3", name="ck_source_automation_level"),
        sa.CheckConstraint("max_attempts BETWEEN 1 AND 10", name="ck_source_automation_attempts"),
        sa.CheckConstraint("cooldown_seconds IN (60, 300, 900, 3600, 21600, 86400)", name="ck_source_automation_cooldown"),
    )
    op.create_table(
        "source_incidents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_code", sa.String(40), nullable=False, unique=True),
        sa.Column("source_id", sa.String(120), nullable=False),
        sa.Column("dataset_code", sa.String(120), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("owner_domain", sa.String(40), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.Column("failure_fingerprint", sa.String(64), nullable=False),
        sa.Column("dedup_key", sa.String(255), nullable=False),
        sa.Column("auto_heal_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("auto_code_repair_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("remediation_level", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_action", sa.String(80), nullable=True),
        sa.Column("last_action_result", sa.String(40), nullable=True),
        sa.Column("repair_branch", sa.String(255), nullable=True),
        sa.Column("repair_pr_number", sa.Integer(), nullable=True),
        sa.Column("repair_commit_sha", sa.String(64), nullable=True),
        sa.Column("repair_ci_status", sa.String(40), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolution_evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["worker_jobs.id"], ondelete="SET NULL"),
        sa.CheckConstraint("category IN ('TEMPORARY_NETWORK','HTTP_5XX','TIMEOUT','RATE_LIMIT','DNS_FAILURE','SOURCE_UNAVAILABLE','SOURCE_STALE','SOURCE_INVALID_ARTIFACT','SOURCE_SCHEMA_VIOLATION','SOURCE_ACCESS_REQUIRED','CHECKSUM_MISMATCH','RAW_STORAGE_ERROR','DISK_PRESSURE','PARSER_ERROR','NORMALIZATION_ERROR','MATCHING_ERROR','PUBLICATION_ERROR','DATABASE_UNAVAILABLE','DATABASE_LOCK','LEASE_STUCK','WORKER_CRASH','WORKER_NOT_RUNNING','CONFIGURATION_ERROR','CODE_REGRESSION','UNKNOWN')", name="ck_source_incidents_category"),
        sa.CheckConstraint("owner_domain IN ('SOURCE_OWNED','OUR_INFRASTRUCTURE','OUR_CODE','ACCESS_REQUIRED','UNKNOWN')", name="ck_source_incidents_owner_domain"),
        sa.CheckConstraint("status IN ('OPEN','RETRY_SCHEDULED','AUTO_HEAL_RUNNING','WAITING_SOURCE','AWAITING_ENGINEERING_REVIEW','REVIEW_REQUIRED','AGENT_UNAVAILABLE','AUTO_REPAIR_EXHAUSTED','RESOLVED','CANCELLED')", name="ck_source_incidents_status"),
        sa.CheckConstraint("remediation_level BETWEEN 0 AND 3", name="ck_source_incidents_level"),
        sa.CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_source_incidents_attempts"),
    )
    for column in ("incident_code", "source_id", "dataset_code", "category", "owner_domain", "status", "failure_fingerprint", "dedup_key", "next_attempt_at"):
        op.create_index(f"ix_source_incidents_{column}", "source_incidents", [column])
    op.create_index(
        "uq_source_incidents_active_dedup", "source_incidents", ["dedup_key"], unique=True,
        postgresql_where=sa.text("status NOT IN ('RESOLVED', 'CANCELLED')"),
    )
    op.create_index(
        "uq_source_incidents_active_engineering_source", "source_incidents", ["source_id"], unique=True,
        postgresql_where=sa.text("status IN ('AWAITING_ENGINEERING_REVIEW', 'REVIEW_REQUIRED', 'AGENT_UNAVAILABLE') AND repair_branch IS NOT NULL"),
    )
    op.create_table(
        "source_incident_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.String(40), nullable=False),
        sa.Column("safe_message", sa.Text(), nullable=True),
        sa.Column("metadata_safe", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(["incident_id"], ["source_incidents.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_source_incident_actions_incident_id", "source_incident_actions", ["incident_id"])
    op.create_index("ix_source_incident_actions_action_type", "source_incident_actions", ["action_type"])


def downgrade() -> None:
    op.drop_table("source_incident_actions")
    op.drop_table("source_incidents")
    op.drop_table("source_automation_policies")
