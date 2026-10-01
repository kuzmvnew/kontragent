from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts.schema_fingerprint import (
    EXPECTED_POSTGRESQL_VERSION,
    compare_fingerprints,
    fingerprint_connection,
)
from scripts.staging_acceptance import DatabaseFactory, false_head_probe


def _sqlalchemy_url(variable: str) -> str:
    if os.getenv("STAGING_PARITY_POSTGRES") != "1":
        pytest.skip("live staging parity probes require explicit opt-in")
    value = os.getenv(variable)
    if not value:
        pytest.skip(f"{variable} is not configured")
    return value


def _url(variable: str) -> str:
    return _sqlalchemy_url(variable).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def test_database_factory_fresh_database_url_keeps_psycopg_driver():
    source = make_url(_sqlalchemy_url("DATABASE_URL"))
    admin_url = source.set(database="postgres").render_as_string(hide_password=False)
    token = uuid.uuid4().hex[:10]
    factory = DatabaseFactory(admin_url, token)
    expected_name = f"sp01_{token}_driver"

    try:
        created_url = factory.create("driver")
        assert created_url == source.set(database=expected_name).render_as_string(
            hide_password=False
        )
        assert make_url(created_url).drivername == "postgresql+psycopg"
        engine = create_engine(created_url)
        try:
            with engine.connect() as connection:
                observed_name = connection.execute(
                    text("SELECT current_database()")
                ).scalar_one()
                assert observed_name == expected_name
        finally:
            engine.dispose()
    finally:
        factory.drop_all()


@pytest.mark.parametrize("variable", ("DATABASE_URL", "PUBLIC_IMPORT_DATABASE_URL"))
def test_postgresql_18_6_fingerprint_determinism_and_false_head(variable):
    database_url = _url(variable)
    with psycopg.connect(database_url) as connection:
        first = fingerprint_connection(connection)
        second = fingerprint_connection(connection)

    assert first == second
    assert first["payload"]["postgresql_version"].split()[0] == EXPECTED_POSTGRESQL_VERSION
    assert first["payload"]["migration_revisions"]
    proof = false_head_probe(database_url, first)
    assert set(proof) == {"table", "column", "index", "constraint"}
    assert all(value["revision_unchanged"] for value in proof.values())


@pytest.mark.parametrize("variable", ("DATABASE_URL", "PUBLIC_IMPORT_DATABASE_URL"))
def test_postgresql_18_6_backup_restore_preserves_fingerprint(variable, tmp_path: Path):
    database_url = _url(variable)
    container = os.getenv("POSTGRES_SERVICE_CONTAINER")
    if not container:
        pytest.skip("PostgreSQL service container is not configured")
    source = make_url(database_url)
    restore_name = f"sp01_ci_restore_{uuid.uuid4().hex[:12]}"
    admin_url = source.set(database="postgres").render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restore_name))
        )
    try:
        with psycopg.connect(database_url) as connection:
            expected = fingerprint_connection(connection)
        backup = tmp_path / f"{source.database}.dump"
        with backup.open("wb") as stream:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--username=staging",
                    str(source.database),
                ],
                stdout=stream,
                stderr=subprocess.PIPE,
                check=False,
            )
        assert result.returncode == 0, result.stderr.decode()
        with backup.open("rb") as stream:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "pg_restore",
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                    "--username=staging",
                    f"--dbname={restore_name}",
                ],
                stdin=stream,
                stderr=subprocess.PIPE,
                check=False,
            )
        assert result.returncode == 0, result.stderr.decode()
        restore_url = source.set(database=restore_name).render_as_string(
            hide_password=False
        )
        with psycopg.connect(restore_url) as connection:
            observed = fingerprint_connection(connection)
        comparison = compare_fingerprints(expected, observed)
        assert comparison["compatible"], comparison["differences"]
    finally:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (restore_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(restore_name)
                )
            )
