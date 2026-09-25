"""add restart-safe Firmoteka mass crawl and Master provenance

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def _index(table: str, *columns: str) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    op.add_column("companies", sa.Column("master_authority", sa.String(30)))
    op.add_column("companies", sa.Column("master_source", sa.String(100)))
    op.add_column(
        "companies",
        sa.Column(
            "official_registry_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("companies", sa.Column("master_provenance", postgresql.JSONB()))
    op.create_index("ix_companies_master_authority", "companies", ["master_authority"])
    op.create_index("ix_companies_master_source", "companies", ["master_source"])
    op.add_column(
        "company_managers",
        sa.Column(
            "official_registry_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.create_table(
        "firmoteka_crawl_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_kind", sa.String(30), nullable=False, server_default="initial"),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("phase", sa.String(30), nullable=False, server_default="discovery"),
        sa.Column("cursor", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("request_delay_seconds", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("request_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("catalog_discovered", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("company_discovered", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("fetched_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("parsed_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("valid_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("new_master_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_master_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("legal_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ip_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("quarantine_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("captcha_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("raw_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("http_status_counts", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_request_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_checkpoint_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('running','paused','completed','failed')", name="ck_firmoteka_crawl_run_status"),
        sa.CheckConstraint("phase IN ('discovery','catalog','companies','daily_refresh','complete')", name="ck_firmoteka_crawl_run_phase"),
        sa.CheckConstraint("request_delay_seconds >= 4", name="ck_firmoteka_delay"),
        sa.CheckConstraint("concurrency = 1", name="ck_firmoteka_concurrency"),
    )
    _index("firmoteka_crawl_runs", "status", "phase")

    op.create_table(
        "firmoteka_catalog_pages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("crawl_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("firmoteka_crawl_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discovered_companies", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('pending','running','succeeded','failed')", name="ck_firmoteka_catalog_status"),
        sa.UniqueConstraint("crawl_run_id", "url_hash", name="uq_firmoteka_catalog_run_url"),
    )
    _index("firmoteka_catalog_pages", "crawl_run_id", "position", "url_hash", "status")

    op.create_table(
        "firmoteka_crawl_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("crawl_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("firmoteka_crawl_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("discovered_from", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("raw_sha256", sa.String(64)),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('pending','running','succeeded','quarantined','failed')", name="ck_firmoteka_item_status"),
        sa.UniqueConstraint("crawl_run_id", "inn", name="uq_firmoteka_crawl_run_inn"),
    )
    _index("firmoteka_crawl_items", "crawl_run_id", "position", "inn", "status", "raw_sha256", "content_hash")

    op.create_table(
        "firmoteka_raw_artifacts",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("artifact_kind", sa.String(30), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(120)),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("response_headers", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("parser_version", sa.String(80), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    _index("firmoteka_raw_artifacts", "artifact_kind", "retrieved_at")

    op.create_table(
        "firmoteka_company_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="SET NULL")),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("ogrn", sa.String(15)),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_as_of", sa.Date()),
        sa.Column("raw_sha256", sa.String(64), sa.ForeignKey("firmoteka_raw_artifacts.sha256", ondelete="RESTRICT"), nullable=False),
        sa.Column("normalized_sha256", sa.String(64), nullable=False),
        sa.Column("normalized_path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("projection", postgresql.JSONB(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("inn", "content_hash", name="uq_firmoteka_snapshot_content"),
    )
    _index("firmoteka_company_snapshots", "dataset_id", "company_id", "inn", "ogrn", "retrieved_at", "source_as_of", "raw_sha256", "normalized_sha256", "content_hash", "is_current")

    op.create_table(
        "firmoteka_quarantine_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("crawl_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("firmoteka_crawl_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_inn", sa.String(12)),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(120), nullable=False),
        sa.Column("raw_sha256", sa.String(64)),
        sa.Column("safe_details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("crawl_run_id", "source_url", "reason", name="uq_firmoteka_quarantine_evidence"),
    )
    _index("firmoteka_quarantine_records", "crawl_run_id", "requested_inn", "reason", "raw_sha256")


def downgrade() -> None:
    op.drop_table("firmoteka_quarantine_records")
    op.drop_table("firmoteka_company_snapshots")
    op.drop_table("firmoteka_raw_artifacts")
    op.drop_table("firmoteka_crawl_items")
    op.drop_table("firmoteka_catalog_pages")
    op.drop_table("firmoteka_crawl_runs")
    op.drop_column("company_managers", "official_registry_verified")
    op.drop_index("ix_companies_master_source", table_name="companies")
    op.drop_index("ix_companies_master_authority", table_name="companies")
    op.drop_column("companies", "master_provenance")
    op.drop_column("companies", "official_registry_verified")
    op.drop_column("companies", "master_source")
    op.drop_column("companies", "master_authority")
