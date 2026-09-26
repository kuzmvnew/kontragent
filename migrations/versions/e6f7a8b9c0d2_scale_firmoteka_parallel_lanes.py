"""scale Firmoteka crawl with durable parallel lane state

Revision ID: e6f7a8b9c0d2
Revises: 9b7d4c2a1e80
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e6f7a8b9c0d2"
down_revision = "9b7d4c2a1e80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_firmoteka_concurrency", "firmoteka_crawl_runs", type_="check"
    )
    op.add_column(
        "firmoteka_crawl_runs",
        sa.Column(
            "catalog_concurrency", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "firmoteka_crawl_runs",
        sa.Column(
            "company_concurrency", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "firmoteka_crawl_runs",
        sa.Column(
            "backpressure_threshold",
            sa.Integer(),
            nullable=False,
            server_default="2000",
        ),
    )
    op.add_column(
        "firmoteka_crawl_runs",
        sa.Column(
            "daily_refresh_horizon_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )
    op.add_column(
        "firmoteka_crawl_runs",
        sa.Column(
            "daily_refresh_budget", sa.Integer(), nullable=False, server_default="500"
        ),
    )
    op.add_column(
        "firmoteka_crawl_runs",
        sa.Column(
            "lane_request_state",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "firmoteka_crawl_runs",
        sa.Column("latency_ms_total", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "firmoteka_crawl_runs",
        sa.Column("latency_ms_max", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "firmoteka_crawl_runs",
        sa.Column(
            "latency_histogram",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_firmoteka_concurrency",
        "firmoteka_crawl_runs",
        "concurrency >= 1 AND concurrency <= 64",
    )
    op.create_check_constraint(
        "ck_firmoteka_catalog_concurrency",
        "firmoteka_crawl_runs",
        "catalog_concurrency >= 1 AND catalog_concurrency <= 64",
    )
    op.create_check_constraint(
        "ck_firmoteka_company_concurrency",
        "firmoteka_crawl_runs",
        "company_concurrency >= 1 AND company_concurrency <= 64",
    )
    op.create_check_constraint(
        "ck_firmoteka_backpressure_threshold",
        "firmoteka_crawl_runs",
        "backpressure_threshold > 0",
    )
    op.create_check_constraint(
        "ck_firmoteka_refresh_horizon",
        "firmoteka_crawl_runs",
        "daily_refresh_horizon_days > 0",
    )
    op.create_check_constraint(
        "ck_firmoteka_refresh_budget",
        "firmoteka_crawl_runs",
        "daily_refresh_budget > 0",
    )

    for table in ("firmoteka_catalog_pages", "firmoteka_crawl_items"):
        op.add_column(
            table,
            sa.Column(
                "claimed_by_job_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("worker_jobs.id", ondelete="SET NULL"),
            ),
        )
        op.add_column(table, sa.Column("claim_fencing_token", sa.BigInteger()))
        op.add_column(
            table, sa.Column("claimed_at", sa.DateTime(timezone=True))
        )
        op.create_index(
            f"ix_{table}_claimed_by_job_id", table, ["claimed_by_job_id"]
        )


def downgrade() -> None:
    for table in ("firmoteka_crawl_items", "firmoteka_catalog_pages"):
        op.drop_index(f"ix_{table}_claimed_by_job_id", table_name=table)
        op.drop_column(table, "claimed_at")
        op.drop_column(table, "claim_fencing_token")
        op.drop_column(table, "claimed_by_job_id")

    for name in (
        "ck_firmoteka_refresh_budget",
        "ck_firmoteka_refresh_horizon",
        "ck_firmoteka_backpressure_threshold",
        "ck_firmoteka_company_concurrency",
        "ck_firmoteka_catalog_concurrency",
        "ck_firmoteka_concurrency",
    ):
        op.drop_constraint(name, "firmoteka_crawl_runs", type_="check")
    op.create_check_constraint(
        "ck_firmoteka_concurrency", "firmoteka_crawl_runs", "concurrency = 1"
    )
    for column in (
        "latency_histogram",
        "latency_ms_max",
        "latency_ms_total",
        "lane_request_state",
        "daily_refresh_budget",
        "daily_refresh_horizon_days",
        "backpressure_threshold",
        "company_concurrency",
        "catalog_concurrency",
    ):
        op.drop_column("firmoteka_crawl_runs", column)
