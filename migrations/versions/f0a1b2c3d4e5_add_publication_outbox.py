"""add durable public publication outbox

Revision ID: f0a1b2c3d4e5
Revises: e6f7a8b9c0d2
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f0a1b2c3d4e5"
down_revision = "e6f7a8b9c0d2"
branch_labels = None
depends_on = None


_STATUSES = (
    "PENDING", "COALESCED", "BUILDING", "READY", "UPLOADING",
    "IMPORTING", "VERIFYING", "PUBLISHED", "RETRY_SCHEDULED", "FAILED",
    "SUPERSEDED",
)

_INCIDENT_CATEGORIES = (
    "TEMPORARY_NETWORK", "HTTP_5XX", "TIMEOUT", "RATE_LIMIT", "DNS_FAILURE",
    "SOURCE_UNAVAILABLE", "SOURCE_STALE", "SOURCE_INVALID_ARTIFACT",
    "SOURCE_SCHEMA_VIOLATION", "SOURCE_ACCESS_REQUIRED", "CHECKSUM_MISMATCH",
    "RAW_STORAGE_ERROR", "DISK_PRESSURE", "PARSER_ERROR", "NORMALIZATION_ERROR",
    "MATCHING_ERROR", "PUBLICATION_ERROR", "DATABASE_UNAVAILABLE", "DATABASE_LOCK",
    "LEASE_STUCK", "WORKER_CRASH", "WORKER_NOT_RUNNING", "CONFIGURATION_ERROR",
    "CODE_REGRESSION", "PUBLIC_BUILD_ERROR", "PUBLIC_UPLOAD_ERROR",
    "PUBLIC_IMPORT_ERROR", "PUBLIC_VERIFY_ERROR", "PUBLIC_VPS_UNAVAILABLE", "UNKNOWN",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "public_projection_publications",
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("last_published_hash", sa.String(length=64), nullable=False),
        sa.Column("last_published_release_id", sa.String(length=120), nullable=False),
        sa.Column("last_enrichment_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["last_enrichment_run_id"], ["company_enrichment_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("company_id"),
    )
    op.create_index("ix_public_projection_publications_last_published_release_id", "public_projection_publications", ["last_published_release_id"])
    op.create_index("ix_public_projection_publications_last_enrichment_run_id", "public_projection_publications", ["last_enrichment_run_id"])

    op.create_table(
        "public_publication_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("trigger_type", sa.String(length=40), nullable=False),
        sa.Column("trigger_company_id", sa.BigInteger(), nullable=True),
        sa.Column("trigger_enrichment_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="PENDING", nullable=False),
        sa.Column("projection_generation", sa.String(length=120), nullable=True),
        sa.Column("projection_hash", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("candidate_release_id", sa.String(length=120), nullable=True),
        sa.Column("previous_release_id", sa.String(length=120), nullable=True),
        sa.Column("published_release_id", sa.String(length=120), nullable=True),
        sa.Column("changed_company_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("changed_company_ids", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("changed_company_inns", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("change_summary", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_main_sha", sa.String(length=40), nullable=False),
        sa.Column("coalesced_into_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("build_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN (" + ",".join(f"'{value}'" for value in _STATUSES) + ")", name="ck_public_publication_requests_status"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_public_publication_requests_attempts"),
        sa.CheckConstraint("changed_company_count >= 0", name="ck_public_publication_requests_changed_count"),
        sa.ForeignKeyConstraint(["trigger_company_id"], ["companies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trigger_enrichment_run_id"], ["company_enrichment_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["coalesced_into_id"], ["public_publication_requests.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_public_publication_requests_status_due", "public_publication_requests", ["status", "next_attempt_at", "created_at"])
    op.create_index("ix_public_publication_requests_trigger_company_id", "public_publication_requests", ["trigger_company_id"])
    op.create_index("ix_public_publication_requests_trigger_enrichment_run_id", "public_publication_requests", ["trigger_enrichment_run_id"], unique=True)
    op.create_index("ix_public_publication_requests_candidate_release_id", "public_publication_requests", ["candidate_release_id"])
    op.create_index("ix_public_publication_requests_published_release_id", "public_publication_requests", ["published_release_id"])
    op.create_index("ix_public_publication_requests_coalesced_into_id", "public_publication_requests", ["coalesced_into_id"])
    op.create_index("ix_public_publication_requests_next_attempt_at", "public_publication_requests", ["next_attempt_at"])

    op.drop_constraint("ck_source_incidents_category", "source_incidents", type_="check")
    op.create_check_constraint(
        "ck_source_incidents_category",
        "source_incidents",
        f"category IN ({_quoted(_INCIDENT_CATEGORIES)})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_source_incidents_category", "source_incidents", type_="check")
    legacy = tuple(value for value in _INCIDENT_CATEGORIES if not value.startswith("PUBLIC_"))
    op.create_check_constraint(
        "ck_source_incidents_category",
        "source_incidents",
        f"category IN ({_quoted(legacy)})",
    )
    op.drop_table("public_publication_requests")
    op.drop_table("public_projection_publications")
