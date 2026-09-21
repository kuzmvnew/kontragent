"""Reconcile missing domain snapshot tables without altering legacy tables.

Revision ID: a7d4e9f2c6b1
Revises: f6a7b8c9d0e1
Create Date: 2026-09-20
"""

import json

from alembic import op
import sqlalchemy as sa

from migrations.schema_validation_v1 import (
    compare_table_schema,
    inspected_table_schema,
    model_table_schema,
)


revision = "a7d4e9f2c6b1"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None
CREATED_MARKER = "created_by_alembic_reconciliation:" + revision


def snapshot_tables():
    """Frozen revision schema. Never import the evolving application models."""
    metadata = sa.MetaData()
    # Referenced tables are declarations only; this revision never creates them.
    sa.Table("companies", metadata, sa.Column("id", sa.BigInteger(), primary_key=True))
    sa.Table("data_sets", metadata, sa.Column("id", sa.BigInteger(), primary_key=True))
    tax = sa.Table(
        "company_tax_regime_snapshots", metadata,
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("entity_type", sa.String(40), nullable=False, index=True),
        sa.Column("data_date", sa.Date(), nullable=False, index=True),
        sa.Column("regime_codes", sa.JSON(), nullable=False),
        sa.Column("source_document_id", sa.String(100), nullable=True),
        sa.Column("source_document_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("company_id", "dataset_id", "data_date", name="uq_company_tax_regime_company_dataset_date"),
        comment=CREATED_MARKER,
    )
    revenue = sa.Table(
        "company_revenue_expense_snapshots", metadata,
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("dataset_id", sa.BigInteger(), sa.ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("data_date", sa.Date(), nullable=False, index=True),
        sa.Column("data_year", sa.Integer(), nullable=False, index=True),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column("source_document_id", sa.String(100), nullable=False, index=True),
        sa.Column("source_company_name", sa.String(1000), nullable=True),
        sa.Column("revenue", sa.Numeric(22, 2), nullable=False),
        sa.Column("expenses", sa.Numeric(22, 2), nullable=False),
        sa.Column("profit_loss", sa.Numeric(22, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("company_id", "dataset_id", "data_date", name="uq_company_revexp_company_dataset_date"),
        comment=CREATED_MARKER,
    )
    return tax, revenue


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("Snapshot reconciliation requires live PostgreSQL schema inspection")
    inspector = sa.inspect(connection)
    tables = snapshot_tables()
    existing = set(inspector.get_table_names())
    errors = []
    # Validate every existing table before issuing any DDL, including the mixed
    # case where one table is absent and the other has an incompatible schema.
    for table in tables:
        if table.name in existing:
            differences, _ = compare_table_schema(
                model_table_schema(table, connection.dialect),
                inspected_table_schema(inspector, table.name), strict=True,
            )
            errors.extend({"table": table.name, **difference} for difference in differences)
    if errors:
        raise RuntimeError("Incompatible pre-existing snapshot schema; no tables changed: " + json.dumps(errors, ensure_ascii=False, sort_keys=True))
    for table in tables:
        if table.name not in existing:
            # Explicit frozen tables, not Base.metadata.create_all. SQLAlchemy
            # emits their PK/FK/unique/index definitions and ownership comment.
            table.create(connection, checkfirst=False)


def downgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    for table in reversed(snapshot_tables()):
        if inspector.has_table(table.name):
            comment = inspector.get_table_comment(table.name).get("text") or ""
            if comment == CREATED_MARKER:
                op.drop_table(table.name)
