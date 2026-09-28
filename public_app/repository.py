"""Read-only PostgreSQL access for the public application."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from public_app.contracts import PublicProjection
from public_app.seo import SeoEligibilityContext, SeoProjection, compile_seo_projection
from public_app.stored_seo import (
    StoredSeoProjectionState,
    StoredSeoProjectionValidation,
    validate_stored_seo_projection,
)


CATALOG_SCAN_BATCH_SIZE = 256

_STORED_SEO_COLUMNS = """
    p.release_id, p.inn, p.payload, p.seo_projection, p.seo_decision,
    p.seo_compiler_version, p.search_visible_hash,
    p.non_identity_content_hash, p.sitemap_shard,
    p.seo_content_updated_at, r.status AS release_status, r.seo_contract_version,
    r.seo_release_cohort, r.seo_released
"""

_INDEX_CANDIDATES = """
    FROM public_publication_state s
    JOIN public_company_projections p ON p.release_id = s.active_release_id
    JOIN public_releases r ON r.release_id = p.release_id
    WHERE s.singleton = TRUE
      AND r.status = 'active'
      AND r.seo_released IS TRUE
      AND r.seo_release_cohort IN (500, 2000, 10000)
      AND p.seo_projection IS NOT NULL
      AND p.seo_decision = 'INDEX'
"""


@dataclass(frozen=True)
class CompanyPageSnapshot:
    """One revision-consistent company page read.

    The SEO value is always safe to render. When persisted SEO is missing,
    corrupt, revision-mismatched, or lacks explicit cohort authorization, it
    is a deterministic noindex projection derived from the same row.
    """

    release_id: str
    projection: PublicProjection
    seo: SeoProjection
    seo_release_cohort: int | None
    seo_released: bool
    stored_seo_valid: bool
    stored_seo_state: StoredSeoProjectionState


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

    def get_company_page_snapshot(self, inn: str) -> CompanyPageSnapshot | None:
        """Read facts and SEO state from one PostgreSQL statement snapshot."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.release_id, p.payload,
                       to_jsonb(p)->'seo_projection' AS seo_projection,
                       to_jsonb(p)->>'seo_decision' AS seo_decision,
                       to_jsonb(p)->>'seo_compiler_version' AS seo_compiler_version,
                       to_jsonb(p)->>'search_visible_hash' AS search_visible_hash,
                       to_jsonb(p)->>'non_identity_content_hash' AS non_identity_content_hash,
                       to_jsonb(p)->>'sitemap_shard' AS sitemap_shard,
                       to_jsonb(p)->>'seo_content_updated_at' AS seo_content_updated_at,
                       to_jsonb(r)->>'status' AS release_status,
                       to_jsonb(r)->>'seo_contract_version' AS seo_contract_version,
                       to_jsonb(r)->>'seo_release_cohort' AS seo_release_cohort,
                       COALESCE(to_jsonb(r)->>'seo_released', 'false') AS seo_released
                FROM public_publication_state s
                JOIN public_company_projections p ON p.release_id = s.active_release_id
                JOIN public_releases r ON r.release_id = p.release_id
                WHERE s.singleton = TRUE AND p.inn = %s
                """,
                (inn,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        release_id = str(row["release_id"])
        projection = PublicProjection.model_validate(row["payload"])
        if (
            projection.publication.release_id != release_id
            or projection.company.inn != inn
        ):
            return None
        cohort_text = row["seo_release_cohort"]
        cohort = int(cohort_text) if cohort_text in {"500", "2000", "10000"} else None
        seo_released = row["seo_released"] == "true" and cohort is not None
        validation = self._validate_stored_row(row, projection, inn=inn)
        if validation.valid:
            assert validation.projection is not None
            effective_seo = validation.projection
        else:
            # Compatibility is deliberately one-way: missing or unauthorized
            # derived state may keep the page visible, but can never grant INDEX.
            effective_seo = compile_seo_projection(
                projection,
                context=SeoEligibilityContext(
                    active_revision_id=release_id,
                    public_ready=True,
                    released=False,
                ),
            )
        return CompanyPageSnapshot(
            release_id=release_id,
            projection=projection,
            seo=effective_seo,
            seo_release_cohort=cohort,
            seo_released=seo_released,
            stored_seo_valid=validation.valid,
            stored_seo_state=validation.state,
        )

    @staticmethod
    def _validate_stored_row(
        row: dict,
        projection: PublicProjection,
        *,
        inn: str | None = None,
    ) -> StoredSeoProjectionValidation:
        return validate_stored_seo_projection(
            public_projection=projection,
            release_id=str(row["release_id"]),
            inn=inn or str(row["inn"]),
            seo_projection=row["seo_projection"],
            seo_decision=row["seo_decision"],
            seo_compiler_version=row["seo_compiler_version"],
            search_visible_hash=row["search_visible_hash"],
            non_identity_content_hash=row["non_identity_content_hash"],
            sitemap_shard_value=row["sitemap_shard"],
            seo_content_updated_at=row["seo_content_updated_at"],
            release_status=row["release_status"],
            seo_contract_version=row["seo_contract_version"],
            seo_release_cohort=row["seo_release_cohort"],
            seo_released=row["seo_released"],
        )

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
            SELECT count(*) FILTER (
                       WHERE table_name = 'public_company_projections'
                   ) = 7
                   AND count(*) FILTER (
                       WHERE table_name = 'public_releases'
                   ) = 3 AS ready
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND (
                (table_name = 'public_company_projections' AND column_name IN (
                  'seo_projection', 'seo_decision', 'seo_compiler_version',
                  'search_visible_hash', 'non_identity_content_hash',
                  'sitemap_shard', 'seo_content_updated_at'
                ))
                OR
                (table_name = 'public_releases' AND column_name IN (
                  'seo_contract_version', 'seo_release_cohort', 'seo_released'
                ))
              )
            """
        )
        row = cursor.fetchone()
        return bool(row and row["ready"])

    def sitemap_rows(self, shard: str | None = None) -> list[dict]:
        rows: list[dict] = []
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            with connection.cursor() as cursor:
                if not self._seo_storage_ready(cursor):
                    return []
                cursor.execute(
                    "SELECT "
                    + _STORED_SEO_COLUMNS
                    + _INDEX_CANDIDATES
                    + " AND (%s::text IS NULL OR p.sitemap_shard = %s)"
                    + " ORDER BY p.inn",
                    (shard, shard),
                )
                candidates = cursor.fetchall()
            for row in candidates:
                try:
                    projection = PublicProjection.model_validate(row["payload"])
                    validation = self._validate_stored_row(row, projection)
                except (TypeError, ValueError):
                    continue
                if validation.state == StoredSeoProjectionState.VALID_INDEX:
                    assert validation.projection is not None
                    rows.append(
                        {
                            "inn": projection.company.inn,
                            "content_updated_at": validation.projection.content_updated_at,
                        }
                    )
        return rows

    def catalog_page(self, page: int, page_size: int = 24) -> tuple[list[PublicProjection], int]:
        if page < 1 or page_size != 24:
            return [], 0
        offset = (page - 1) * page_size
        items: list[PublicProjection] = []
        total = 0
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            with connection.cursor() as cursor:
                if not self._seo_storage_ready(cursor):
                    return [], 0
            # A server-side cursor keeps memory bounded while validation—not
            # SQL JSON predicates—determines exact count and page membership.
            with connection.cursor(name="seo_catalog_candidates") as cursor:
                cursor.execute(
                    "SELECT "
                    + _STORED_SEO_COLUMNS
                    + _INDEX_CANDIDATES
                    + " ORDER BY p.normalized_name, p.inn"
                )
                while batch := cursor.fetchmany(CATALOG_SCAN_BATCH_SIZE):
                    for row in batch:
                        try:
                            projection = PublicProjection.model_validate(row["payload"])
                            validation = self._validate_stored_row(row, projection)
                        except (TypeError, ValueError):
                            continue
                        if validation.state != StoredSeoProjectionState.VALID_INDEX:
                            continue
                        if total >= offset and len(items) < page_size:
                            items.append(projection)
                        total += 1
        return items, total

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
