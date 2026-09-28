#!/usr/bin/env python3
"""Validate, stage and atomically activate one public release bundle."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.public_release_common import load_bundle, payload_sha256  # noqa: E402
from public_app.contracts import PublicProjection  # noqa: E402
from public_app.seo import (  # noqa: E402
    SEO_COMPILER_VERSION,
    SeoEligibilityContext,
    SeoProjection,
    compile_seo_projection,
)


SEO_COLUMNS = {
    "seo_projection",
    "seo_decision",
    "seo_compiler_version",
    "search_visible_hash",
    "non_identity_content_hash",
    "sitemap_shard",
    "seo_content_updated_at",
}
SEO_RELEASE_COLUMNS = {
    "seo_contract_version",
    "seo_release_cohort",
    "seo_released",
}


def _seo_storage_available(cursor) -> bool:
    cursor.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND (
            (table_name = 'public_company_projections' AND column_name = ANY(%s))
            OR
            (table_name = 'public_releases' AND column_name = ANY(%s))
          )
        """,
        (list(SEO_COLUMNS), list(SEO_RELEASE_COLUMNS)),
    )
    found = {(row["table_name"], row["column_name"]) for row in cursor.fetchall()}
    expected = {
        *(("public_company_projections", name) for name in SEO_COLUMNS),
        *(("public_releases", name) for name in SEO_RELEASE_COLUMNS),
    }
    return found == expected


def _compile_expected_release_seo(
    projection: PublicProjection,
    *,
    release_id: str,
    released: bool,
    previous: dict[str, SeoProjection],
) -> SeoProjection:
    """Compile one release row from the canonical persisted lineage context."""

    return compile_seo_projection(
        projection,
        context=SeoEligibilityContext(
            active_revision_id=release_id,
            public_ready=True,
            released=released,
        ),
        previous=previous.get(projection.company.inn),
    )


def _load_previous_seo(
    cursor,
    previous_release_id: str | None,
    *,
    _lineage: frozenset[str] = frozenset(),
) -> dict[str, SeoProjection]:
    """Load and verify usable SEO rows from the exact persisted predecessor.

    Every persisted predecessor edge is traversed for structural integrity.
    Historical native-v2 rows are replayed against their own predecessor before
    they can become compiler input. Legacy releases remain SEO context barriers,
    and legitimately absent per-company SEO rows contribute no fabricated state.
    """

    if previous_release_id is None:
        return {}
    if previous_release_id in _lineage:
        raise ValueError("public release predecessor lineage contains a cycle")

    cursor.execute(
        """
        SELECT release_id, previous_release_id, record_count, seo_contract_version,
               seo_release_cohort, seo_released
        FROM public_releases
        WHERE release_id=%s
        """,
        (previous_release_id,),
    )
    release = cursor.fetchone()
    if release is None:
        raise ValueError("public release predecessor does not exist")

    cursor.execute(
        """
        SELECT inn, payload, payload_sha256, seo_projection, seo_decision,
               seo_compiler_version, search_visible_hash,
               non_identity_content_hash, sitemap_shard,
               seo_content_updated_at
        FROM public_company_projections
        WHERE release_id=%s
        """,
        (previous_release_id,),
    )
    rows = cursor.fetchall()
    if len(rows) != release["record_count"]:
        raise ValueError("predecessor release projection set is incomplete")
    seo_fields = (
        "seo_projection",
        "seo_decision",
        "seo_compiler_version",
        "search_visible_hash",
        "non_identity_content_hash",
        "sitemap_shard",
        "seo_content_updated_at",
    )
    ancestor_seo = _load_previous_seo(
        cursor,
        release["previous_release_id"],
        _lineage=_lineage | {previous_release_id},
    )
    marker = release["seo_contract_version"]
    if marker is None:
        if any(any(row[name] is not None for name in seo_fields) for row in rows):
            raise ValueError("legacy predecessor contains unversioned SEO derived state")
        return {}
    if marker != SEO_COMPILER_VERSION:
        raise ValueError("predecessor release uses unsupported SEO compiler version")
    released = bool(
        release["seo_released"] and release["seo_release_cohort"] in {500, 2000, 10000}
    )
    loaded: dict[str, SeoProjection] = {}
    for row in rows:
        present = tuple(row[name] is not None for name in seo_fields)
        if not any(present):
            continue
        if not all(present):
            raise ValueError("predecessor release SEO projection is incomplete")
        try:
            projection = PublicProjection.model_validate(row["payload"])
            stored = SeoProjection.model_validate(row["seo_projection"])
        except (TypeError, ValueError) as exc:
            raise ValueError("predecessor release SEO projection is invalid") from exc
        expected = _compile_expected_release_seo(
            projection,
            release_id=previous_release_id,
            released=released,
            previous=ancestor_seo,
        )
        if (
            stored != expected
            or row["inn"] != projection.company.inn
            or projection.publication.release_id != previous_release_id
            or row["payload_sha256"] != payload_sha256(projection)
            or row["seo_decision"] != expected.eligibility.decision.value
            or row["seo_compiler_version"] != expected.compiler_version
            or row["search_visible_hash"] != expected.search_visible_hash
            or row["non_identity_content_hash"] != expected.non_identity_content_hash
            or row["sitemap_shard"] != expected.sitemap_shard
            or row["seo_content_updated_at"] != expected.content_updated_at
        ):
            raise ValueError("predecessor release SEO projection is not canonical")
        loaded[row["inn"]] = stored
    return loaded


def import_release(connection, bundle_dir: Path, expected_release_id: str | None = None) -> dict:
    manifest, projections, manifest_sha = load_bundle(bundle_dir)
    if expected_release_id and manifest.release_id != expected_release_id:
        raise ValueError("bundle release_id does not match the expected release")
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        seo_storage = _seo_storage_available(cursor)
        cursor.execute(
            "SELECT * FROM public_releases WHERE release_id=%s FOR UPDATE",
            (manifest.release_id,),
        )
        existing = cursor.fetchone()
        if existing:
            if existing["manifest_sha256"] != manifest_sha or existing["record_count"] != manifest.record_count:
                raise ValueError("release_id already exists with different content")
            if (
                manifest.previous_release_id is not None
                and existing["previous_release_id"] != manifest.previous_release_id
            ):
                raise ValueError(
                    "stored release previous_release_id does not match repeated bundle"
                )
            cursor.execute(
                "SELECT inn, payload_sha256 FROM public_company_projections WHERE release_id=%s",
                (manifest.release_id,),
            )
            stored = {row["inn"]: row["payload_sha256"] for row in cursor.fetchall()}
            expected = {item.company.inn: payload_sha256(item) for item in projections}
            if stored != expected:
                raise ValueError("stored release payload does not match repeated bundle")
            if seo_storage:
                cursor.execute(
                    """
                    SELECT inn, seo_projection, seo_decision,
                           seo_compiler_version, search_visible_hash,
                           non_identity_content_hash, sitemap_shard,
                           seo_content_updated_at
                    FROM public_company_projections
                    WHERE release_id=%s
                    """,
                    (manifest.release_id,),
                )
                stored_rows = cursor.fetchall()
                marker = existing["seo_contract_version"]
                if marker is None:
                    if any(
                        any(
                            row[name] is not None
                            for name in (
                                "seo_projection",
                                "seo_decision",
                                "seo_compiler_version",
                                "search_visible_hash",
                                "non_identity_content_hash",
                                "sitemap_shard",
                                "seo_content_updated_at",
                            )
                        )
                        for row in stored_rows
                    ):
                        raise ValueError("legacy release contains unversioned SEO derived state")
                else:
                    if marker != SEO_COMPILER_VERSION:
                        raise ValueError("stored release uses unsupported SEO compiler version")
                    previous_seo = _load_previous_seo(
                        cursor,
                        existing["previous_release_id"],
                    )
                    released = bool(
                        existing["seo_released"]
                        and existing["seo_release_cohort"] in {500, 2000, 10000}
                    )
                    stored_by_inn = {row["inn"]: row for row in stored_rows}
                    if set(stored_by_inn) != set(expected):
                        raise ValueError("stored release SEO projection set is incomplete")
                    for projection in projections:
                        row = stored_by_inn[projection.company.inn]
                        if any(
                            row[name] is None
                            for name in (
                                "seo_projection",
                                "seo_decision",
                                "seo_compiler_version",
                                "search_visible_hash",
                                "non_identity_content_hash",
                                "sitemap_shard",
                                "seo_content_updated_at",
                            )
                        ):
                            raise ValueError("stored release SEO projection is incomplete")
                        expected_seo = _compile_expected_release_seo(
                            projection,
                            release_id=manifest.release_id,
                            released=released,
                            previous=previous_seo,
                        )
                        try:
                            stored_projection = SeoProjection.model_validate(row["seo_projection"])
                        except (TypeError, ValueError) as exc:
                            raise ValueError("stored release SEO projection is invalid") from exc
                        if (
                            stored_projection != expected_seo
                            or row["seo_decision"] != expected_seo.eligibility.decision.value
                            or row["seo_compiler_version"] != expected_seo.compiler_version
                            or row["search_visible_hash"] != expected_seo.search_visible_hash
                            or row["non_identity_content_hash"] != expected_seo.non_identity_content_hash
                            or row["sitemap_shard"] != expected_seo.sitemap_shard
                            or row["seo_content_updated_at"] != expected_seo.content_updated_at
                        ):
                            raise ValueError(
                                "stored release SEO projection does not match repeated bundle"
                            )
            cursor.execute(
                "SELECT active_release_id FROM public_publication_state WHERE singleton=TRUE"
            )
            state = cursor.fetchone()
            return {
                "release_id": manifest.release_id,
                "record_count": manifest.record_count,
                "idempotent": True,
                "active": bool(state and state["active_release_id"] == manifest.release_id),
            }

        cursor.execute(
            """INSERT INTO public_publication_state(singleton, active_release_id)
               VALUES(TRUE, NULL) ON CONFLICT(singleton) DO NOTHING"""
        )
        cursor.execute(
            "SELECT active_release_id FROM public_publication_state WHERE singleton=TRUE FOR UPDATE"
        )
        state = cursor.fetchone()
        active_release_id = state["active_release_id"] if state else None
        if manifest.previous_release_id and manifest.previous_release_id != active_release_id:
            raise ValueError("bundle previous_release_id does not match the active release")
        previous_seo = (
            _load_previous_seo(cursor, active_release_id) if seo_storage else {}
        )

        release_values = (
            manifest.release_id,
            manifest.schema_version,
            manifest.source_main_sha,
            manifest.previous_release_id,
            manifest.created_at,
            manifest.record_count,
            manifest_sha,
        )
        if seo_storage:
            cursor.execute(
                """
                INSERT INTO public_releases (
                    release_id, schema_version, source_main_sha, previous_release_id,
                    created_at, record_count, manifest_sha256, status,
                    seo_contract_version, seo_release_cohort, seo_released
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'staged',%s,NULL,FALSE)
                """,
                release_values + (SEO_COMPILER_VERSION,),
            )
        else:
            cursor.execute(
                """
                INSERT INTO public_releases (
                    release_id, schema_version, source_main_sha, previous_release_id,
                    created_at, record_count, manifest_sha256, status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'staged')
                """,
                release_values,
            )
        for projection in projections:
            payload = projection.model_dump(mode="json")
            base_values = (
                manifest.release_id,
                projection.company.inn,
                projection.company.name,
                " ".join(projection.company.name.casefold().split()),
                Jsonb(payload),
                payload_sha256(projection),
                projection.publication.content_updated_at,
                projection.publication.index_eligible,
            )
            if seo_storage:
                seo = _compile_expected_release_seo(
                    projection,
                    release_id=manifest.release_id,
                    released=False,
                    previous=previous_seo,
                )
                cursor.execute(
                    """
                    INSERT INTO public_company_projections (
                        release_id, inn, name, normalized_name, payload,
                        payload_sha256, content_updated_at, index_eligible,
                        seo_projection, seo_decision, seo_compiler_version,
                        search_visible_hash, non_identity_content_hash,
                        sitemap_shard, seo_content_updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    base_values + (
                        Jsonb(seo.model_dump(mode="json")),
                        seo.eligibility.decision.value,
                        seo.compiler_version,
                        seo.search_visible_hash,
                        seo.non_identity_content_hash,
                        seo.sitemap_shard,
                        seo.content_updated_at,
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO public_company_projections (
                        release_id, inn, name, normalized_name, payload,
                        payload_sha256, content_updated_at, index_eligible
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    base_values,
                )
        cursor.execute(
            """SELECT count(*) AS count, count(DISTINCT inn) AS unique_count,
                      count(*) FILTER (WHERE payload->'publication'->>'release_id'=%s) AS matching_count
               FROM public_company_projections WHERE release_id=%s""",
            (manifest.release_id, manifest.release_id),
        )
        validation = cursor.fetchone()
        expected_count = manifest.record_count
        if tuple(validation.values()) != (expected_count, expected_count, expected_count):
            raise ValueError("staged release failed database validation")
        if seo_storage:
            cursor.execute(
                """
                SELECT count(*) AS count
                FROM public_company_projections
                WHERE release_id=%s AND seo_projection IS NOT NULL
                """,
                (manifest.release_id,),
            )
            if int(cursor.fetchone()["count"]) != expected_count:
                raise ValueError("staged release has incomplete SEO projection")
        cursor.execute(
            "UPDATE public_releases SET previous_release_id=%s WHERE release_id=%s",
            (active_release_id, manifest.release_id),
        )
        if active_release_id:
            cursor.execute(
                "UPDATE public_releases SET status='staged' WHERE release_id=%s",
                (active_release_id,),
            )
        cursor.execute(
            "UPDATE public_releases SET status='active' WHERE release_id=%s",
            (manifest.release_id,),
        )
        cursor.execute(
            """UPDATE public_publication_state
               SET active_release_id=%s, updated_at=now() WHERE singleton=TRUE""",
            (manifest.release_id,),
        )
    return {
        "release_id": manifest.release_id,
        "record_count": manifest.record_count,
        "previous_release_id": active_release_id,
        "idempotent": False,
        "active": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--database-url", default=os.getenv("PUBLIC_IMPORT_DATABASE_URL"))
    parser.add_argument("--expected-release-id")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("PUBLIC_IMPORT_DATABASE_URL or --database-url is required")
    url = args.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url) as connection:
        result = import_release(connection, args.bundle, args.expected_release_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
