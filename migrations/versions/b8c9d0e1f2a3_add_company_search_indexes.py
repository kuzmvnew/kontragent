"""add PostgreSQL search indexes

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-18
"""

from alembic import op


revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Building the trigram index on a populated registry can legitimately take
    # longer than the application's defensive per-statement timeout.
    op.execute("SET LOCAL statement_timeout = '0'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_companies_inn_prefix "
        "ON companies (inn text_pattern_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_companies_name_lower_trgm "
        "ON companies USING gin (lower(name) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_companies_name_lower "
        "ON companies (lower(name))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_companies_name_lower")
    op.execute("DROP INDEX IF EXISTS ix_companies_name_lower_trgm")
    op.execute("DROP INDEX IF EXISTS ix_companies_inn_prefix")
