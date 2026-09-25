from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from public_app.contracts import ReleaseManifest
from scripts.import_public_release import import_release
from scripts.public_release_common import canonical_json, write_checksums
from scripts.rollback_public_release import rollback_release
from tests.public_test_support import forty_projections


TEST_URL = os.getenv("PUBLIC_TEST_DATABASE_URL")
WEB_TEST_URL = os.getenv("PUBLIC_TEST_WEB_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="PUBLIC_TEST_DATABASE_URL is not configured")


def bundle(tmp_path: Path, release_id: str, previous: str | None = None) -> Path:
    path = tmp_path / release_id
    path.mkdir()
    projections = forty_projections(release_id)
    with gzip.open(path / "companies.jsonl.gz", "wt", encoding="utf-8", newline="\n") as stream:
        for item in projections:
            stream.write(canonical_json(item.model_dump(mode="json")).decode() + "\n")
    now = datetime(2026, 9, 25, 7, 0, tzinfo=timezone.utc)
    manifest = ReleaseManifest(
        schema_version="public-projection-v1", release_id=release_id,
        source_main_sha="9b00e84ac5c81ae405e191e614a29ac35182c6c3",
        previous_release_id=previous, created_at=now, result_date=now.date(),
        content_updated_at=now, record_count=40, companies_file="companies.jsonl.gz",
    )
    (path / "manifest.json").write_bytes(canonical_json(manifest.model_dump(mode="json")) + b"\n")
    write_checksums(path)
    return path


@pytest.fixture(autouse=True)
def clean_public_database():
    with psycopg.connect(TEST_URL) as connection:
        connection.execute("DELETE FROM public_publication_state")
        connection.execute("DELETE FROM public_company_projections")
        connection.execute("DELETE FROM public_releases")
        connection.execute("INSERT INTO public_publication_state(singleton, active_release_id) VALUES(TRUE,NULL)")
    yield


def active_release() -> str | None:
    with psycopg.connect(TEST_URL) as connection:
        return connection.execute("SELECT active_release_id FROM public_publication_state WHERE singleton=TRUE").fetchone()[0]


def test_atomic_import_and_idempotent_repeat(tmp_path):
    first = bundle(tmp_path, "public-v1-test-a")
    with psycopg.connect(TEST_URL) as connection:
        result = import_release(connection, first)
    assert result["record_count"] == 40
    assert active_release() == "public-v1-test-a"
    with psycopg.connect(TEST_URL) as connection:
        repeated = import_release(connection, first)
    assert repeated["idempotent"] is True
    assert repeated["active"] is True


def test_checksum_corruption_preserves_active_release(tmp_path):
    first = bundle(tmp_path, "public-v1-test-a")
    second = bundle(tmp_path, "public-v1-test-b", previous="public-v1-test-a")
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, first)
    with (second / "companies.jsonl.gz").open("ab") as stream:
        stream.write(b"corruption")
    with psycopg.connect(TEST_URL) as connection, pytest.raises(ValueError, match="checksum mismatch"):
        import_release(connection, second)
    assert active_release() == "public-v1-test-a"


def test_failed_staged_import_rolls_back_and_preserves_active_release(tmp_path):
    first = bundle(tmp_path, "public-v1-test-a")
    second = bundle(tmp_path, "public-v1-test-b", previous="public-v1-wrong-parent")
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, first)
        connection.execute(
            """INSERT INTO public_releases(
                   release_id,schema_version,source_main_sha,created_at,
                   record_count,manifest_sha256,status
               ) VALUES(%s,'public-projection-v1',%s,now(),40,%s,'staged')""",
            (
                "public-v1-wrong-parent",
                "9b00e84ac5c81ae405e191e614a29ac35182c6c3",
                "0" * 64,
            ),
        )
    with psycopg.connect(TEST_URL) as connection, pytest.raises(ValueError, match="previous_release_id"):
        import_release(connection, second)
    assert active_release() == "public-v1-test-a"
    with psycopg.connect(TEST_URL) as connection:
        assert connection.execute(
            "SELECT count(*) FROM public_releases WHERE release_id='public-v1-test-b'"
        ).fetchone()[0] == 0


def test_second_atomic_switch_and_exact_rollback(tmp_path):
    first = bundle(tmp_path, "public-v1-test-a")
    second = bundle(tmp_path, "public-v1-test-b", previous="public-v1-test-a")
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, first)
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, second)
    assert active_release() == "public-v1-test-b"
    with psycopg.connect(TEST_URL) as connection, pytest.raises(ValueError, match="exact previous"):
        rollback_release(connection, "public-v1-not-previous")
    assert active_release() == "public-v1-test-b"
    with psycopg.connect(TEST_URL) as connection:
        result = rollback_release(connection, "public-v1-test-a")
    assert result["active_release_id"] == "public-v1-test-a"
    assert active_release() == "public-v1-test-a"


def test_public_web_role_is_read_only(tmp_path):
    if not WEB_TEST_URL:
        pytest.skip("PUBLIC_TEST_WEB_DATABASE_URL is not configured")
    first = bundle(tmp_path, "public-v1-test-read-role")
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, first)
    with psycopg.connect(WEB_TEST_URL) as connection:
        assert connection.execute("SELECT count(*) FROM public_company_projections").fetchone()[0] == 40
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("UPDATE public_publication_state SET updated_at=now()")
