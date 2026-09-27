"""Add company factory generations and source priority metadata.

Revision ID: b7d3e5f1a9c2
Revises: a6c1d9e4f2b7
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b7d3e5f1a9c2"
down_revision = "a6c1d9e4f2b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("data_sets", sa.Column("source_family", sa.String(80)))
    op.add_column("data_sets", sa.Column("capability", sa.String(100)))
    op.add_column("data_sets", sa.Column("risk_role", sa.String(50)))
    op.add_column("data_sets", sa.Column("positive_role", sa.String(50)))
    op.add_column("data_sets", sa.Column("coverage_role", sa.String(50)))
    op.add_column("data_sets", sa.Column("applicability", postgresql.JSONB()))
    op.add_column("data_sets", sa.Column("freshness", postgresql.JSONB()))
    op.add_column("data_sets", sa.Column("precedence", sa.Integer()))
    op.add_column("data_sets", sa.Column("publicability", sa.String(40)))
    op.add_column("data_sets", sa.Column("impact_priority", sa.Integer()))
    for column in (
        "source_family",
        "capability",
        "risk_role",
        "precedence",
        "publicability",
        "impact_priority",
    ):
        op.create_index(f"ix_data_sets_{column}", "data_sets", [column])
    op.execute(
        """
        UPDATE data_sets AS d
        SET source_family = s.code,
            capability = d.domain,
            risk_role = 'context',
            positive_role = 'none',
            coverage_role = CASE WHEN d.domain = 'registry' THEN 'identity' ELSE 'supporting' END,
            applicability = '{"entity_types": ["legal", "individual_entrepreneur"]}'::jsonb,
            freshness = jsonb_build_object(
              'policy', d.freshness_policy,
              'schedule', d.refresh_schedule
            ),
            precedence = d.priority,
            publicability = 'semantic_projection_only',
            impact_priority = d.priority
        FROM data_sources AS s
        WHERE s.id = d.source_id
        """
    )

    op.create_table(
        "factory_generations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("generation_type", sa.String(30), nullable=False),
        sa.Column("generation_key", sa.String(255), nullable=False, unique=True),
        sa.Column("source_id", sa.String(120)),
        sa.Column(
            "dataset_id",
            sa.BigInteger(),
            sa.ForeignKey("data_sets.id", ondelete="CASCADE"),
        ),
        sa.Column("publication_generation", sa.BigInteger()),
        sa.Column("ruleset_version", sa.String(120)),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "selection_complete", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("cursor_company_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("selected_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("scheduled_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("completed_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
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
            "generation_type IN ('source','risk_ruleset','semantic_ruleset')",
            name="ck_factory_generations_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','complete','failed')",
            name="ck_factory_generations_status",
        ),
        sa.CheckConstraint(
            "selected_count >= 0 AND scheduled_count >= 0 "
            "AND completed_count >= 0 AND failed_count >= 0",
            name="ck_factory_generations_counts",
        ),
    )
    op.create_index("ix_factory_generations_generation_type", "factory_generations", ["generation_type"])
    op.create_index("ix_factory_generations_source_id", "factory_generations", ["source_id"])
    op.create_index("ix_factory_generations_dataset_id", "factory_generations", ["dataset_id"])
    op.create_index("ix_factory_generations_status_created", "factory_generations", ["status", "created_at"])

    op.create_table(
        "factory_generation_companies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "generation_id",
            sa.Uuid(),
            sa.ForeignKey("factory_generations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            sa.BigInteger(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "enrichment_run_id",
            sa.Uuid(),
            sa.ForeignKey("company_enrichment_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("risk_assessment_id", sa.String(36)),
        sa.Column("summary_id", sa.String(36)),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.Text()),
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
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "generation_id", "company_id", name="uq_factory_generation_company"
        ),
        sa.CheckConstraint(
            "status IN ('pending','scheduled','complete','failed','not_applicable')",
            name="ck_factory_generation_companies_status",
        ),
    )
    op.create_index("ix_factory_generation_companies_generation_id", "factory_generation_companies", ["generation_id"])
    op.create_index("ix_factory_generation_companies_company_id", "factory_generation_companies", ["company_id"])
    op.create_index("ix_factory_generation_companies_enrichment_run_id", "factory_generation_companies", ["enrichment_run_id"])
    op.create_index("ix_factory_generation_companies_status", "factory_generation_companies", ["generation_id", "status"])

    # Existing operational sources are an adopted baseline.  Only a source
    # becoming operational after this migration should enqueue a new Master
    # backfill automatically.
    op.execute(
        """
        INSERT INTO factory_generations (
          id, generation_type, generation_key, source_id, dataset_id,
          status, selection_complete, metadata_json
        )
        SELECT gen_random_uuid(), 'source', 'source-operational:' || code,
               code, id, 'complete', true,
               '{"baseline_adopted": true}'::jsonb
        FROM data_sets
        WHERE enabled = true
          AND auto_update_status = 'configured'
          AND operational_status = 'current'
          AND last_success_at IS NOT NULL
          AND coalesce((coverage->>'operational_accepted')::boolean, true) = true
        ON CONFLICT (generation_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("factory_generation_companies")
    op.drop_table("factory_generations")
    for column in (
        "impact_priority",
        "publicability",
        "precedence",
        "risk_role",
        "capability",
        "source_family",
    ):
        op.drop_index(f"ix_data_sets_{column}", table_name="data_sets")
    for column in (
        "impact_priority",
        "publicability",
        "precedence",
        "freshness",
        "applicability",
        "coverage_role",
        "positive_role",
        "risk_role",
        "capability",
        "source_family",
    ):
        op.drop_column("data_sets", column)
