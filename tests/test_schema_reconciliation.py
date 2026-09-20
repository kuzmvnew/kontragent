from copy import deepcopy
import importlib
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa

from app.database.base import Base
from app.database.postgres import engine
import app.models  # noqa: F401
from migrations.schema_validation_v1 import (
    audit_metadata,
    compare_table_schema,
    inspected_table_schema,
    model_table_schema,
)


reconciliation = importlib.import_module(
    "migrations.versions.a7d4e9f2c6b1_reconcile_missing_domain_snapshot_tables"
)
TABLES = (
    "company_tax_regime_snapshots", "company_revenue_expense_snapshots",
)


def test_all_orm_tables_exist_after_alembic_clean_install():
    # CI provisions a blank PostgreSQL database via `alembic upgrade head`.
    # Do not create or patch missing application tables in this regression.
    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
        heads = set(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
        assert set(connection.scalars(sa.text("SELECT version_num FROM alembic_version"))) == heads
        assert set(TABLES).issubset(sa.inspect(connection).get_table_names())
        report = audit_metadata(connection, Base.metadata)
        assert report["compatible"], report["errors"]


@pytest.fixture
def migration_connection():
    # All DDL/DML below is rolled back in a unique schema of the isolated test
    # database. No tests migrate, downgrade or write the historical database.
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
            schema = "schema_reconcile_test_" + uuid4().hex
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            connection.exec_driver_sql("CREATE TABLE companies (id BIGINT PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE data_sets (id BIGINT PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO companies VALUES (1)")
            connection.exec_driver_sql("INSERT INTO data_sets VALUES (1)")
            with Operations.context(MigrationContext.configure(connection)):
                yield connection
        finally:
            transaction.rollback()


def _assert_snapshot_schema(connection):
    inspector = sa.inspect(connection)
    for name in TABLES:
        errors, _ = compare_table_schema(
            model_table_schema(Base.metadata.tables[name], connection.dialect),
            inspected_table_schema(inspector, name), strict=True,
        )
        assert not errors, errors


def _seed(connection):
    connection.exec_driver_sql("""
        INSERT INTO company_tax_regime_snapshots
            (company_id, dataset_id, entity_type, data_date, regime_codes)
        VALUES (1, 1, 'LEGAL', DATE '2026-09-18', '["USN"]'::json)
    """)
    connection.exec_driver_sql("""
        INSERT INTO company_revenue_expense_snapshots
            (company_id, dataset_id, data_date, data_year, source_document_id,
             revenue, expenses, profit_loss)
        VALUES (1, 1, DATE '2026-09-18', 2025, 'fixture', 100.25, 75.00, 25.25)
    """)


def _state(connection):
    inspector = sa.inspect(connection)
    return {name: {
        "schema": inspected_table_schema(inspector, name),
        "rows": list(connection.scalars(sa.text(f"SELECT row_to_json(t)::text FROM {name} t ORDER BY id"))),
        "oid": connection.scalar(sa.text("SELECT to_regclass(:name)::oid"), {"name": name}),
    } for name in TABLES if inspector.has_table(name)}


def test_fresh_upgrade_downgrade_upgrade_recreates_owned_tables(migration_connection):
    connection = migration_connection
    assert not set(TABLES).intersection(sa.inspect(connection).get_table_names())
    reconciliation.upgrade()
    _assert_snapshot_schema(connection)
    assert all(sa.inspect(connection).get_table_comment(name)["text"] == reconciliation.CREATED_MARKER for name in TABLES)
    reconciliation.downgrade()
    assert not set(TABLES).intersection(sa.inspect(connection).get_table_names())
    reconciliation.upgrade()
    _assert_snapshot_schema(connection)


@pytest.mark.parametrize("legacy_comment", [None, "legacy table", "created_by_alembic_reconciliation:another_revision"])
def test_legacy_upgrade_and_downgrade_preserve_data_schema_and_oids(migration_connection, legacy_comment):
    connection = migration_connection
    reconciliation.upgrade()
    _seed(connection)
    for name in TABLES:
        comment_sql = "NULL" if legacy_comment is None else "'" + legacy_comment + "'"
        connection.exec_driver_sql(f"COMMENT ON TABLE {name} IS {comment_sql}")
    before = _state(connection)
    reconciliation.upgrade()
    assert _state(connection) == before
    reconciliation.downgrade()
    assert _state(connection) == before


def test_mixed_legacy_and_created_ownership_drops_only_new_table(migration_connection):
    connection = migration_connection
    reconciliation.upgrade()
    connection.exec_driver_sql(f"COMMENT ON TABLE {TABLES[0]} IS 'legacy tax snapshot'")
    sa.Table(TABLES[1], sa.MetaData()).drop(connection)
    before = _state(connection)
    reconciliation.upgrade()
    reconciliation.downgrade()
    assert _state(connection) == before


@pytest.mark.parametrize("ddl, expected_error", [
    ("ALTER TABLE company_revenue_expense_snapshots ALTER COLUMN revenue TYPE NUMERIC(20, 2)", "type_mismatch"),
    ("ALTER TABLE company_revenue_expense_snapshots ALTER COLUMN revenue DROP NOT NULL", "nullable_mismatch"),
    ("ALTER TABLE company_revenue_expense_snapshots DROP CONSTRAINT company_revenue_expense_snapshots_company_id_fkey", "missing_or_incompatible_foreign_key"),
    ("ALTER TABLE company_revenue_expense_snapshots DROP CONSTRAINT uq_company_revexp_company_dataset_date", "missing_or_incompatible_unique_constraint"),
    ("DROP INDEX ix_company_revenue_expense_snapshots_data_year", "missing_or_incompatible_index"),
    ("ALTER TABLE company_revenue_expense_snapshots ALTER COLUMN created_at DROP DEFAULT", "default_mismatch"),
    ("ALTER TABLE company_revenue_expense_snapshots ALTER COLUMN id DROP IDENTITY", "identity_mismatch"),
    ("CREATE UNIQUE INDEX unexpected_unique_company ON company_revenue_expense_snapshots (company_id)", "unexpected_unique_index"),
])
def test_incompatible_legacy_fails_before_creating_other_missing_table(migration_connection, ddl, expected_error):
    connection = migration_connection
    reconciliation.upgrade()
    sa.Table(TABLES[0], sa.MetaData()).drop(connection)
    connection.exec_driver_sql(ddl)
    before = _state(connection)
    with pytest.raises(RuntimeError, match=expected_error):
        reconciliation.upgrade()
    assert _state(connection) == before
    assert not sa.inspect(connection).has_table(TABLES[0])


def test_schema_gate_catches_missing_column_and_primary_key(migration_connection):
    connection = migration_connection
    reconciliation.upgrade()
    connection.exec_driver_sql("ALTER TABLE company_tax_regime_snapshots DROP COLUMN source_document_date")
    connection.exec_driver_sql("ALTER TABLE company_tax_regime_snapshots DROP CONSTRAINT company_tax_regime_snapshots_pkey")
    errors, _ = compare_table_schema(
        model_table_schema(Base.metadata.tables[TABLES[0]], connection.dialect),
        inspected_table_schema(sa.inspect(connection), TABLES[0]),
    )
    assert {error["kind"] for error in errors} == {"missing_column", "primary_key_mismatch"}


def test_unique_index_equivalence_requires_actual_uniqueness(migration_connection):
    connection = migration_connection
    reconciliation.upgrade()
    actual = inspected_table_schema(sa.inspect(connection), TABLES[0])
    expected = model_table_schema(Base.metadata.tables[TABLES[0]], connection.dialect)
    name = "ix_company_tax_regime_snapshots_entity_type"
    expected["indexes"][name]["unique"] = True
    errors, _ = compare_table_schema(expected, actual)
    assert any(error["path"] == name for error in errors)
    equivalent = deepcopy(actual)
    equivalent["unique_constraints"].append({"name": "uq_type", "columns": ["entity_type"]})
    errors, observations = compare_table_schema(expected, equivalent)
    assert not errors
    assert any(item["kind"] == "unique_index_backed_by_unique_constraint" for item in observations)


def test_pytest_rejects_historical_database_before_collection():
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "DATABASE_URL": "postgresql+psycopg://localhost/kontragent"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_schema_reconciliation.py"],
        cwd=root, env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "Refusing pytest against historical database" in result.stderr
