import json
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.database.base import Base
from app.database.postgres import engine
from scripts import reconcile_legacy_v3_schema as reconciliation


def _database_name(connection):
    return connection.scalar(sa.text("SELECT current_database()"))


def _apply(connection):
    identity = reconciliation.database_identity(connection)
    return reconciliation.apply_reconciliation(
        connection,
        confirm_database=identity["database"],
        confirm_host=identity["host"],
        confirm_port=identity["port"],
    )


def _set_revision(connection, revision):
    connection.execute(sa.text("DELETE FROM public.alembic_version"))
    connection.execute(
        sa.text("INSERT INTO public.alembic_version (version_num) VALUES (:revision)"),
        {"revision": revision},
    )


def _company(connection):
    value = connection.scalar(sa.text("SELECT id FROM public.companies LIMIT 1"))
    if value is not None:
        return value
    return connection.scalar(
        sa.text(
            "INSERT INTO public.companies (inn, name, entity_type) "
            "VALUES (:inn, 'DEV-005 fixture', 'legal') RETURNING id"
        ),
        {"inn": str(uuid4().int)[:12]},
    )


def _second_company(connection):
    return connection.scalar(
        sa.text(
            "INSERT INTO public.companies (inn, name, entity_type) "
            "VALUES (:inn, 'DEV-005 fixture second', 'legal') RETURNING id"
        ),
        {"inn": str(uuid4().int)[:12]},
    )


def _seed_legacy(
    connection,
    *,
    schema="public",
    company_id=None,
    assessment_id="00000000-0000-0000-0000-000000000001",
    summary_id="10000000-0000-0000-0000-000000000001",
    input_hash="a" * 64,
    summary_company_id=None,
    risk_assessment_id=None,
    payload_marker="fixture-secret-value",
):
    company_id = company_id or _company(connection)
    summary_company_id = summary_company_id or company_id
    risk_assessment_id = risk_assessment_id or assessment_id
    prefix = f'"{schema}".'
    connection.execute(
        sa.text(
            f"""
            INSERT INTO {prefix}company_risk_assessments_v3
              (assessment_id, company_id, input_hash, risk_engine_version,
               coverage_engine_version, calculated_at, risk_score, risk_label,
               normalized_results, coverage, result_payload)
            VALUES
              (:assessment_id, :company_id, :input_hash, 'risk-engine-test',
               'coverage-engine-test', now(), 17, 'MEDIUM',
               CAST(:normalized_results AS JSONB), CAST(:coverage AS JSONB),
               CAST(:result_payload AS JSONB))
            """
        ),
        {
            "assessment_id": assessment_id,
            "company_id": company_id,
            "input_hash": input_hash,
            "normalized_results": json.dumps({"marker": payload_marker}),
            "coverage": json.dumps({"coverage_score": 100}),
            "result_payload": json.dumps({"label": "MEDIUM"}),
        },
    )
    connection.execute(
        sa.text(
            f"""
            INSERT INTO {prefix}company_summaries_v3
              (summary_id, company_id, risk_assessment_id,
               summary_engine_version, generated_at, structured_payload)
            VALUES
              (:summary_id, :company_id, :risk_assessment_id,
               'summary-engine-test', now(), CAST(:structured_payload AS JSONB))
            """
        ),
        {
            "summary_id": summary_id,
            "company_id": summary_company_id,
            "risk_assessment_id": risk_assessment_id,
            "structured_payload": json.dumps({"conclusion": payload_marker}),
        },
    )
    return company_id


def _create_legacy(connection, *, schema="public", seed=True):
    if schema != "public":
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    risk, summary = reconciliation.legacy_tables(schema=schema)
    risk.create(connection)
    summary.create(connection)
    if seed:
        _seed_legacy(connection, schema=schema)


@pytest.fixture
def transaction_connection():
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            assert _database_name(connection) != reconciliation.PROTECTED_DATABASE
            assert reconciliation.current_revisions(connection) == [
                reconciliation.CURRENT_SCHEMA_HEAD
            ]
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def legacy_connection(transaction_connection):
    connection = transaction_connection
    Base.metadata.tables[reconciliation.SUMMARY_TABLE].drop(connection)
    Base.metadata.tables[reconciliation.RISK_TABLE].drop(connection)
    _set_revision(connection, reconciliation.LEGACY_REVISION)
    _create_legacy(connection)
    return connection


@pytest.fixture
def protocol_postgres_major(transaction_connection, monkeypatch):
    """Exercise the protocol on CI's disposable DB, independently of rollout support.

    Only protocol tests opt in. PG18 executes the unchanged production guard;
    other test servers temporarily use their actual major. Guard tests never
    request this fixture, and monkeypatch restores the constant after each test.
    """
    assert reconciliation.EXPECTED_POSTGRES_MAJOR == 18
    identity = reconciliation.database_identity(transaction_connection)
    assert identity["database"] != reconciliation.PROTECTED_DATABASE
    major = identity["postgresql_version_num"] // 10000
    if major != 18:
        monkeypatch.setattr(reconciliation, "EXPECTED_POSTGRES_MAJOR", major)


def test_blank_install_is_canonical_head_and_unchanged(transaction_connection):
    connection = transaction_connection
    before = reconciliation.canonical_fingerprints(connection)
    state = reconciliation.classify_state(connection)
    after = reconciliation.canonical_fingerprints(connection)
    assert state["state"] == "NO_OP_PASS"
    assert reconciliation.current_revisions(connection) == [
        reconciliation.CURRENT_SCHEMA_HEAD
    ]
    assert before == after


def test_canonical_parent_to_exact_target_passes(transaction_connection):
    connection = transaction_connection
    Base.metadata.tables[reconciliation.SUMMARY_TABLE].drop(connection)
    Base.metadata.tables[reconciliation.RISK_TABLE].drop(connection)
    _set_revision(connection, reconciliation.CANONICAL_PARENT)
    reconciliation._apply_canonical_revision(connection)
    assert reconciliation.current_revisions(connection) == [
        reconciliation.CANONICAL_TARGET
    ]
    assert all(
        value["exact"]
        for value in reconciliation.canonical_fingerprints(connection).values()
    )


def test_recognized_exact_legacy_preflight_is_eligible(legacy_connection):
    state = reconciliation.classify_state(legacy_connection)
    assert state["state"] == "ELIGIBLE_FOR_RECONCILIATION"
    assert state["full_pre_v3_compatibility"]["compatible"] is True
    assert all(
        value["exact"] for value in state["public_legacy_fingerprints"].values()
    )


def test_wrong_revision_fails_closed(legacy_connection):
    _set_revision(legacy_connection, reconciliation.CANONICAL_PARENT)
    state = reconciliation.classify_state(legacy_connection)
    assert state["state"] == "BLOCKED_UNRECOGNIZED_LEGACY_STATE"


def test_legacy_missing_column_fails_closed(legacy_connection):
    legacy_connection.exec_driver_sql(
        "ALTER TABLE public.company_risk_assessments_v3 DROP COLUMN risk_label"
    )
    state = reconciliation.classify_state(legacy_connection)
    assert state["state"].startswith("BLOCKED_")
    assert any(
        error["kind"] == "missing_column"
        for error in state["public_legacy_fingerprints"]["risk"]["errors"]
    )


def test_mixed_legacy_and_canonical_shape_fails_closed(legacy_connection):
    connection = legacy_connection
    reconciliation.legacy_tables()[1].drop(connection)
    Base.metadata.tables[reconciliation.SUMMARY_TABLE].create(connection)
    state = reconciliation.classify_state(connection)
    assert state["state"] == "BLOCKED_PARTIAL_RECONCILIATION_STATE"


def test_unexpected_semantic_constraint_fails_closed(legacy_connection):
    legacy_connection.exec_driver_sql(
        "ALTER TABLE public.company_risk_assessments_v3 ADD CONSTRAINT "
        "ck_unexpected_score CHECK (risk_score >= 0)"
    )
    state = reconciliation.classify_state(legacy_connection)
    errors = state["public_legacy_fingerprints"]["risk"]["errors"]
    assert state["state"].startswith("BLOCKED_")
    assert any(error["kind"] == "unexpected_check_constraint" for error in errors)


def test_unvalidated_constraint_fails_exact_fingerprint(legacy_connection):
    connection = legacy_connection
    connection.exec_driver_sql(
        "ALTER TABLE public.company_risk_assessments_v3 ADD CONSTRAINT "
        "ck_unvalidated_score CHECK (risk_score >= 0) NOT VALID"
    )
    fingerprint = reconciliation.legacy_fingerprints(
        connection, schema="public"
    )["risk"]
    assert fingerprint["exact"] is False
    assert any(error["kind"] == "unvalidated_constraint" for error in fingerprint["errors"])


def test_canonical_target_is_no_op(transaction_connection):
    assert reconciliation.classify_state(transaction_connection)["state"] == "NO_OP_PASS"


def test_canonical_target_with_valid_archive_is_no_op_verify(transaction_connection):
    connection = transaction_connection
    _create_legacy(connection, schema=reconciliation.ARCHIVE_SCHEMA)
    state = reconciliation.classify_state(connection)
    assert state["state"] == "NO_OP_VERIFY_PASS"
    assert all(value["exact"] for value in state["archive_fingerprints"].values())


def test_partial_archive_state_is_blocked(transaction_connection):
    connection = transaction_connection
    connection.exec_driver_sql(
        f'CREATE SCHEMA "{reconciliation.ARCHIVE_SCHEMA}"'
    )
    state = reconciliation.classify_state(connection)
    assert state["state"] == "BLOCKED_PARTIAL_RECONCILIATION_STATE"


@pytest.mark.usefixtures("protocol_postgres_major")
def test_archive_is_never_overwritten(transaction_connection):
    connection = transaction_connection
    _create_legacy(connection, schema=reconciliation.ARCHIVE_SCHEMA)
    before = reconciliation.legacy_row_inventory(
        connection, schema=reconciliation.ARCHIVE_SCHEMA
    )
    manifest = _apply(connection)
    after = reconciliation.legacy_row_inventory(
        connection, schema=reconciliation.ARCHIVE_SCHEMA
    )
    assert manifest["result"] == "NO_OP_VERIFY_PASS"
    assert before == after


@pytest.mark.usefixtures("protocol_postgres_major")
def test_apply_preserves_legacy_v1_business_rows_and_installs_exact_canonical(
    legacy_connection,
):
    connection = legacy_connection
    before = reconciliation.legacy_row_inventory(connection, schema="public")
    manifest = _apply(connection)
    after = reconciliation.legacy_row_inventory(
        connection, schema=reconciliation.ARCHIVE_SCHEMA
    )
    assert manifest["result"] == "RECONCILED_PASS"
    assert before == after
    assert before["risk"]["total_count"] == after["risk"]["total_count"]
    assert before["summary"]["total_count"] == after["summary"]["total_count"]
    assert before["risk"]["id_inventory_sha256"] == after["risk"]["id_inventory_sha256"]
    assert before["summary"]["id_inventory_sha256"] == after["summary"]["id_inventory_sha256"]
    assert before["risk"]["payload_sha256"] == after["risk"]["payload_sha256"]
    assert before["summary"]["payload_sha256"] == after["summary"]["payload_sha256"]
    assert manifest["archive_move"]["verified"] is True
    assert manifest["metadata_adoption"] == {
        "target": reconciliation.CANONICAL_PARENT,
        "executed_after_archive_verification": True,
        "result": "PASS",
    }
    assert manifest["canonical_migration"]["final_revision"] == [
        reconciliation.CANONICAL_TARGET
    ]
    assert manifest["v1"]["preserved"] is True
    assert manifest["other_business_tables"]["preserved"] is True
    assert all(
        value["exact"]
        for value in manifest["final_canonical_fingerprints"].values()
    )
    assert all(
        value["exact"]
        for value in manifest["final_archive_fingerprints"].values()
    )
    assert manifest["final_schema_completeness"]["compatible"] is True

    archive_inspector = sa.inspect(connection)
    summary_fk = archive_inspector.get_foreign_keys(
        reconciliation.SUMMARY_TABLE, schema=reconciliation.ARCHIVE_SCHEMA
    )
    assert any(
        fk["referred_schema"] == reconciliation.ARCHIVE_SCHEMA
        and fk["referred_table"] == reconciliation.RISK_TABLE
        for fk in summary_fk
    )
    for fingerprint in manifest["final_archive_fingerprints"].values():
        assert fingerprint["owned_sequences"][0]["sequence_schema"] == (
            reconciliation.ARCHIVE_SCHEMA
        )


@pytest.mark.usefixtures("protocol_postgres_major")
def test_historical_anomalies_are_preserved_and_recorded(legacy_connection):
    connection = legacy_connection
    company_id = connection.scalar(
        sa.text("SELECT company_id FROM public.company_risk_assessments_v3 LIMIT 1")
    )
    other_company = _second_company(connection)
    try:
        connection.exec_driver_sql("SET LOCAL session_replication_role = replica")
    except sa.exc.DBAPIError as error:
        pytest.skip(f"PostgreSQL role cannot create the historical anomaly fixture: {error}")
    _seed_legacy(
        connection,
        company_id=company_id,
        summary_company_id=other_company,
        assessment_id="00000000-0000-0000-0000-000000000002",
        summary_id="10000000-0000-0000-0000-000000000002",
        input_hash="a" * 64,
    )
    connection.execute(
        sa.text(
            "INSERT INTO public.company_summaries_v3 "
            "(summary_id, company_id, risk_assessment_id, summary_engine_version, "
            "generated_at, structured_payload) VALUES "
            "('10000000-0000-0000-0000-000000000003', :company_id, "
            "'missing-risk-assessment', 'summary-engine-test', now(), '{}'::jsonb)"
        ),
        {"company_id": company_id},
    )
    connection.exec_driver_sql("SET LOCAL session_replication_role = origin")
    before = reconciliation.legacy_row_inventory(connection, schema="public")
    assert before["cross_table"]["orphan_summary_count"] == 1
    assert before["cross_table"]["company_mismatch_count"] == 1
    assert before["risk"]["duplicate_reuse_identities"]["groups"] == 1
    manifest = _apply(connection)
    assert manifest["legacy_rows"]["before"] == manifest["legacy_rows"]["archive"]
    assert manifest["legacy_rows"]["archive"]["cross_table"][
        "orphan_summary_count"
    ] == 1


@pytest.mark.usefixtures("protocol_postgres_major")
def test_parent_compatibility_failure_prevents_metadata_adoption(legacy_connection):
    connection = legacy_connection
    assert reconciliation.classify_state(connection)["state"] == (
        "ELIGIBLE_FOR_RECONCILIATION"
    )
    connection.exec_driver_sql(
        "ALTER TABLE public.companies ADD CONSTRAINT "
        "ck_dev005_unexpected CHECK (id > 0)"
    )
    state = reconciliation.classify_state(connection)
    assert state["full_pre_v3_compatibility"]["compatible"] is False
    assert any(
        error["table"] == "companies"
        and error["kind"] == "unexpected_check_constraint"
        and error["path"] == "ck_dev005_unexpected"
        for error in state["full_pre_v3_compatibility"]["errors"]
    )
    with pytest.raises(
        reconciliation.ReconciliationBlocked,
        match="^BLOCKED_UNRECOGNIZED_LEGACY_STATE$",
    ):
        _apply(connection)
    assert reconciliation.current_revisions(connection) == [
        reconciliation.LEGACY_REVISION
    ]
    assert not reconciliation._schema_exists(
        connection, reconciliation.ARCHIVE_SCHEMA
    )


def test_production_apply_version_guard_on_live_eligible_legacy(legacy_connection):
    # No protocol fixture: prove the real apply entry point still blocks PG17,
    # and executes the supported production path on PG18.
    assert reconciliation.EXPECTED_POSTGRES_MAJOR == 18
    connection = legacy_connection
    state = reconciliation.classify_state(connection)
    assert state["state"] == "ELIGIBLE_FOR_RECONCILIATION"
    major = state["database_identity"]["postgresql_version_num"] // 10000
    if major == 18:
        assert _apply(connection)["result"] == "RECONCILED_PASS"
    else:
        with pytest.raises(
            reconciliation.ReconciliationBlocked,
            match=f"^PostgreSQL major mismatch: expected 18, observed {major}\\.",
        ):
            _apply(connection)
        assert reconciliation.current_revisions(connection) == [
            reconciliation.LEGACY_REVISION
        ]
        assert not reconciliation._schema_exists(
            connection, reconciliation.ARCHIVE_SCHEMA
        )


@pytest.mark.parametrize(("allow_protected_database", "rollout_authorization"), [
    (False, None),
    (True, None),
    (False, reconciliation.PROTECTED_DATABASE),
    (True, "wrong-database"),
])
def test_working_database_apply_guard_requires_two_explicit_authorizations(
    allow_protected_database, rollout_authorization,
):
    assert reconciliation.EXPECTED_POSTGRES_MAJOR == 18
    identity = {
        "database": reconciliation.PROTECTED_DATABASE,
        "host": "127.0.0.1",
        "port": 5432,
        "postgresql_version": "18.6",
        "postgresql_version_num": 180006,
    }
    with pytest.raises(reconciliation.ReconciliationBlocked, match="forbidden"):
        reconciliation.enforce_apply_guard(
            identity,
            confirm_database=reconciliation.PROTECTED_DATABASE,
            confirm_host="127.0.0.1",
            confirm_port=5432,
            allow_protected_database=allow_protected_database,
            rollout_authorization=rollout_authorization,
        )
    reconciliation.enforce_apply_guard(
        identity,
        confirm_database=reconciliation.PROTECTED_DATABASE,
        confirm_host="127.0.0.1",
        confirm_port=5432,
        allow_protected_database=True,
        rollout_authorization=reconciliation.PROTECTED_DATABASE,
    )


def test_apply_guard_rejects_wrong_confirmation_and_postgresql_major():
    assert reconciliation.EXPECTED_POSTGRES_MAJOR == 18
    identity = {
        "database": "clone",
        "host": "127.0.0.1",
        "port": 5432,
        "postgresql_version": "18.6",
        "postgresql_version_num": 180006,
    }
    reconciliation.enforce_apply_guard(
        identity,
        confirm_database="clone",
        confirm_host="127.0.0.1",
        confirm_port=5432,
        allow_protected_database=False,
        rollout_authorization=None,
    )
    with pytest.raises(reconciliation.ReconciliationBlocked, match="confirm-database"):
        reconciliation.enforce_apply_guard(
            identity,
            confirm_database="other",
            confirm_host="127.0.0.1",
            confirm_port=5432,
            allow_protected_database=False,
            rollout_authorization=None,
        )
    with pytest.raises(reconciliation.ReconciliationBlocked, match="confirm-host"):
        reconciliation.enforce_apply_guard(
            identity,
            confirm_database="clone",
            confirm_host="localhost",
            confirm_port=5432,
            allow_protected_database=False,
            rollout_authorization=None,
        )
    with pytest.raises(reconciliation.ReconciliationBlocked, match="confirm-port"):
        reconciliation.enforce_apply_guard(
            identity,
            confirm_database="clone",
            confirm_host="127.0.0.1",
            confirm_port=5433,
            allow_protected_database=False,
            rollout_authorization=None,
        )
    identity["postgresql_version_num"] = 170009
    identity["postgresql_version"] = "17.9"
    with pytest.raises(
        reconciliation.ReconciliationBlocked,
        match=r"^PostgreSQL major mismatch: expected 18, observed 17\.9$",
    ):
        reconciliation.enforce_apply_guard(
            identity,
            confirm_database="clone",
            confirm_host="127.0.0.1",
            confirm_port=5432,
            allow_protected_database=False,
            rollout_authorization=None,
        )


def test_default_cli_mode_is_read_only_inspect():
    assert reconciliation.parse_args([]).action == "inspect"


def test_manifest_contains_hashes_but_excludes_full_business_payload(
    legacy_connection, tmp_path
):
    manifest = reconciliation.inspect_connection(
        legacy_connection, action="inspect"
    )
    path = reconciliation.write_manifest(manifest, tmp_path / "manifest.json")
    serialized = path.read_text(encoding="utf-8")
    assert "fixture-secret-value" not in serialized
    assert manifest["legacy_rows"]["risk"]["payload_sha256"]
    assert manifest["legacy_rows"]["summary"]["payload_sha256"]
