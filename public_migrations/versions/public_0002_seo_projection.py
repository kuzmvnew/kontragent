"""Add versioned SEO projection fields to the isolated public database.

Revision ID: public_0002
Revises: public_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "public_0002"
down_revision = "public_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("public_company_projections", sa.Column("seo_projection", postgresql.JSONB(), nullable=True))
    op.add_column("public_company_projections", sa.Column("seo_decision", sa.String(40), nullable=True))
    op.add_column("public_company_projections", sa.Column("seo_compiler_version", sa.String(80), nullable=True))
    op.add_column("public_company_projections", sa.Column("search_visible_hash", sa.String(64), nullable=True))
    op.add_column("public_company_projections", sa.Column("non_identity_content_hash", sa.String(64), nullable=True))
    op.add_column("public_company_projections", sa.Column("sitemap_shard", sa.String(1), nullable=True))
    op.add_column("public_company_projections", sa.Column("seo_content_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_public_projection_seo_decision",
        "public_company_projections",
        "seo_decision IS NULL OR seo_decision IN ('INDEX','NOINDEX_RECOVERABLE','NOT_PUBLISHED','GONE')",
    )
    op.create_check_constraint(
        "ck_public_projection_sitemap_shard",
        "public_company_projections",
        "sitemap_shard IS NULL OR sitemap_shard ~ '^[0-9a-f]$'",
    )
    op.create_check_constraint(
        "ck_public_projection_search_hash",
        "public_company_projections",
        "search_visible_hash IS NULL OR search_visible_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_public_projection_non_identity_hash",
        "public_company_projections",
        "non_identity_content_hash IS NULL OR non_identity_content_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_index(
        "ix_public_projection_seo_sitemap",
        "public_company_projections",
        ["release_id", "seo_decision", "sitemap_shard", "inn"],
    )
    op.create_index(
        "ix_public_projection_seo_catalog",
        "public_company_projections",
        ["release_id", "seo_decision", "normalized_name", "inn"],
    )


def downgrade() -> None:
    op.drop_index("ix_public_projection_seo_catalog", table_name="public_company_projections")
    op.drop_index("ix_public_projection_seo_sitemap", table_name="public_company_projections")
    op.drop_constraint("ck_public_projection_non_identity_hash", "public_company_projections", type_="check")
    op.drop_constraint("ck_public_projection_search_hash", "public_company_projections", type_="check")
    op.drop_constraint("ck_public_projection_sitemap_shard", "public_company_projections", type_="check")
    op.drop_constraint("ck_public_projection_seo_decision", "public_company_projections", type_="check")
    op.drop_column("public_company_projections", "seo_content_updated_at")
    op.drop_column("public_company_projections", "sitemap_shard")
    op.drop_column("public_company_projections", "non_identity_content_hash")
    op.drop_column("public_company_projections", "search_visible_hash")
    op.drop_column("public_company_projections", "seo_compiler_version")
    op.drop_column("public_company_projections", "seo_decision")
    op.drop_column("public_company_projections", "seo_projection")
