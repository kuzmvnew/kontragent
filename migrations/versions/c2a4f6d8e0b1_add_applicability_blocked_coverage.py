"""Add durable blocked company applicability coverage.

Revision ID: c2a4f6d8e0b1
Revises: b7d3e5f1a9c2
"""

from alembic import op


revision = "c2a4f6d8e0b1"
down_revision = "b7d3e5f1a9c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_company_source_coverage_status",
        "company_source_coverage",
        type_="check",
    )
    op.create_check_constraint(
        "ck_company_source_coverage_status",
        "company_source_coverage",
        "status IN ('PENDING','RUNNING','FOUND','NOT_FOUND','NOT_APPLICABLE',"
        "'APPLICABILITY_UNKNOWN','SOURCE_UNAVAILABLE','TIMEOUT','PARSING_ERROR',"
        "'STALE_DATA','ACCESS_REQUIRED')",
    )
    op.drop_constraint(
        "ck_company_source_coverage_execution_status",
        "company_source_coverage",
        type_="check",
    )
    op.create_check_constraint(
        "ck_company_source_coverage_execution_status",
        "company_source_coverage",
        "execution_status IN ('pending','queued','running','retry_scheduled',"
        "'blocked','succeeded','failed','cancelled')",
    )
    op.execute(
        """
        UPDATE company_source_coverage
        SET status = 'APPLICABILITY_UNKNOWN',
            execution_status = 'blocked',
            last_error = 'Source applicability is unresolved'
        WHERE source_snapshot->>'applicability' = 'UNKNOWN'
          AND worker_job_id IS NULL
          AND status = 'SOURCE_UNAVAILABLE'
          AND execution_status = 'failed'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE company_source_coverage
        SET status = 'SOURCE_UNAVAILABLE',
            execution_status = 'failed',
            last_error = 'Applicability blocker preserved during schema downgrade'
        WHERE status = 'APPLICABILITY_UNKNOWN'
           OR execution_status = 'blocked'
        """
    )
    op.execute(
        """
        UPDATE company_enrichment_runs AS r
        SET status = 'waiting_sources',
            stage = 'source_enrichment',
            public_ready = false,
            last_error_code = 'applicability_unknown',
            last_error = 'Applicability blocker preserved during schema downgrade',
            finished_at = NULL
        WHERE EXISTS (
          SELECT 1
          FROM company_source_coverage AS c
          WHERE c.enrichment_run_id = r.id
            AND c.source_snapshot->>'applicability' = 'UNKNOWN'
            AND c.worker_job_id IS NULL
        )
        """
    )
    op.drop_constraint(
        "ck_company_source_coverage_execution_status",
        "company_source_coverage",
        type_="check",
    )
    op.create_check_constraint(
        "ck_company_source_coverage_execution_status",
        "company_source_coverage",
        "execution_status IN ('pending','queued','running','retry_scheduled',"
        "'succeeded','failed','cancelled')",
    )
    op.drop_constraint(
        "ck_company_source_coverage_status",
        "company_source_coverage",
        type_="check",
    )
    op.create_check_constraint(
        "ck_company_source_coverage_status",
        "company_source_coverage",
        "status IN ('PENDING','RUNNING','FOUND','NOT_FOUND','NOT_APPLICABLE',"
        "'SOURCE_UNAVAILABLE','TIMEOUT','PARSING_ERROR','STALE_DATA',"
        "'ACCESS_REQUIRED')",
    )
