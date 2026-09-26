"""add canonical company enrichment workflow

Revision ID: 9b7d4c2a1e80
Revises: d5e6f7a8b9c0
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "9b7d4c2a1e80"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_enrichment_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            sa.BigInteger(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column(
            "workflow_version",
            sa.String(40),
            nullable=False,
            server_default="company-enrichment-v1",
        ),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("stage", sa.String(40), nullable=False, server_default="planning"),
        sa.Column(
            "applicable_sources",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "completed_source_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "failed_source_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("restart_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_restarts", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("risk_assessment_id", sa.String(36)),
        sa.Column("summary_id", sa.String(36)),
        sa.Column(
            "public_ready", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("last_error_code", sa.String(120)),
        sa.Column("last_error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending','waiting_sources','retry_scheduled',"
            "'running','succeeded','failed','cancelled')",
            name="ck_company_enrichment_runs_status",
        ),
        sa.CheckConstraint(
            "stage IN ('planning','source_enrichment','risk','summary',"
            "'complete','failed')",
            name="ck_company_enrichment_runs_stage",
        ),
        sa.CheckConstraint(
            "source_count >= 0 AND completed_source_count >= 0 "
            "AND failed_source_count >= 0",
            name="ck_company_enrichment_runs_counts",
        ),
        sa.CheckConstraint(
            "(completed_source_count + failed_source_count) <= source_count",
            name="ck_company_enrichment_runs_count_bounds",
        ),
        sa.CheckConstraint(
            "restart_count >= 0 AND max_restarts >= 0 "
            "AND restart_count <= max_restarts",
            name="ck_company_enrichment_runs_restarts",
        ),
    )
    op.create_index(
        "ix_company_enrichment_runs_company_id",
        "company_enrichment_runs",
        ["company_id"],
    )
    op.create_index(
        "ix_company_enrichment_runs_trigger",
        "company_enrichment_runs",
        ["trigger"],
    )
    op.create_index(
        "ix_company_enrichment_runs_risk_assessment_id",
        "company_enrichment_runs",
        ["risk_assessment_id"],
    )
    op.create_index(
        "ix_company_enrichment_runs_summary_id",
        "company_enrichment_runs",
        ["summary_id"],
    )
    op.create_index(
        "ix_company_enrichment_runs_company_created",
        "company_enrichment_runs",
        ["company_id", "created_at"],
    )
    op.create_index(
        "ix_company_enrichment_runs_status_updated",
        "company_enrichment_runs",
        ["status", "updated_at"],
    )

    op.create_table(
        "company_source_coverage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "enrichment_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("company_enrichment_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            sa.BigInteger(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            sa.BigInteger(),
            sa.ForeignKey("data_sets.id", ondelete="SET NULL"),
        ),
        sa.Column("source_id", sa.String(120), nullable=False),
        sa.Column("worker_source_id", sa.String(120), nullable=False),
        sa.Column("mode", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column(
            "source_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("handler_version", sa.String(80), nullable=False),
        sa.Column(
            "worker_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("worker_jobs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "worker_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("worker_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("publication_generation", sa.BigInteger()),
        sa.Column("source_data_date", sa.Date()),
        sa.Column("replay_pointer", sa.Text()),
        sa.Column("replay_checksum", sa.String(128)),
        sa.Column(
            "master_replay_signal_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "enrichment_run_id",
            "source_id",
            name="uq_company_source_coverage_run_source",
        ),
        sa.CheckConstraint(
            "mode IN ('local_bulk_replay','point_check')",
            name="ck_company_source_coverage_mode",
        ),
        sa.CheckConstraint(
            "status IN ('pending','queued','running','retry_scheduled',"
            "'succeeded','failed','cancelled')",
            name="ck_company_source_coverage_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_company_source_coverage_attempts",
        ),
    )
    for column in (
        "enrichment_run_id",
        "company_id",
        "dataset_id",
        "source_id",
        "worker_source_id",
        "worker_job_id",
        "worker_run_id",
    ):
        op.create_index(
            f"ix_company_source_coverage_{column}",
            "company_source_coverage",
            [column],
        )
    op.create_index(
        "ix_company_source_coverage_company_source",
        "company_source_coverage",
        ["company_id", "source_id"],
    )
    op.create_index(
        "ix_company_source_coverage_status_updated",
        "company_source_coverage",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_table("company_source_coverage")
    op.drop_table("company_enrichment_runs")
