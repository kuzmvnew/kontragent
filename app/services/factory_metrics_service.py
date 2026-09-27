"""Read-only operational measurements for the company factory.

The service intentionally uses only persisted timestamps and PostgreSQL
statistics.  It does not infer production throughput from configured limits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichmentRun, CompanySourceCoverage
from app.models.factory import FactoryGeneration, FactoryGenerationCompany
from app.models.registry_master import MasterReplaySignal
from app.models.source import DataSet
from app.services.replay_readiness_service import (
    operational_replay_predicates,
    unresolved_replay_exists_clause,
)
from app.services.source_applicability_service import (
    SourceApplicability,
    source_applicability_expression,
)


def _scalar(session: Session, statement: str, **parameters: Any) -> int | float:
    value = session.execute(text(statement), parameters).scalar()
    return value or 0


def _mapping(session: Session, statement: str, **parameters: Any) -> dict[str, Any]:
    row = session.execute(text(statement), parameters).mappings().one()
    return dict(row)


def _rate(count: int, window_hours: float) -> dict[str, float]:
    per_hour = float(count) / window_hours
    return {
        "count": int(count),
        "per_hour": round(per_hour, 3),
        "per_day": round(per_hour * 24, 3),
    }


def collect_factory_metrics(
    session: Session,
    *,
    window_hours: float = 1.0,
    now: datetime | None = None,
    enforce_read_only: bool = True,
) -> dict[str, Any]:
    """Collect one database-authoritative, mutation-free factory snapshot."""

    if window_hours <= 0:
        raise ValueError("window_hours must be positive")
    observed_at = now or datetime.now(timezone.utc)
    cutoff = observed_at - timedelta(hours=window_hours)
    if enforce_read_only:
        session.execute(text("SET TRANSACTION READ ONLY"))

    totals = _mapping(
        session,
        """
        WITH latest AS (
          SELECT r.*,
                 row_number() OVER (
                   PARTITION BY r.company_id
                   ORDER BY r.created_at DESC, r.id DESC
                 ) AS position
          FROM company_enrichment_runs r
        )
        SELECT
          (SELECT count(*) FROM companies) AS master,
          count(*) FILTER (WHERE position = 1) AS companies_with_run,
          count(*) FILTER (
            WHERE position = 1 AND status = 'succeeded'
              AND completed_source_count = source_count
              AND failed_source_count = 0
              AND NOT EXISTS (
                SELECT 1 FROM company_source_coverage c
                WHERE c.enrichment_run_id = latest.id
                  AND c.status = 'APPLICABILITY_UNKNOWN'
              )
          ) AS fully_enriched,
          count(*) FILTER (
            WHERE position = 1 AND public_ready
              AND risk_assessment_id IS NOT NULL
              AND summary_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM company_source_coverage c
                WHERE c.enrichment_run_id = latest.id
                  AND c.status = 'APPLICABILITY_UNKNOWN'
              )
          ) AS public_ready,
          count(*) FILTER (
            WHERE position = 1
              AND status IN ('pending','waiting_sources','retry_scheduled','running')
              AND EXISTS (
                SELECT 1 FROM company_source_coverage c
                WHERE c.enrichment_run_id = latest.id
                  AND c.execution_status IN (
                    'pending','queued','running','retry_scheduled'
                  )
              )
          ) AS enrichment_active,
          count(*) FILTER (
            WHERE position = 1
              AND EXISTS (
                SELECT 1 FROM company_source_coverage c
                WHERE c.enrichment_run_id = latest.id
                  AND c.status = 'APPLICABILITY_UNKNOWN'
              )
          ) AS applicability_blocked_runs,
          count(*) FILTER (WHERE position = 1 AND status = 'failed') AS enrichment_failed
        FROM latest
        """,
    )
    totals = {key: int(value or 0) for key, value in totals.items()}
    ranked_runs = (
        select(
            CompanyEnrichmentRun.id.label("run_id"),
            CompanyEnrichmentRun.company_id.label("company_id"),
            CompanyEnrichmentRun.status.label("status"),
            CompanyEnrichmentRun.public_ready.label("public_ready"),
            CompanyEnrichmentRun.source_count.label("source_count"),
            CompanyEnrichmentRun.completed_source_count.label(
                "completed_source_count"
            ),
            CompanyEnrichmentRun.failed_source_count.label("failed_source_count"),
            CompanyEnrichmentRun.risk_assessment_id.label("risk_assessment_id"),
            CompanyEnrichmentRun.summary_id.label("summary_id"),
            CompanyEnrichmentRun.finished_at.label("finished_at"),
            func.row_number()
            .over(
                partition_by=CompanyEnrichmentRun.company_id,
                order_by=(
                    CompanyEnrichmentRun.created_at.desc(),
                    CompanyEnrichmentRun.id.desc(),
                ),
            )
            .label("position"),
        )
        .subquery()
    )
    latest_runs = select(ranked_runs).where(ranked_runs.c.position == 1).subquery()
    coverage_blocker = (
        select(CompanySourceCoverage.id)
        .where(
            CompanySourceCoverage.enrichment_run_id == latest_runs.c.run_id,
            CompanySourceCoverage.status == "APPLICABILITY_UNKNOWN",
        )
        .exists()
    )
    unresolved_blocking = unresolved_replay_exists_clause(
        latest_runs.c.company_id,
        latest_runs.c.run_id,
        now=observed_at,
    )
    unresolved_unknown = unresolved_replay_exists_clause(
        latest_runs.c.company_id,
        latest_runs.c.run_id,
        now=observed_at,
        outcomes=(SourceApplicability.UNKNOWN,),
    )
    fully_ready = (
        latest_runs.c.status == "succeeded",
        latest_runs.c.completed_source_count == latest_runs.c.source_count,
        latest_runs.c.failed_source_count == 0,
        ~coverage_blocker,
        ~unresolved_blocking,
    )
    public_ready = (
        latest_runs.c.public_ready.is_(True),
        latest_runs.c.risk_assessment_id.is_not(None),
        latest_runs.c.summary_id.is_not(None),
        ~coverage_blocker,
        ~unresolved_blocking,
    )
    totals["fully_enriched"] = int(
        session.scalar(
            select(func.count()).select_from(latest_runs).where(*fully_ready)
        )
        or 0
    )
    totals["public_ready"] = int(
        session.scalar(
            select(func.count()).select_from(latest_runs).where(*public_ready)
        )
        or 0
    )
    totals["applicability_blocked_runs"] = int(
        session.scalar(
            select(func.count())
            .select_from(latest_runs)
            .where(or_(coverage_blocker, unresolved_unknown))
        )
        or 0
    )
    totals["enrichment_not_started"] = max(
        0, totals["master"] - totals["companies_with_run"]
    )
    totals["enrichment_backlog"] = max(
        0, totals["master"] - totals["fully_enriched"]
    )

    queues = _mapping(
        session,
        """
        SELECT
          (SELECT count(*) FROM master_replay_signals
             WHERE status IN ('pending','scheduled')) AS master_replay_total,
          (SELECT count(*) FROM company_source_coverage
             WHERE execution_status IN ('pending','queued','running','retry_scheduled'))
             AS source_expectations,
          (SELECT count(*) FROM company_source_coverage
             WHERE status = 'APPLICABILITY_UNKNOWN') AS applicability_blockers,
          (SELECT count(*) FROM worker_jobs
             WHERE status IN ('queued','retry_scheduled')) AS worker_pending,
          (SELECT count(*) FROM worker_jobs WHERE status = 'running') AS worker_running,
          (SELECT EXTRACT(EPOCH FROM (:observed_at - min(created_at)))
             FROM company_enrichment_runs
             WHERE status IN ('pending','waiting_sources','retry_scheduled','running'))
             AS oldest_enrichment_seconds,
          (SELECT EXTRACT(EPOCH FROM (:observed_at - min(created_at)))
             FROM worker_jobs WHERE status IN ('queued','retry_scheduled'))
             AS oldest_worker_job_seconds
        """,
        observed_at=observed_at,
    )
    queues["applicability_blocker_rows"] = int(
        queues.get("applicability_blockers") or 0
    )
    # Operational blocker units are latest company workflows.  Coverage and
    # replay are OR-ed, so recovery changes representation without double count.
    queues["applicability_blockers"] = totals["applicability_blocked_runs"]
    applicability = source_applicability_expression(
        DataSet.applicability,
        Company.entity_type,
        Company.inn,
    )

    def replay_count(outcome: SourceApplicability) -> int:
        return int(
            session.scalar(
                select(func.count(MasterReplaySignal.id))
                .join(DataSet, DataSet.code == MasterReplaySignal.target_source_id)
                .join(Company, Company.id == MasterReplaySignal.company_id)
                .where(
                    MasterReplaySignal.status.in_(("pending", "scheduled")),
                    *operational_replay_predicates(DataSet, now=observed_at),
                    applicability == outcome.value,
                )
            )
            or 0
        )

    queues["master_replay_actionable"] = replay_count(
        SourceApplicability.APPLICABLE
    )
    queues["master_replay_not_applicable"] = replay_count(
        SourceApplicability.NOT_APPLICABLE
    )
    queues["master_replay_applicability_unknown"] = replay_count(
        SourceApplicability.UNKNOWN
    )
    generation_counts = dict(
        session.execute(
            select(
                FactoryGenerationCompany.status,
                func.count(FactoryGenerationCompany.id),
            )
            .join(
                FactoryGeneration,
                FactoryGeneration.id == FactoryGenerationCompany.generation_id,
            )
            .where(FactoryGeneration.generation_type == "source")
            .group_by(FactoryGenerationCompany.status)
        ).all()
    )
    for status in (
        "pending",
        "scheduled",
        "complete",
        "failed",
        "not_applicable",
        "applicability_unknown",
    ):
        queues[f"source_generation_{status}"] = int(
            generation_counts.get(status, 0)
        )
    queues["source_generation_actionable"] = (
        queues["source_generation_pending"] + queues["source_generation_scheduled"]
    )
    queues = {
        key: (round(float(value), 3) if key.endswith("_seconds") else int(value or 0))
        if value is not None
        else None
        for key, value in queues.items()
    }

    firmoteka = _mapping(
        session,
        """
        WITH identities AS (
          SELECT inn, min(source_url) AS source_url
          FROM firmoteka_crawl_items
          GROUP BY inn
        ), latest_run AS (
          SELECT * FROM firmoteka_crawl_runs
          ORDER BY created_at DESC, id DESC LIMIT 1
        )
        SELECT
          (SELECT count(*) FROM firmoteka_crawl_items) AS discovered_rows,
          (SELECT count(*) FROM identities) AS unique_company_urls,
          (SELECT count(*) FROM identities WHERE length(inn) = 10) AS legal,
          (SELECT count(*) FROM identities WHERE length(inn) = 12) AS ip,
          (SELECT count(*) FROM firmoteka_crawl_items)
            - (SELECT count(*) FROM identities) AS duplicate_identities,
          (SELECT count(DISTINCT inn) FROM firmoteka_crawl_items
             WHERE status = 'succeeded') AS fetched,
          (SELECT count(*) FROM firmoteka_crawl_items
             WHERE status IN ('pending','running')) AS backlog,
          (SELECT count(*) FROM firmoteka_quarantine_records) AS quarantined,
          (SELECT count(*) FROM firmoteka_quarantine_records
             WHERE reason ILIKE '%%identity%%' OR reason ILIKE '%%inn%%'
                OR reason ILIKE '%%ogrn%%') AS invalid_identities,
          (SELECT count(*) FROM firmoteka_catalog_pages
             WHERE status = 'succeeded') AS catalog_pages_succeeded,
          (SELECT count(*) FROM firmoteka_catalog_pages) AS catalog_pages_total,
          (SELECT status FROM latest_run) AS crawl_status,
          (SELECT phase FROM latest_run) AS crawl_phase,
          (SELECT catalog_concurrency FROM latest_run) AS catalog_lanes,
          (SELECT company_concurrency FROM latest_run) AS company_lanes,
          (SELECT http_status_counts FROM latest_run) AS http_status_counts,
          (SELECT captcha_count FROM latest_run) AS captcha_count,
          (SELECT latency_histogram FROM latest_run) AS latency_histogram,
          (SELECT latency_ms_max FROM latest_run) AS latency_ms_max
        """,
    )
    for key in (
        "discovered_rows",
        "unique_company_urls",
        "legal",
        "ip",
        "duplicate_identities",
        "fetched",
        "backlog",
        "quarantined",
        "invalid_identities",
        "catalog_pages_succeeded",
        "catalog_pages_total",
        "catalog_lanes",
        "company_lanes",
        "captcha_count",
        "latency_ms_max",
    ):
        firmoteka[key] = int(firmoteka.get(key) or 0)

    observed_counts = _mapping(
        session,
        """
        SELECT
          (SELECT count(*) FROM companies WHERE created_at >= :cutoff) AS master,
          (SELECT count(*) FROM (
             SELECT company_id, min(finished_at) AS first_ready_at
             FROM company_enrichment_runs
             WHERE status = 'succeeded'
               AND completed_source_count = source_count
               AND failed_source_count = 0
               AND NOT EXISTS (
                 SELECT 1 FROM company_source_coverage c
                 WHERE c.enrichment_run_id = company_enrichment_runs.id
                   AND c.status = 'APPLICABILITY_UNKNOWN'
               )
             GROUP BY company_id
           ) x WHERE first_ready_at >= :cutoff) AS fully_enriched,
          (SELECT count(*) FROM (
             SELECT company_id, min(finished_at) AS first_ready_at
             FROM company_enrichment_runs
             WHERE public_ready
               AND risk_assessment_id IS NOT NULL
               AND summary_id IS NOT NULL
               AND NOT EXISTS (
                 SELECT 1 FROM company_source_coverage c
                 WHERE c.enrichment_run_id = company_enrichment_runs.id
                   AND c.status = 'APPLICABILITY_UNKNOWN'
               )
             GROUP BY company_id
           ) x WHERE first_ready_at >= :cutoff) AS public_ready,
          (SELECT count(*) FROM company_risk_assessments_v3
             WHERE calculated_at >= :cutoff) AS risk,
          (SELECT count(*) FROM company_summaries_v3
             WHERE generated_at >= :cutoff) AS summary,
          (SELECT count(*) FROM company_source_coverage
             WHERE finished_at >= :cutoff) AS source_expectations,
          (SELECT count(*) FROM worker_jobs WHERE created_at >= :cutoff) AS worker_jobs,
          (SELECT count(*) FROM worker_runs
             WHERE finished_at >= :cutoff) AS worker_runs,
          (SELECT count(DISTINCT inn) FROM firmoteka_crawl_items
             WHERE fetched_at >= :cutoff AND status = 'succeeded') AS firmoteka_fetched
        """,
        cutoff=cutoff,
    )
    observed_counts["fully_enriched"] = int(
        session.scalar(
            select(func.count())
            .select_from(latest_runs)
            .where(*fully_ready, latest_runs.c.finished_at >= cutoff)
        )
        or 0
    )
    observed_counts["public_ready"] = int(
        session.scalar(
            select(func.count())
            .select_from(latest_runs)
            .where(*public_ready, latest_runs.c.finished_at >= cutoff)
        )
        or 0
    )
    rates = {
        key: _rate(int(value or 0), window_hours)
        for key, value in observed_counts.items()
    }

    latency = _mapping(
        session,
        """
        SELECT
          count(*) AS sample_count,
          percentile_cont(0.5) WITHIN GROUP (
            ORDER BY EXTRACT(EPOCH FROM (r.started_at - j.created_at))
          ) AS p50_seconds,
          percentile_cont(0.95) WITHIN GROUP (
            ORDER BY EXTRACT(EPOCH FROM (r.started_at - j.created_at))
          ) AS p95_seconds,
          max(EXTRACT(EPOCH FROM (r.started_at - j.created_at))) AS max_seconds
        FROM worker_runs r
        JOIN worker_jobs j ON j.id = r.job_id
        WHERE r.started_at >= :cutoff
        """,
        cutoff=cutoff,
    )
    latency = {
        key: int(value or 0)
        if key == "sample_count"
        else round(float(value), 3) if value is not None else None
        for key, value in latency.items()
    }

    database = _mapping(
        session,
        """
        SELECT
          pg_database_size(current_database()) AS size_bytes,
          blks_read,
          blks_hit,
          temp_files,
          temp_bytes,
          tup_returned,
          tup_fetched,
          tup_inserted,
          tup_updated,
          tup_deleted,
          xact_commit,
          xact_rollback,
          deadlocks,
          blk_read_time,
          blk_write_time,
          stats_reset
        FROM pg_stat_database
        WHERE datname = current_database()
        """,
    )
    numeric_database = {}
    for key, value in database.items():
        if key == "stats_reset":
            numeric_database[key] = value.isoformat() if value else None
        elif isinstance(value, float):
            numeric_database[key] = round(value, 3)
        else:
            numeric_database[key] = int(value or 0)

    storage = _mapping(
        session,
        """
        SELECT
          (SELECT coalesce(sum(size_bytes), 0) FROM firmoteka_raw_artifacts)
            + (SELECT coalesce(sum(
                CASE
                  WHEN coalesce(manifest->>'size_bytes', '') ~ '^[0-9]+$'
                  THEN (manifest->>'size_bytes')::bigint ELSE 0
                END
              ), 0) FROM worker_raw_manifests) AS raw_bytes_known,
          (SELECT coalesce(sum(size_bytes), 0) FROM firmoteka_raw_artifacts
             WHERE retrieved_at >= :cutoff)
            + (SELECT coalesce(sum(
                CASE
                  WHEN coalesce(manifest->>'size_bytes', '') ~ '^[0-9]+$'
                  THEN (manifest->>'size_bytes')::bigint ELSE 0
                END
              ), 0) FROM worker_raw_manifests WHERE created_at >= :cutoff)
              AS raw_bytes_window
        """,
        cutoff=cutoff,
    )
    storage = {key: int(value or 0) for key, value in storage.items()}
    storage["raw_bytes_per_hour"] = round(
        storage["raw_bytes_window"] / window_hours, 3
    )
    storage["postgres_bytes"] = numeric_database["size_bytes"]

    return {
        "observed_at": observed_at.isoformat(),
        "window_hours": window_hours,
        "totals": totals,
        "queues": queues,
        "rates": rates,
        "current_companies_per_day": rates["public_ready"]["per_day"],
        "queue_latency": latency,
        "firmoteka": firmoteka,
        "database": numeric_database,
        "storage": storage,
    }
