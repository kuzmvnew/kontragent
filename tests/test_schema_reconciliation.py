from copy import deepcopy
import importlib
import os
from pathlib import Path
import re
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
    _canonical_check,
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


def _old_broken_canonical_check(value):
    """Characterize the TASK-014 implementation without importing Git history."""
    def strip_outer_parentheses(expression):
        expression = expression.strip()
        while expression.startswith("(") and expression.endswith(")"):
            depth = 0
            encloses_all = True
            for index, character in enumerate(expression):
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0 and index != len(expression) - 1:
                        encloses_all = False
                        break
            if not encloses_all or depth != 0:
                break
            expression = expression[1:-1].strip()
        return expression

    value = strip_outer_parentheses(str(value))
    parts = re.split(r"('(?:''|[^'])*')", value)
    cast_pattern = re.compile(
        r"::\s*(?:character\s+varying|varchar|text|smallint|integer|bigint|"
        r"numeric|boolean|date)(?:\s*\([^)]*\))?(?:\s*\[\s*\])?",
        flags=re.IGNORECASE,
    )
    value = "".join(
        part if index % 2 else cast_pattern.sub("", part)
        for index, part in enumerate(parts)
    )
    value = re.sub(
        r"\b([A-Za-z_][\w.]*)\s*=\s*ANY\s*\(\s*ARRAY\s*\[(.*?)\]\s*\)",
        r"\1 IN (\2)", value, flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(
        r"\b([A-Za-z_][\w.]*)\s*<>\s*ALL\s*\(\s*ARRAY\s*\[(.*?)\]\s*\)",
        r"\1 NOT IN (\2)", value, flags=re.IGNORECASE | re.DOTALL,
    )
    parts = re.split(r"('(?:''|[^'])*')", value)
    value = "".join(
        part if index % 2 else part.lower()
        for index, part in enumerate(parts)
    )
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s*([(),])\s*", r"\1", value)
    value = re.sub(r"\s*(>=|<=|<>|!=|=|>|<)\s*", r"\1", value)
    return strip_outer_parentheses(value)


def _schema_with_check(sqltext, *, column_name=None, column_type=None):
    columns = {}
    if column_name is not None:
        columns[column_name] = {
            "type": column_type,
            "nullable": True,
            "default": None,
            "client_default": None,
            "identity": None,
        }
    return {
        "columns": columns,
        "primary_key": [],
        "foreign_keys": [],
        "unique_constraints": [],
        "check_constraints": [{
            "name": "ck_task_014",
            "sqltext": _canonical_check(sqltext),
        }],
        "indexes": {},
    }


QA_F02C_COUNTEREXAMPLES = (
    ("literal repeated whitespace", "label = 'A  B'", "label = 'A B'"),
    ("literal comma whitespace", "label = 'A , B'", "label = 'A,B'"),
    ("literal parenthesis whitespace", "label = 'A ( B )'", "label = 'A(B)'"),
    ("literal operator whitespace", "label = 'A = B'", "label = 'A=B'"),
    ("scalar cast type", "code::text = '01'", "code::integer = '01'"),
    (
        "numeric cast typemod",
        "amount::numeric(10, 2) >= 0",
        "amount::numeric(12, 4) >= 0",
    ),
    (
        "array and scalar cast types",
        "code::text = ANY (ARRAY['01'::text])",
        "code::integer = ANY (ARRAY['01'::integer])",
    ),
)


@pytest.mark.parametrize(
    ("case", "expected_sql", "actual_sql"),
    QA_F02C_COUNTEREXAMPLES,
    ids=[case[0] for case in QA_F02C_COUNTEREXAMPLES],
)
def test_task_014_counterexamples_fail_closed(case, expected_sql, actual_sql):
    assert case
    assert expected_sql != actual_sql
    assert _old_broken_canonical_check(expected_sql) == _old_broken_canonical_check(actual_sql)
    assert _canonical_check(expected_sql) != _canonical_check(actual_sql)
    errors, _ = compare_table_schema(
        _schema_with_check(expected_sql), _schema_with_check(actual_sql),
    )
    assert any(
        error["kind"] == "missing_or_incompatible_check_constraint"
        and error["path"] == "ck_task_014"
        for error in errors
    )


@pytest.mark.parametrize(("expected_sql", "actual_sql"), (
    ("status = 'active'", "status = 'inactive'"),
    ("label = 'A  B'", "label = 'A B'"),
    ("label = 'A'' B'", "label = 'A''B'"),
    ("label = 'x::text'", "label = 'x'"),
))
def test_check_string_literals_are_preserved_exactly(expected_sql, actual_sql):
    assert _canonical_check(expected_sql) != _canonical_check(actual_sql)
    errors, _ = compare_table_schema(
        _schema_with_check(expected_sql), _schema_with_check(actual_sql),
    )
    assert errors


@pytest.mark.parametrize(("expected_sql", "actual_sql"), (
    ("code::text = '01'", "code::integer = '01'"),
    ("amount::numeric(10, 2) >= 0", "amount::numeric(12, 4) >= 0"),
    ("happened_on::date >= DATE '2026-01-01'", "happened_on::text >= '2026-01-01'"),
))
def test_check_casts_are_significant_by_default(expected_sql, actual_sql):
    assert _canonical_check(expected_sql) != _canonical_check(actual_sql)
    errors, _ = compare_table_schema(
        _schema_with_check(expected_sql), _schema_with_check(actual_sql),
    )
    assert errors


@pytest.mark.parametrize(("orm_sql", "postgresql_sql"), (
    (
        "fact_type IN ('website', 'phone', 'A  B')",
        "((fact_type)::text = ANY ((ARRAY['website'::character varying, "
        "'phone'::character varying, 'A  B'::character varying])::text[]))",
    ),
    (
        "code IN ('alpha', 'beta')",
        "(code = ANY (ARRAY['alpha'::text, 'beta'::text]))",
    ),
    (
        "fact_type IN ('website', 'phone')",
        "((fact_type)::text = ANY (ARRAY['website'::character varying::text, "
        "'phone'::character varying::text]))",
    ),
    ("( AMOUNT >= 0 )", "amount>=0"),
))
def test_known_postgresql_check_representations_are_equivalent(
    orm_sql, postgresql_sql,
):
    column_name = "fact_type" if "fact_type" in orm_sql.lower() else "code"
    column_type = "VARCHAR(60)" if column_name == "fact_type" else "TEXT"
    if "amount" in orm_sql.lower():
        assert _canonical_check(orm_sql) == _canonical_check(postgresql_sql)
        schemas = (_schema_with_check(orm_sql), _schema_with_check(postgresql_sql))
    else:
        assert _canonical_check(orm_sql) != _canonical_check(postgresql_sql)
        schemas = (
            _schema_with_check(
                orm_sql, column_name=column_name, column_type=column_type,
            ),
            _schema_with_check(
                postgresql_sql, column_name=column_name, column_type=column_type,
            ),
        )
    errors, _ = compare_table_schema(
        *schemas,
    )
    assert not errors


def test_dump_restored_varchar_any_rejects_non_text_second_cast():
    expected = _schema_with_check(
        "code IN ('alpha')", column_name="code", column_type="VARCHAR(20)"
    )
    actual = _schema_with_check(
        "code::text = ANY (ARRAY['alpha'::character varying::integer])",
        column_name="code", column_type="VARCHAR(20)",
    )
    errors, _ = compare_table_schema(expected, actual)
    assert errors


@pytest.mark.parametrize(("expected_sql", "actual_sql"), (
    ('U&"a" = 1', 'U & "a" = 1'),
    ('u&"a" = 1', 'u & "a" = 1'),
    ("label = U&'a'", "label = U & 'a'"),
))
def test_unicode_prefix_adjacency_is_semantically_significant(
    expected_sql, actual_sql,
):
    assert _canonical_check(expected_sql) != _canonical_check(actual_sql)
    errors, _ = compare_table_schema(
        _schema_with_check(expected_sql), _schema_with_check(actual_sql),
    )
    assert any(
        error["kind"] == "missing_or_incompatible_check_constraint"
        for error in errors
    )


def test_unicode_prefix_case_and_ordinary_formatting_are_equivalent():
    assert _canonical_check('U&"a" = \'x\'') == _canonical_check(
        'u&"a"=\'x\'',
    )


def test_unicode_quoted_identifier_escaped_content_stays_one_token():
    expected_sql = 'U&"a""b" = 1'
    actual_sql = 'U & "a""b" = 1'
    assert _canonical_check(expected_sql) != _canonical_check(actual_sql)


def test_ordinary_quoted_identifier_formatting_remains_equivalent():
    assert _canonical_check('( "a" = 1 )') == _canonical_check('"a"=1')


def test_unicode_uescape_fails_closed_as_opaque():
    expected_sql = 'U&"a!0062" UESCAPE \'!\' = 1'
    actual_sql = 'u&"a!0062"   uescape \'!\'=1'
    assert _canonical_check(expected_sql)[0] == "opaque"
    assert _canonical_check(actual_sql)[0] == "opaque"
    assert _canonical_check(expected_sql) != _canonical_check(actual_sql)


@pytest.mark.parametrize("literal", (
    "A  B",
    "A , B",
    "A ( B )",
    "A = B",
    "A::text",
    "A IN B",
    "A ANY B",
    "A''quoted",
))
def test_sql_looking_string_contents_remain_opaque_to_lexer(literal):
    left = f"label = '{literal}'"
    right = f"label = '{literal.replace(' ', '')}'"
    if left == right:
        right = f"label = '{literal}x'"
    assert _canonical_check(left) != _canonical_check(right)


@pytest.mark.parametrize(("column_type", "actual_sql", "compatible"), (
    ("TEXT", "code = ANY (ARRAY['01'::text])", True),
    (
        "VARCHAR(20)",
        "(code)::text = ANY ((ARRAY['01'::character varying])::text[])",
        True,
    ),
    ("INTEGER", "code = ANY (ARRAY['01'::text])", False),
    ("NUMERIC(10, 2)", "code = ANY (ARRAY['01'::text])", False),
    (None, "code = ANY (ARRAY['01'::text])", False),
))
def test_in_any_equivalence_requires_resolved_text_like_column_type(
    column_type, actual_sql, compatible,
):
    column = {"column_name": "code", "column_type": column_type} if column_type else {}
    expected = _schema_with_check("code IN ('01')", **column)
    actual = _schema_with_check(actual_sql, **column)
    errors, _ = compare_table_schema(expected, actual)
    assert (not errors) is compatible


def test_same_in_any_syntax_is_context_dependent_for_text_and_integer():
    expression = "code IN ('01')"
    deparse = "code = ANY (ARRAY['01'::text])"
    text_errors, _ = compare_table_schema(
        _schema_with_check(expression, column_name="code", column_type="TEXT"),
        _schema_with_check(deparse, column_name="code", column_type="TEXT"),
    )
    integer_errors, _ = compare_table_schema(
        _schema_with_check(expression, column_name="code", column_type="INTEGER"),
        _schema_with_check(deparse, column_name="code", column_type="INTEGER"),
    )
    assert not text_errors
    assert any(
        error["kind"] == "missing_or_incompatible_check_constraint"
        for error in integer_errors
    )


@pytest.mark.parametrize(("expected_sql", "actual_sql"), (
    ("code IN ('01')", "code = ANY (ARRAY['01'::text, '02'::text])"),
    ("code IN ('01')", "code = ANY (ARRAY['02'::text])"),
    ("code IN ('01')", "code <> ANY (ARRAY['01'::text])"),
    ("code IN ('01')", "code = ANY (ARRAY['01'::varchar])"),
    ("code IN ('01')", "code = '01'"),
    ("lower(code) IN ('01')", "code = ANY (ARRAY['01'::text])"),
    ("code IN ('01', NULL)", "code = ANY (ARRAY['01'::text])"),
))
def test_in_any_equivalence_rejects_unproven_shapes(expected_sql, actual_sql):
    errors, _ = compare_table_schema(
        _schema_with_check(
            expected_sql, column_name="code", column_type="TEXT",
        ),
        _schema_with_check(
            actual_sql, column_name="code", column_type="TEXT",
        ),
    )
    assert any(
        error["kind"] == "missing_or_incompatible_check_constraint"
        for error in errors
    )


@pytest.mark.parametrize(("expected_sql", "actual_sql"), (
    ("amount BETWEEN 1 AND 2", "amount >= 1 AND amount <= 2"),
    ("qty IN (1, NULL)", "qty = ANY (ARRAY[1, 2, NULL::integer])"),
    ("payload @> '{\"active\": true}'", "payload->>'active' = 'true'"),
))
def test_unknown_check_equivalence_fails_closed(expected_sql, actual_sql):
    assert _canonical_check(expected_sql) != _canonical_check(actual_sql)
    errors, _ = compare_table_schema(
        _schema_with_check(expected_sql), _schema_with_check(actual_sql),
    )
    assert errors


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
        assert any(
            item["table"] == "dataset_publications"
            and item["kind"] == "intentional_database_index"
            and item["path"] == "uq_dataset_publications_one_active"
            for item in report["observations"]
        )


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


def _orm_table_errors(connection, name, *, strict=True):
    return compare_table_schema(
        model_table_schema(Base.metadata.tables[name], connection.dialect),
        inspected_table_schema(sa.inspect(connection), name),
        strict=strict,
    )


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


@pytest.mark.parametrize(("legacy_comment", "owned"), [
    (reconciliation.CREATED_MARKER, True),
    ("prefix " + reconciliation.CREATED_MARKER, False),
    (reconciliation.CREATED_MARKER + " trailing audit text", False),
    (
        "Legacy table; quoted audit example follows:\n"
        + reconciliation.CREATED_MARKER
        + "\nNot owned by this migration.",
        False,
    ),
    ("legacy table", False),
    (None, False),
])
def test_downgrade_requires_exact_ownership_comment(
    migration_connection, legacy_comment, owned,
):
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
    if owned:
        assert not set(TABLES).intersection(sa.inspect(connection).get_table_names())
    else:
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


def test_expected_orm_check_is_represented_and_compatible(migration_connection):
    connection = migration_connection
    Base.metadata.tables["company_public_facts"].create(connection)
    expected = model_table_schema(
        Base.metadata.tables["company_public_facts"], connection.dialect,
    )
    assert expected["check_constraints"] == [{
        "name": "ck_company_public_fact_type",
        "sqltext": _canonical_check(
            "fact_type IN ('website', 'phone', 'email', 'registered_address', "
            "'factual_address', 'postal_address', 'mass_address', 'mass_director', "
            "'mass_founder', 'public_bank_details', 'related_company')"
        ),
    }]
    errors, _ = _orm_table_errors(connection, "company_public_facts")
    assert not errors, errors


def test_missing_expected_orm_check_fails(migration_connection):
    connection = migration_connection
    Base.metadata.tables["company_public_facts"].create(connection)
    connection.exec_driver_sql(
        "ALTER TABLE company_public_facts "
        "DROP CONSTRAINT ck_company_public_fact_type"
    )
    errors, _ = _orm_table_errors(connection, "company_public_facts")
    assert any(
        error["kind"] == "missing_or_incompatible_check_constraint"
        and error["path"] == "ck_company_public_fact_type"
        for error in errors
    )


def test_wrong_expected_orm_check_fails(migration_connection):
    connection = migration_connection
    Base.metadata.tables["company_public_facts"].create(connection)
    connection.exec_driver_sql(
        "ALTER TABLE company_public_facts "
        "DROP CONSTRAINT ck_company_public_fact_type"
    )
    connection.exec_driver_sql(
        "ALTER TABLE company_public_facts ADD CONSTRAINT "
        "ck_company_public_fact_type CHECK (fact_type <> '')"
    )
    errors, _ = _orm_table_errors(connection, "company_public_facts")
    assert any(
        error["kind"] == "missing_or_incompatible_check_constraint"
        and error["path"] == "ck_company_public_fact_type"
        for error in errors
    )


def test_extra_restrictive_check_fails(migration_connection):
    connection = migration_connection
    reconciliation.upgrade()
    connection.exec_driver_sql(
        "ALTER TABLE company_revenue_expense_snapshots ADD CONSTRAINT "
        "ck_profit_loss_nonnegative CHECK (profit_loss >= 0)"
    )
    errors, _ = _orm_table_errors(
        connection, "company_revenue_expense_snapshots"
    )
    assert any(
        error["kind"] == "unexpected_check_constraint"
        and error["path"] == "ck_profit_loss_nonnegative"
        for error in errors
    )


def test_extra_restrictive_unique_fails(migration_connection):
    connection = migration_connection
    reconciliation.upgrade()
    connection.exec_driver_sql(
        "ALTER TABLE company_revenue_expense_snapshots ADD CONSTRAINT "
        "uq_revexp_company_only UNIQUE (company_id)"
    )
    errors, _ = _orm_table_errors(
        connection, "company_revenue_expense_snapshots"
    )
    assert any(
        error["kind"] == "unexpected_unique_constraint"
        and error["path"] == "uq_revexp_company_only"
        for error in errors
    )


def test_missing_expected_unique_fails(migration_connection):
    connection = migration_connection
    reconciliation.upgrade()
    connection.exec_driver_sql(
        "ALTER TABLE company_revenue_expense_snapshots DROP CONSTRAINT "
        "uq_company_revexp_company_dataset_date"
    )
    errors, _ = _orm_table_errors(
        connection, "company_revenue_expense_snapshots"
    )
    assert any(
        error["kind"] == "missing_or_incompatible_unique_constraint"
        and error["path"] == "uq_company_revexp_company_dataset_date"
        for error in errors
    )


def test_wrong_foreign_key_action_fails(migration_connection):
    connection = migration_connection
    reconciliation.upgrade()
    connection.exec_driver_sql(
        "ALTER TABLE company_revenue_expense_snapshots DROP CONSTRAINT "
        "company_revenue_expense_snapshots_company_id_fkey"
    )
    connection.exec_driver_sql(
        "ALTER TABLE company_revenue_expense_snapshots ADD CONSTRAINT "
        "company_revenue_expense_snapshots_company_id_fkey "
        "FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE RESTRICT"
    )
    errors, _ = _orm_table_errors(
        connection, "company_revenue_expense_snapshots"
    )
    kinds = {error["kind"] for error in errors}
    assert "missing_or_incompatible_foreign_key" in kinds
    assert "unexpected_foreign_key" in kinds


def test_unique_constraint_and_unique_index_are_equivalent(migration_connection):
    connection = migration_connection
    reconciliation.upgrade()
    connection.exec_driver_sql(
        "ALTER TABLE company_tax_regime_snapshots DROP CONSTRAINT "
        "uq_company_tax_regime_company_dataset_date"
    )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_company_tax_regime_company_dataset_date "
        "ON company_tax_regime_snapshots (company_id, dataset_id, data_date)"
    )
    errors, observations = _orm_table_errors(
        connection, "company_tax_regime_snapshots"
    )
    assert not errors, errors
    assert any(
        item["kind"] == "equivalent_unique_index"
        and item["path"] == "uq_company_tax_regime_company_dataset_date"
        for item in observations
    )


def test_pytest_rejects_historical_database_before_collection():
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "DATABASE_URL": "postgresql+psycopg://localhost/kontragent"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_schema_reconciliation.py"],
        cwd=root, env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "Refusing pytest against historical database" in result.stderr
