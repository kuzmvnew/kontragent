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


def import_release(connection, bundle_dir: Path, expected_release_id: str | None = None) -> dict:
    manifest, projections, manifest_sha = load_bundle(bundle_dir)
    if expected_release_id and manifest.release_id != expected_release_id:
        raise ValueError("bundle release_id does not match the expected release")
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT * FROM public_releases WHERE release_id=%s FOR UPDATE",
            (manifest.release_id,),
        )
        existing = cursor.fetchone()
        if existing:
            if existing["manifest_sha256"] != manifest_sha or existing["record_count"] != manifest.record_count:
                raise ValueError("release_id already exists with different content")
            cursor.execute(
                "SELECT inn, payload_sha256 FROM public_company_projections WHERE release_id=%s",
                (manifest.release_id,),
            )
            stored = {row["inn"]: row["payload_sha256"] for row in cursor.fetchall()}
            expected = {item.company.inn: payload_sha256(item) for item in projections}
            if stored != expected:
                raise ValueError("stored release payload does not match repeated bundle")
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
            """
            INSERT INTO public_releases (
                release_id, schema_version, source_main_sha, previous_release_id,
                created_at, record_count, manifest_sha256, status
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,'staged')
            """,
            (
                manifest.release_id,
                manifest.schema_version,
                manifest.source_main_sha,
                manifest.previous_release_id,
                manifest.created_at,
                manifest.record_count,
                manifest_sha,
            ),
        )
        for projection in projections:
            payload = projection.model_dump(mode="json")
            cursor.execute(
                """
                INSERT INTO public_company_projections (
                    release_id, inn, name, normalized_name, payload,
                    payload_sha256, content_updated_at, index_eligible
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    manifest.release_id,
                    projection.company.inn,
                    projection.company.name,
                    " ".join(projection.company.name.casefold().split()),
                    Jsonb(payload),
                    payload_sha256(projection),
                    projection.publication.content_updated_at,
                    projection.publication.index_eligible,
                ),
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
