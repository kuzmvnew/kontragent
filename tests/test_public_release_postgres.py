from __future__ import annotations

import gzip
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from public_app.contracts import PublicProjection, ReleaseManifest
from public_app.repository import PublicRepository
from public_app.seo import (
    SEO_COMPILER_VERSION,
    IdentityEvidenceState,
    SeoDecision,
    SeoEligibilityContext,
    compile_seo_projection,
)
from scripts.import_public_release import import_release
from scripts.public_release_common import canonical_json, write_checksums
from scripts.rollback_public_release import rollback_release
from tests.public_test_support import forty_projections

TEST_URL = os.getenv("PUBLIC_TEST_DATABASE_URL")
WEB_TEST_URL = os.getenv("PUBLIC_TEST_WEB_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="PUBLIC_TEST_DATABASE_URL is not configured")


def bundle(
    tmp_path: Path,
    release_id: str,
    previous: str | None = None,
    *,
    name_prefix: str = "ООО ТЕСТ",
) -> Path:
    path = tmp_path / release_id
    path.mkdir()
    projections = forty_projections(release_id)
    if name_prefix != "ООО ТЕСТ":
        renamed = []
        for item in projections:
            value = item.model_dump(mode="json")
            suffix = item.company.inn
            value["company"]["name"] = f"{name_prefix} {suffix}"
            value["company"]["full_name"] = f"{name_prefix} ПОЛНОЕ {suffix}"
            renamed.append(PublicProjection.model_validate(value))
        projections = renamed
    with gzip.open(path / "companies.jsonl.gz", "wt", encoding="utf-8", newline="\n") as stream:
        for item in projections:
            stream.write(canonical_json(item.model_dump(mode="json")).decode() + "\n")
    now = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
    manifest = ReleaseManifest(
        schema_version="public-projection-v1", release_id=release_id,
        source_main_sha="9b00e84ac5c81ae405e191e614a29ac35182c6c3",
        cohort_manifest_path="docs/releases/public-v1-cohort-40.json",
        cohort_manifest_sha256="1" * 64,
        cohort_source_main_sha="6dc86fdd2911d3e681a085fdd10e5665b0bcf855",
        previous_release_id=previous, created_at=now, result_date=now.date(),
        content_updated_at=now, record_count=40, companies_file="companies.jsonl.gz",
    )
    (path / "manifest.json").write_bytes(canonical_json(manifest.model_dump(mode="json")) + b"\n")
    write_checksums(path)
    return path


def authorize_seo_release(connection, release_id: str) -> None:
    """Test-only explicit cohort activation with complete persisted SEO rows."""

    rows = connection.execute(
        "SELECT inn, payload FROM public_company_projections WHERE release_id=%s",
        (release_id,),
    ).fetchall()
    for inn, payload in rows:
        projection = PublicProjection.model_validate(payload)
        seo = compile_seo_projection(
            projection,
            context=SeoEligibilityContext(
                active_revision_id=release_id,
                public_ready=True,
                released=True,
            ),
        )
        connection.execute(
            """
            UPDATE public_company_projections
            SET seo_projection=%s, seo_decision=%s, seo_compiler_version=%s,
                search_visible_hash=%s, non_identity_content_hash=%s,
                sitemap_shard=%s, seo_content_updated_at=%s
            WHERE release_id=%s AND inn=%s
            """,
            (
                Jsonb(seo.model_dump(mode="json")),
                seo.eligibility.decision.value,
                seo.compiler_version,
                seo.search_visible_hash,
                seo.non_identity_content_hash,
                seo.sitemap_shard,
                seo.content_updated_at,
                release_id,
                inn,
            ),
        )
    connection.execute(
        """
        UPDATE public_releases
        SET seo_contract_version=%s, seo_release_cohort=500, seo_released=TRUE
        WHERE release_id=%s
        """,
        (SEO_COMPILER_VERSION, release_id),
    )


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


def test_legacy_v1_reimport_after_v2_is_idempotent_without_backfill(tmp_path):
    release = bundle(tmp_path, "public-v1-legacy-repeat")
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, release)
        connection.execute(
            """
            UPDATE public_releases
            SET seo_contract_version=NULL, seo_release_cohort=NULL, seo_released=FALSE
            WHERE release_id='public-v1-legacy-repeat'
            """
        )
        connection.execute(
            """
            UPDATE public_company_projections
            SET seo_projection=NULL, seo_decision=NULL, seo_compiler_version=NULL,
                search_visible_hash=NULL, non_identity_content_hash=NULL,
                sitemap_shard=NULL, seo_content_updated_at=NULL
            WHERE release_id='public-v1-legacy-repeat'
            """
        )
    with psycopg.connect(TEST_URL) as connection:
        repeated = import_release(connection, release)
        remaining = connection.execute(
            """
            SELECT count(*) FILTER (WHERE seo_projection IS NOT NULL),
                   max(seo_contract_version)
            FROM public_company_projections p
            JOIN public_releases r USING (release_id)
            WHERE release_id='public-v1-legacy-repeat'
            """
        ).fetchone()
    assert repeated["idempotent"] is True
    assert remaining == (0, None)
    repository = PublicRepository(TEST_URL)
    snapshot = repository.get_company_page_snapshot(
        forty_projections("public-v1-legacy-repeat")[0].company.inn
    )
    assert snapshot is not None
    assert snapshot.stored_seo_valid is False
    assert snapshot.seo.robots == "noindex, follow"
    assert repository.sitemap_rows() == []
    assert repository.catalog_page(1) == ([], 0)


def test_active_public_release_without_seo_cohort_is_not_index_authority(tmp_path):
    release_id = "public-v2-no-seo-cohort"
    release = bundle(tmp_path, release_id)
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, release)
        inn = connection.execute(
            "SELECT min(inn) FROM public_company_projections WHERE release_id=%s",
            (release_id,),
        ).fetchone()[0]
    repository = PublicRepository(TEST_URL)
    snapshot = repository.get_company_page_snapshot(inn)
    assert snapshot is not None
    assert snapshot.seo_released is False
    assert snapshot.seo_release_cohort is None
    assert snapshot.seo.eligibility.decision == SeoDecision.NOT_PUBLISHED
    assert snapshot.seo.robots == "noindex, follow"
    assert repository.sitemap_rows() == []
    assert repository.catalog_page(1) == ([], 0)


def test_native_v2_reimport_verifies_derived_state_and_detects_tamper(tmp_path):
    release = bundle(tmp_path, "public-v2-native-repeat")
    with psycopg.connect(TEST_URL) as connection:
        imported = import_release(connection, release)
        repeated = import_release(connection, release)
        marker = connection.execute(
            "SELECT seo_contract_version FROM public_releases WHERE release_id=%s",
            ("public-v2-native-repeat",),
        ).fetchone()[0]
    assert imported["idempotent"] is False
    assert repeated["idempotent"] is True
    assert marker == SEO_COMPILER_VERSION

    with psycopg.connect(TEST_URL) as connection:
        connection.execute(
            """
            UPDATE public_company_projections
            SET search_visible_hash=%s
            WHERE release_id=%s
              AND inn=(SELECT min(inn) FROM public_company_projections WHERE release_id=%s)
            """,
            ("0" * 64, "public-v2-native-repeat", "public-v2-native-repeat"),
        )
    with psycopg.connect(TEST_URL) as connection, pytest.raises(
        ValueError, match="does not match repeated bundle"
    ):
        import_release(connection, release)


def test_partial_seo_storage_and_stored_noindex_are_fail_closed(tmp_path):
    release_id = "public-v2-partial-seo"
    release = bundle(tmp_path, release_id)
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, release)
        authorize_seo_release(connection, release_id)
        inns = [
            row[0]
            for row in connection.execute(
                "SELECT inn FROM public_company_projections WHERE release_id=%s ORDER BY inn",
                (release_id,),
            ).fetchall()
        ]
        missing_inn, noindex_inn = inns[:2]
        connection.execute(
            """
            UPDATE public_company_projections
            SET seo_projection=NULL, seo_decision=NULL, seo_compiler_version=NULL,
                search_visible_hash=NULL, non_identity_content_hash=NULL,
                sitemap_shard=NULL, seo_content_updated_at=NULL
            WHERE release_id=%s AND inn=%s
            """,
            (release_id, missing_inn),
        )
        projection = PublicProjection.model_validate(
            connection.execute(
                "SELECT payload FROM public_company_projections WHERE release_id=%s AND inn=%s",
                (release_id, noindex_inn),
            ).fetchone()[0]
        )
        noindex = compile_seo_projection(
            projection,
            context=SeoEligibilityContext(
                active_revision_id=release_id,
                public_ready=True,
                released=True,
                identity_state=IdentityEvidenceState.DISPUTED,
            ),
        )
        assert noindex.eligibility.decision == SeoDecision.NOINDEX_RECOVERABLE
        connection.execute(
            """
            UPDATE public_company_projections
            SET seo_projection=%s, seo_decision=%s, seo_compiler_version=%s,
                search_visible_hash=%s, non_identity_content_hash=%s,
                sitemap_shard=%s, seo_content_updated_at=%s
            WHERE release_id=%s AND inn=%s
            """,
            (
                Jsonb(noindex.model_dump(mode="json")),
                noindex.eligibility.decision.value,
                noindex.compiler_version,
                noindex.search_visible_hash,
                noindex.non_identity_content_hash,
                noindex.sitemap_shard,
                noindex.content_updated_at,
                release_id,
                noindex_inn,
            ),
        )

    repository = PublicRepository(TEST_URL)
    missing = repository.get_company_page_snapshot(missing_inn)
    stored_noindex = repository.get_company_page_snapshot(noindex_inn)
    assert missing is not None
    assert missing.stored_seo_valid is False
    assert missing.seo.eligibility.decision != SeoDecision.INDEX
    assert missing.seo.robots == "noindex, follow"
    assert stored_noindex is not None
    assert stored_noindex.stored_seo_valid is True
    assert stored_noindex.seo.eligibility.decision == SeoDecision.NOINDEX_RECOVERABLE
    assert compile_seo_projection(stored_noindex.projection).eligibility.decision == SeoDecision.INDEX

    sitemap_inns = {row["inn"] for row in repository.sitemap_rows()}
    catalog_inns = {
        item.company.inn
        for page in (1, 2)
        for item in repository.catalog_page(page)[0]
    }
    assert len(sitemap_inns) == len(catalog_inns) == 38
    assert {missing_inn, noindex_inn}.isdisjoint(sitemap_inns)
    assert {missing_inn, noindex_inn}.isdisjoint(catalog_inns)


def test_revision_mismatch_cannot_enter_page_sitemap_or_catalog(tmp_path):
    release_id = "public-v2-revision-mismatch"
    release = bundle(tmp_path, release_id)
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, release)
        authorize_seo_release(connection, release_id)
        inn, payload = connection.execute(
            """
            SELECT inn, seo_projection
            FROM public_company_projections
            WHERE release_id=%s ORDER BY inn LIMIT 1
            """,
            (release_id,),
        ).fetchone()
        payload["active_revision_id"] = "public-v2-other-revision"
        connection.execute(
            """
            UPDATE public_company_projections SET seo_projection=%s
            WHERE release_id=%s AND inn=%s
            """,
            (Jsonb(payload), release_id, inn),
        )

    repository = PublicRepository(TEST_URL)
    snapshot = repository.get_company_page_snapshot(inn)
    assert snapshot is not None
    assert snapshot.stored_seo_valid is False
    assert snapshot.seo.robots == "noindex, follow"
    assert inn not in {row["inn"] for row in repository.sitemap_rows()}
    assert inn not in {
        item.company.inn
        for page in (1, 2)
        for item in repository.catalog_page(page)[0]
    }


def test_active_release_switch_never_mixes_company_snapshot(tmp_path):
    first_id = "public-v2-race-a"
    second_id = "public-v2-race-b"
    first = bundle(tmp_path, first_id, name_prefix="ООО РЕВИЗИЯ А")
    second = bundle(tmp_path, second_id, previous=first_id, name_prefix="ООО РЕВИЗИЯ Б")
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, first)
    with psycopg.connect(TEST_URL) as connection:
        import_release(connection, second)
        authorize_seo_release(connection, first_id)
        authorize_seo_release(connection, second_id)
        inn = connection.execute(
            "SELECT min(inn) FROM public_company_projections WHERE release_id=%s",
            (first_id,),
        ).fetchone()[0]

    errors: list[BaseException] = []

    def switch_releases() -> None:
        try:
            with psycopg.connect(TEST_URL) as connection:
                for index in range(120):
                    target = first_id if index % 2 == 0 else second_id
                    with connection.transaction():
                        connection.execute(
                            "UPDATE public_releases SET status='staged' WHERE status='active'"
                        )
                        connection.execute(
                            "UPDATE public_releases SET status='active' WHERE release_id=%s",
                            (target,),
                        )
                        connection.execute(
                            """
                            UPDATE public_publication_state
                            SET active_release_id=%s, updated_at=now()
                            WHERE singleton=TRUE
                            """,
                            (target,),
                        )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=switch_releases)
    worker.start()
    repository = PublicRepository(TEST_URL)
    observed = set()
    for _ in range(240):
        snapshot = repository.get_company_page_snapshot(inn)
        assert snapshot is not None
        observed.add(snapshot.release_id)
        assert snapshot.release_id == snapshot.projection.publication.release_id
        assert snapshot.release_id == snapshot.seo.active_revision_id
        assert snapshot.seo.metadata.h1 == snapshot.projection.company.name
        if snapshot.release_id == first_id:
            assert snapshot.projection.company.name.startswith("ООО РЕВИЗИЯ А")
        else:
            assert snapshot.release_id == second_id
            assert snapshot.projection.company.name.startswith("ООО РЕВИЗИЯ Б")
    worker.join(timeout=20)
    assert not worker.is_alive()
    assert errors == []
    assert observed == {first_id, second_id}


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
