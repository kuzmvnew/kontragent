"""Evidence-based storage forecasts for company-factory scale gates."""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session


COMPANY_VARIABLE_RELATIONS = (
    "companies",
    "company_source_data",
    "company_enrichment_runs",
    "company_source_coverage",
    "company_risk_assessments_v3",
    "company_summaries_v3",
    "company_revenue_expense_snapshots",
    "company_tax_offences",
    "company_tax_payment_snapshots",
    "company_tax_payment_items",
    "company_headcounts",
    "company_msp_profiles",
    "company_tax_regime_snapshots",
    "company_legal_events",
    "company_registry_changes",
    "master_replay_signals",
    "worker_jobs",
    "worker_runs",
    "worker_raw_manifests",
)


def forecast_factory_storage(
    session: Session,
    *,
    targets: Sequence[int] = (10_000, 100_000, 1_000_000, 12_000_000),
    wal_factor: float = 0.5,
    backup_generations: int = 2,
) -> dict[str, Any]:
    """Forecast using current per-company bytes and explicit retention factors."""

    if wal_factor < 0 or backup_generations < 0:
        raise ValueError("storage retention factors cannot be negative")
    if any(target <= 0 for target in targets):
        raise ValueError("storage targets must be positive")
    master = int(session.execute(text("SELECT count(*) FROM companies")).scalar() or 0)
    fetched = int(
        session.execute(
            text(
                "SELECT count(DISTINCT inn) FROM firmoteka_crawl_items "
                "WHERE status = 'succeeded'"
            )
        ).scalar()
        or 0
    )
    database_bytes = int(
        session.execute(text("SELECT pg_database_size(current_database())")).scalar()
        or 0
    )
    relation = session.execute(
        text(
            """
            SELECT
              coalesce(sum(pg_relation_size(c.oid)), 0) AS heap_bytes,
              coalesce(sum(pg_indexes_size(c.oid)), 0) AS index_bytes,
              coalesce(sum(pg_total_relation_size(c.oid)), 0) AS total_bytes
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname = ANY(CAST(:relations AS text[]))
            """
        ),
        {"relations": list(COMPANY_VARIABLE_RELATIONS)},
    ).mappings().one()
    variable_heap = int(relation["heap_bytes"] or 0)
    variable_indexes = int(relation["index_bytes"] or 0)
    variable_total = int(relation["total_bytes"] or 0)
    fixed_postgres = max(0, database_bytes - variable_total)
    raw = session.execute(
        text(
            """
            WITH firmoteka AS (
              SELECT
                coalesce(sum(size_bytes) FILTER (WHERE artifact_kind = 'company'), 0)
                  AS company_bytes,
                coalesce(sum(size_bytes) FILTER (WHERE artifact_kind <> 'company'), 0)
                  AS fixed_catalog_bytes
              FROM firmoteka_raw_artifacts
            ), other_raw AS (
              SELECT coalesce(sum(
                CASE
                  WHEN coalesce(m.manifest->>'response_size', '') ~ '^[0-9]+$'
                    THEN (m.manifest->>'response_size')::bigint
                  WHEN coalesce(m.manifest->>'size_bytes', '') ~ '^[0-9]+$'
                    THEN (m.manifest->>'size_bytes')::bigint
                  WHEN coalesce(m.manifest->>'stored_size', '') ~ '^[0-9]+$'
                    THEN (m.manifest->>'stored_size')::bigint
                  ELSE 0
                END
              ), 0) AS bytes
              FROM worker_raw_manifests m
              JOIN worker_runs r ON r.id = m.run_id
              JOIN worker_jobs j ON j.id = r.job_id
              WHERE j.source_id <> 'firmoteka'
            )
            SELECT
              firmoteka.company_bytes
                AS company_bytes,
              firmoteka.fixed_catalog_bytes + other_raw.bytes
                AS fixed_catalog_bytes
            FROM firmoteka CROSS JOIN other_raw
            """
        )
    ).mappings().one()
    company_raw = int(raw["company_bytes"] or 0)
    fixed_raw = int(raw["fixed_catalog_bytes"] or 0)
    heap_per_company = variable_heap / master if master else 0.0
    index_per_company = variable_indexes / master if master else 0.0
    raw_per_company = company_raw / fetched if fetched else 0.0
    forecasts: dict[str, dict[str, int]] = {}
    for target in targets:
        normalized = round(heap_per_company * target)
        indexes = round(index_per_company * target)
        postgres = fixed_postgres + normalized + indexes
        raw_bytes = fixed_raw + round(raw_per_company * target)
        wal = round((normalized + indexes) * wal_factor)
        backups = round((postgres + raw_bytes) * backup_generations)
        forecasts[str(target)] = {
            "raw_bytes": raw_bytes,
            "normalized_heap_bytes": normalized,
            "index_bytes": indexes,
            "postgres_bytes": postgres,
            "wal_bytes": wal,
            "backup_bytes": backups,
            "total_bytes": raw_bytes + postgres + wal + backups,
        }
    return {
        "basis": {
            "master_companies": master,
            "firmoteka_fetched_companies": fetched,
            "database_bytes": database_bytes,
            "fixed_postgres_bytes": fixed_postgres,
            "fixed_raw_catalog_bytes": fixed_raw,
            "raw_bytes_per_fetched_company": round(raw_per_company, 3),
            "normalized_heap_bytes_per_master": round(heap_per_company, 3),
            "index_bytes_per_master": round(index_per_company, 3),
            "wal_factor": wal_factor,
            "backup_generations": backup_generations,
        },
        "forecasts": forecasts,
    }
