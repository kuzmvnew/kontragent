"""Read-only PostgreSQL access for the public application."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from public_app.contracts import PublicProjection
from public_app.seo import SeoDecision, SeoProjection, compile_seo_projection, sitemap_shard


class PublicRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or os.getenv("PUBLIC_DATABASE_URL", "")
        self.database_url = self.database_url.replace(
            "postgresql+psycopg://", "postgresql://", 1
        )

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        if not self.database_url:
            raise RuntimeError("PUBLIC_DATABASE_URL is required")
        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        ) as connection:
            yield connection

    def active_release(self) -> dict | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.release_id, r.schema_version, r.created_at,
                       r.record_count, r.source_main_sha,
                       (SELECT count(*) FROM public_company_projections p
                         WHERE p.release_id = r.release_id) AS actual_record_count
                FROM public_publication_state s
                JOIN public_releases r ON r.release_id = s.active_release_id
                WHERE s.singleton = TRUE
                """
            )
            return cursor.fetchone()

    def get_company(self, inn: str) -> PublicProjection | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.payload
                FROM public_publication_state s
                JOIN public_company_projections p
                  ON p.release_id = s.active_release_id
                WHERE s.singleton = TRUE AND p.inn = %s
                """,
                (inn,),
            )
            row = cursor.fetchone()
        return PublicProjection.model_validate(row["payload"]) if row else None

    def get_seo_projection(self, inn: str) -> SeoProjection | None:
        """Read the release-owned SEO snapshot without requiring v2 columns."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.release_id, to_jsonb(p)->'seo_projection' AS seo_projection
                FROM public_publication_state s
                JOIN public_company_projections p ON p.release_id = s.active_release_id
                WHERE s.singleton = TRUE AND p.inn = %s
                """,
                (inn,),
            )
            row = cursor.fetchone()
        if not row or row["seo_projection"] is None:
            return None
        seo = SeoProjection.model_validate(row["seo_projection"])
        return seo if seo.active_revision_id == row["release_id"] else None

    def search(self, query: str, limit: int = 20) -> list[PublicProjection]:
        normalized = " ".join(query.casefold().split())
        if not normalized:
            return []
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.payload
                FROM public_publication_state s
                JOIN public_company_projections p
                  ON p.release_id = s.active_release_id
                WHERE s.singleton = TRUE
                  AND (p.inn = %s OR p.normalized_name LIKE %s)
                ORDER BY CASE WHEN p.inn = %s THEN 0 ELSE 1 END,
                         p.normalized_name, p.inn
                LIMIT %s
                """,
                (query.strip(), f"%{normalized}%", query.strip(), limit),
            )
            rows = cursor.fetchall()
        return [PublicProjection.model_validate(row["payload"]) for row in rows]

    @staticmethod
    def _seo_storage_ready(cursor) -> bool:
        cursor.execute(
            """
            SELECT count(*) = 7 AS ready
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'public_company_projections'
              AND column_name IN (
                'seo_projection', 'seo_decision', 'seo_compiler_version',
                'search_visible_hash', 'non_identity_content_hash',
                'sitemap_shard', 'seo_content_updated_at'
              )
            """
        )
        row = cursor.fetchone()
        return bool(row and row["ready"])

    @staticmethod
    def _stored_seo_complete(cursor) -> bool:
        cursor.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE p.seo_projection IS NOT NULL) AS compiled
            FROM public_publication_state s
            JOIN public_company_projections p ON p.release_id = s.active_release_id
            WHERE s.singleton = TRUE
            """
        )
        row = cursor.fetchone()
        return bool(row and int(row["total"]) > 0 and int(row["total"]) == int(row["compiled"]))

    def sitemap_rows(self, shard: str | None = None) -> list[dict]:
        with self._connection() as connection, connection.cursor() as cursor:
            if self._seo_storage_ready(cursor) and self._stored_seo_complete(cursor):
                cursor.execute(
                    """
                    SELECT p.inn, p.seo_content_updated_at AS content_updated_at
                    FROM public_publication_state s
                    JOIN public_company_projections p
                      ON p.release_id = s.active_release_id
                    WHERE s.singleton = TRUE
                      AND p.seo_decision = 'INDEX'
                      AND (%s IS NULL OR p.sitemap_shard = %s)
                    ORDER BY p.inn
                    """,
                    (shard, shard),
                )
                return list(cursor.fetchall())
            # Mixed-version fallback is bounded by the v1 release constraint (40).
            cursor.execute(
                """
                SELECT p.payload
                FROM public_publication_state s
                JOIN public_company_projections p ON p.release_id = s.active_release_id
                WHERE s.singleton = TRUE
                ORDER BY p.inn
                """
            )
            projections = [PublicProjection.model_validate(row["payload"]) for row in cursor.fetchall()]
        rows = []
        for projection in projections:
            seo = compile_seo_projection(projection)
            if seo.eligibility.decision == SeoDecision.INDEX and (shard is None or seo.sitemap_shard == shard):
                rows.append({"inn": projection.company.inn, "content_updated_at": seo.content_updated_at})
        return rows

    def catalog_page(self, page: int, page_size: int = 24) -> tuple[list[PublicProjection], int]:
        if page < 1 or page_size != 24:
            return [], 0
        offset = (page - 1) * page_size
        with self._connection() as connection, connection.cursor() as cursor:
            if self._seo_storage_ready(cursor) and self._stored_seo_complete(cursor):
                cursor.execute(
                    """
                    SELECT count(*) AS count
                    FROM public_publication_state s
                    JOIN public_company_projections p ON p.release_id = s.active_release_id
                    WHERE s.singleton = TRUE AND p.seo_decision = 'INDEX'
                    """
                )
                total = int(cursor.fetchone()["count"])
                cursor.execute(
                    """
                    SELECT p.payload
                    FROM public_publication_state s
                    JOIN public_company_projections p ON p.release_id = s.active_release_id
                    WHERE s.singleton = TRUE AND p.seo_decision = 'INDEX'
                    ORDER BY p.normalized_name, p.inn
                    LIMIT %s OFFSET %s
                    """,
                    (page_size, offset),
                )
                return [PublicProjection.model_validate(row["payload"]) for row in cursor.fetchall()], total
            # Mixed-version fallback remains bounded to the current 40-card schema.
            cursor.execute(
                """
                SELECT p.payload
                FROM public_publication_state s
                JOIN public_company_projections p ON p.release_id = s.active_release_id
                WHERE s.singleton = TRUE
                ORDER BY p.normalized_name, p.inn
                """
            )
            projections = [PublicProjection.model_validate(row["payload"]) for row in cursor.fetchall()]
        eligible = [item for item in projections if compile_seo_projection(item).eligibility.decision == SeoDecision.INDEX]
        return eligible[offset : offset + page_size], len(eligible)

    def ready(self) -> tuple[bool, str | None, int]:
        try:
            release = self.active_release()
        except Exception:
            return False, None, 0
        if not release:
            return False, None, 0
        count = int(release["record_count"])
        actual = int(release["actual_record_count"])
        return count > 0 and actual == count, str(release["release_id"]), actual
