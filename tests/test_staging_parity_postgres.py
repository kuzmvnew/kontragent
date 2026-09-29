from __future__ import annotations

import os

import psycopg
import pytest

from scripts.schema_fingerprint import (
    EXPECTED_POSTGRESQL_VERSION,
    fingerprint_connection,
)
from scripts.staging_acceptance import false_head_probe


def _url(variable: str) -> str:
    if os.getenv("STAGING_PARITY_POSTGRES") != "1":
        pytest.skip("live staging parity probes require explicit opt-in")
    value = os.getenv(variable)
    if not value:
        pytest.skip(f"{variable} is not configured")
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


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
