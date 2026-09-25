"""Read-only PostgreSQL access for the public application."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from public_app.contracts import PublicProjection


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
                       r.record_count, r.source_main_sha
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

    def sitemap_rows(self) -> list[dict]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.inn, p.content_updated_at
                FROM public_publication_state s
                JOIN public_company_projections p
                  ON p.release_id = s.active_release_id
                WHERE s.singleton = TRUE AND p.index_eligible = TRUE
                ORDER BY p.inn
                """
            )
            return list(cursor.fetchall())

    def ready(self) -> tuple[bool, str | None, int]:
        try:
            release = self.active_release()
        except Exception:
            return False, None, 0
        if not release:
            return False, None, 0
        count = int(release["record_count"])
        return count == 40, str(release["release_id"]), count
