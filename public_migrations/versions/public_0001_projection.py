"""Create isolated Public Projection v1 tables.

Revision ID: public_0001
Revises: None
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "public_0001"
down_revision = None
branch_labels = ("public_projection",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_releases",
        sa.Column("release_id", sa.String(120), primary_key=True),
        sa.Column("schema_version", sa.String(80), nullable=False),
        sa.Column("source_main_sha", sa.String(40), nullable=False),
        sa.Column("previous_release_id", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.CheckConstraint("record_count = 40", name="ck_public_release_record_count"),
        sa.CheckConstraint("status IN ('staged','active','rolled_back')", name="ck_public_release_status"),
        sa.ForeignKeyConstraint(["previous_release_id"], ["public_releases.release_id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "uq_public_release_one_active",
        "public_releases",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "public_company_projections",
        sa.Column("release_id", sa.String(120), nullable=False),
        sa.Column("inn", sa.String(10), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("normalized_name", sa.String(500), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("content_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("index_eligible", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["release_id"], ["public_releases.release_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("release_id", "inn"),
        sa.CheckConstraint("inn ~ '^[0-9]{10}$'", name="ck_public_projection_legal_inn"),
    )
    op.create_index("ix_public_projection_inn", "public_company_projections", ["inn"])
    op.create_index("ix_public_projection_normalized_name", "public_company_projections", ["normalized_name"])
    op.create_table(
        "public_publication_state",
        sa.Column("singleton", sa.Boolean(), primary_key=True),
        sa.Column("active_release_id", sa.String(120), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("singleton = TRUE", name="ck_publication_state_singleton"),
        sa.ForeignKeyConstraint(["active_release_id"], ["public_releases.release_id"], ondelete="RESTRICT"),
    )
    op.execute("INSERT INTO public_publication_state(singleton, active_release_id) VALUES(TRUE, NULL)")


def downgrade() -> None:
    op.drop_table("public_publication_state")
    op.drop_table("public_company_projections")
    op.drop_index("uq_public_release_one_active", table_name="public_releases")
    op.drop_table("public_releases")
