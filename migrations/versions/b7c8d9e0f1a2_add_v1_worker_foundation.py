"""add isolated V1 worker foundation

Revision ID: b7c8d9e0f1a2
Revises: 3f7a9c2d5e61
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b7c8d9e0f1a2"
down_revision = "3f7a9c2d5e61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=120), nullable=False),
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column("handler_version", sa.String(length=80), nullable=False),
        sa.Column(
            "schedule_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=30), server_default="queued", nullable=False
        ),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column(
            "timeout_seconds", sa.Integer(), server_default="300", nullable=False
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("max_attempts > 0", name="ck_worker_jobs_max_attempts"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_scheduled', "
            "'succeeded', 'failed', 'cancelled')",
            name="ck_worker_jobs_status",
        ),
        sa.CheckConstraint("timeout_seconds > 0", name="ck_worker_jobs_timeout"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_worker_jobs_claim",
        "worker_jobs",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index("ix_worker_jobs_job_type", "worker_jobs", ["job_type"])
    op.create_index("ix_worker_jobs_source_id", "worker_jobs", ["source_id"])

    op.create_table(
        "worker_handler_registry",
        sa.Column("source_id", sa.String(length=120), nullable=False),
        sa.Column("handler_version", sa.String(length=80), nullable=False),
        sa.Column(
            "approved", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "live_mode", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("live_mode = false", name="ck_worker_handlers_not_live"),
        sa.PrimaryKeyConstraint("source_id", "handler_version"),
    )

    op.create_table(
        "worker_leases",
        sa.Column("source_id", sa.String(length=120), nullable=False),
        sa.Column("owner_worker_id", sa.String(length=200), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "fencing_token > 0", name="ck_worker_leases_fencing_token"
        ),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_index("ix_worker_leases_expiry", "worker_leases", ["expires_at"])
    op.create_index(
        "ix_worker_leases_owner_worker_id", "worker_leases", ["owner_worker_id"]
    )

    op.create_table(
        "worker_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status", sa.String(length=30), server_default="running", nullable=False
        ),
        sa.Column("worker_id", sa.String(length=200), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("handler_version", sa.String(length=80), nullable=False),
        sa.Column(
            "current_stage",
            sa.String(length=50),
            server_default="claimed",
            nullable=False,
        ),
        sa.Column(
            "errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "checksum_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "retryable", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.CheckConstraint("attempt_no > 0", name="ck_worker_runs_attempt"),
        sa.CheckConstraint(
            "fencing_token > 0", name="ck_worker_runs_fencing_token"
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_worker_runs_duration",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'timed_out', 'interrupted')",
            name="ck_worker_runs_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["worker_jobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_no", name="uq_worker_runs_attempt"),
    )
    op.create_index("ix_worker_runs_heartbeat_at", "worker_runs", ["heartbeat_at"])
    op.create_index("ix_worker_runs_job_id", "worker_runs", ["job_id"])
    op.create_index("ix_worker_runs_stale", "worker_runs", ["status", "heartbeat_at"])
    op.create_index("ix_worker_runs_started_at", "worker_runs", ["started_at"])
    op.create_index("ix_worker_runs_worker_id", "worker_runs", ["worker_id"])

    op.create_table(
        "worker_raw_manifests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_reference", sa.Text(), nullable=False),
        sa.Column(
            "checksum_algorithm",
            sa.String(length=30),
            server_default="sha256",
            nullable=False,
        ),
        sa.Column("checksum", sa.String(length=128), nullable=False),
        sa.Column(
            "manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "immutable", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("immutable = true", name="ck_worker_raw_manifest_immutable"),
        sa.ForeignKeyConstraint(["run_id"], ["worker_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "artifact_reference",
            name="uq_worker_raw_manifest_reference",
        ),
    )
    op.create_index(
        "ix_worker_raw_manifests_checksum", "worker_raw_manifests", ["checksum"]
    )
    op.create_index("ix_worker_raw_manifests_run_id", "worker_raw_manifests", ["run_id"])

    op.create_table(
        "worker_publication_state",
        sa.Column("source_id", sa.String(length=120), nullable=False),
        sa.Column("active_pointer", sa.Text(), nullable=True),
        sa.Column("rollback_pointer", sa.Text(), nullable=True),
        sa.Column("generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "last_fencing_token", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("published_by_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "validation_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_fencing_token >= 0", name="ck_worker_publication_fencing_token"
        ),
        sa.CheckConstraint(
            "generation >= 0", name="ck_worker_publication_generation"
        ),
        sa.ForeignKeyConstraint(
            ["published_by_run_id"], ["worker_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_index(
        "ix_worker_publication_state_published_by_run_id",
        "worker_publication_state",
        ["published_by_run_id"],
    )


def downgrade() -> None:
    op.drop_table("worker_publication_state")
    op.drop_table("worker_raw_manifests")
    op.drop_table("worker_runs")
    op.drop_table("worker_leases")
    op.drop_table("worker_handler_registry")
    op.drop_table("worker_jobs")
