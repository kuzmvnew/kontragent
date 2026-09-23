"""add S02 controlled live pilot state and generations

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_tax_debt_snapshots",
        sa.Column(
            "publication_generation",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
    )
    op.drop_constraint(
        "uq_company_tax_debt_company_dataset_date",
        "company_tax_debt_snapshots",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_company_tax_debt_company_dataset_date_generation",
        "company_tax_debt_snapshots",
        ["company_id", "dataset_id", "data_date", "publication_generation"],
    )
    op.create_index(
        "ix_company_tax_debt_snapshots_publication_generation",
        "company_tax_debt_snapshots",
        ["publication_generation"],
        unique=False,
    )

    op.create_table(
        "fns_tax_debt_publication_generations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column("worker_run_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("publication_scope", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("staging_pointer", sa.Text(), nullable=False),
        sa.Column("raw_pointer", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("source_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("official_actual_until", sa.Date(), nullable=True),
        sa.Column("last_data_date", sa.Date(), nullable=True),
        sa.Column("record_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "counters", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "validation_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "dataset_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "generation >= 0", name="ck_fns_tax_debt_generation_nonnegative"
        ),
        sa.CheckConstraint(
            "publication_scope IN ('baseline', 'pilot')",
            name="ck_fns_tax_debt_publication_scope",
        ),
        sa.CheckConstraint(
            "status IN ('baseline', 'active', 'rollback', 'superseded')",
            name="ck_fns_tax_debt_generation_status",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["fns_tax_debt_raw_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["data_sets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["worker_run_id"], ["worker_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_id",
            "artifact_id",
            "publication_scope",
            name="uq_fns_tax_debt_publication_artifact",
        ),
        sa.UniqueConstraint(
            "dataset_id",
            "generation",
            name="uq_fns_tax_debt_publication_generation",
        ),
    )
    for column in (
        "dataset_id",
        "artifact_id",
        "worker_run_id",
        "status",
        "checksum",
    ):
        op.create_index(
            f"ix_fns_tax_debt_publication_generations_{column}",
            "fns_tax_debt_publication_generations",
            [column],
            unique=False,
        )
    op.create_index(
        "uq_fns_tax_debt_one_active_generation",
        "fns_tax_debt_publication_generations",
        ["dataset_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "fns_tax_debt_pilot_state",
        sa.Column("source_id", sa.String(length=120), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=True),
        sa.Column("pilot_environment", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "cohort_inns",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_discovery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered_artifact_url", sa.Text(), nullable=True),
        sa.Column("discovered_xsd_url", sa.Text(), nullable=True),
        sa.Column("discovered_checksum", sa.String(length=64), nullable=True),
        sa.Column("discovered_source_as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("official_actual_until", sa.Date(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_raw_pointer", sa.Text(), nullable=True),
        sa.Column("active_checksum", sa.String(length=64), nullable=True),
        sa.Column("active_source_as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("baseline_generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("normalized_generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("fact_generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("query_generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("rollback_fact_generation", sa.BigInteger(), nullable=True),
        sa.Column("rollback_normalized_generation", sa.BigInteger(), nullable=True),
        sa.Column("active_data_date", sa.Date(), nullable=True),
        sa.Column("baseline_data_date", sa.Date(), nullable=True),
        sa.Column(
            "counters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "freshness", sa.String(length=20), server_default="unknown", nullable=False
        ),
        sa.Column(
            "errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "normalized_generation >= 0 AND fact_generation >= 0 "
            "AND query_generation >= 0 AND baseline_generation >= 0",
            name="ck_fns_tax_debt_pilot_fact_query_generation",
        ),
        sa.CheckConstraint(
            "freshness IN ('unknown', 'current', 'stale')",
            name="ck_fns_tax_debt_pilot_freshness",
        ),
        sa.CheckConstraint(
            "generation >= 0", name="ck_fns_tax_debt_pilot_generation"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["data_sets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("source_id"),
        sa.UniqueConstraint("dataset_id"),
    )


def downgrade() -> None:
    # The pilot scope never becomes the legacy shared dataset.  Restore the
    # captured baseline metadata and publication pointer before removing the
    # pilot ledger and collapsing the schema back to its pre-pilot unique key.
    op.execute(
        sa.text(
            """
            UPDATE worker_publication_state AS publication
            SET active_pointer = generation.staging_pointer,
                rollback_pointer = NULL,
                generation = publication.generation + 1,
                published_by_run_id = generation.worker_run_id,
                validation_metadata = jsonb_build_object(
                    'checksum', generation.checksum,
                    'validation', generation.validation_metadata,
                    'staging', jsonb_build_object(
                        'replayable', true,
                        'source_id', pilot.source_id,
                        'migration_restore', true
                    )
                ),
                updated_at = generation.published_at
            FROM fns_tax_debt_pilot_state AS pilot
            JOIN fns_tax_debt_publication_generations AS generation
              ON generation.dataset_id = pilot.dataset_id
             AND generation.generation = pilot.baseline_generation
             AND generation.publication_scope = 'baseline'
            WHERE publication.source_id = pilot.source_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE data_sets AS d
            SET last_attempt_at = (g.dataset_metadata ->> 'last_attempt_at')::timestamptz,
                last_success_at = (g.dataset_metadata ->> 'last_success_at')::timestamptz,
                last_data_date = (g.dataset_metadata ->> 'last_data_date')::date,
                source_as_of = (g.dataset_metadata ->> 'source_as_of')::timestamptz,
                retrieved_at = (g.dataset_metadata ->> 'retrieved_at')::timestamptz,
                checked_at = (g.dataset_metadata ->> 'checked_at')::timestamptz,
                published_at = (g.dataset_metadata ->> 'published_at')::timestamptz,
                record_count = (g.dataset_metadata ->> 'record_count')::bigint,
                coverage = NULLIF(g.dataset_metadata -> 'coverage', 'null'::jsonb),
                operational_status = COALESCE(
                    g.dataset_metadata ->> 'operational_status',
                    d.operational_status
                ),
                last_error = g.dataset_metadata ->> 'last_error',
                last_error_at = (g.dataset_metadata ->> 'last_error_at')::timestamptz,
                retry_count = COALESCE(
                    (g.dataset_metadata ->> 'retry_count')::integer,
                    0
                ),
                next_retry_at = (g.dataset_metadata ->> 'next_retry_at')::timestamptz,
                next_expected_update_at =
                    (g.dataset_metadata ->> 'next_expected_update_at')::timestamptz,
                auto_update_status = COALESCE(
                    g.dataset_metadata ->> 'auto_update_status',
                    d.auto_update_status
                )
            FROM fns_tax_debt_pilot_state AS p
            JOIN fns_tax_debt_publication_generations AS g
              ON g.dataset_id = p.dataset_id
             AND g.generation = p.baseline_generation
             AND g.publication_scope = 'baseline'
            WHERE d.id = p.dataset_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM company_tax_debt_snapshots AS snapshot
            USING fns_tax_debt_pilot_state AS pilot
            WHERE snapshot.dataset_id = pilot.dataset_id
              AND snapshot.publication_generation <> pilot.baseline_generation
            """
        )
    )
    op.drop_table("fns_tax_debt_pilot_state")
    op.drop_index(
        "uq_fns_tax_debt_one_active_generation",
        table_name="fns_tax_debt_publication_generations",
    )
    for column in reversed(
        ("dataset_id", "artifact_id", "worker_run_id", "status", "checksum")
    ):
        op.drop_index(
            f"ix_fns_tax_debt_publication_generations_{column}",
            table_name="fns_tax_debt_publication_generations",
        )
    op.drop_table("fns_tax_debt_publication_generations")

    op.drop_index(
        "ix_company_tax_debt_snapshots_publication_generation",
        table_name="company_tax_debt_snapshots",
    )
    op.drop_constraint(
        "uq_company_tax_debt_company_dataset_date_generation",
        "company_tax_debt_snapshots",
        type_="unique",
    )
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY company_id, dataset_id, data_date
                           ORDER BY publication_generation, id DESC
                       ) AS duplicate_rank
                FROM company_tax_debt_snapshots
            )
            DELETE FROM company_tax_debt_snapshots AS snapshot
            USING ranked
            WHERE snapshot.id = ranked.id
              AND ranked.duplicate_rank > 1
            """
        )
    )
    op.create_unique_constraint(
        "uq_company_tax_debt_company_dataset_date",
        "company_tax_debt_snapshots",
        ["company_id", "dataset_id", "data_date"],
    )
    op.drop_column("company_tax_debt_snapshots", "publication_generation")
