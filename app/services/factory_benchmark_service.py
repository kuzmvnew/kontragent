"""Read-only, reproducible set-based enrichment benchmark."""

from __future__ import annotations

import resource
import time
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session


SET_BASED_MATCH_SQL = """
WITH target AS (
  SELECT id, inn
  FROM companies
  WHERE id = ANY(CAST(:company_ids AS bigint[]))
), matches AS (
  SELECT 'revenue_expense' AS source, f.company_id
    FROM company_revenue_expense_snapshots f JOIN target t ON t.id = f.company_id
  UNION ALL
  SELECT 'tax_offence', f.company_id
    FROM company_tax_offences f JOIN target t ON t.id = f.company_id
  UNION ALL
  SELECT 'tax_paid', f.company_id
    FROM company_tax_payment_snapshots f JOIN target t ON t.id = f.company_id
  UNION ALL
  SELECT 'headcount', f.company_id
    FROM company_headcounts f JOIN target t ON t.id = f.company_id
  UNION ALL
  SELECT 'msp', f.company_id
    FROM company_msp_profiles f JOIN target t ON t.id = f.company_id
)
SELECT
  (SELECT count(*) FROM target) AS target_count,
  count(*) AS matched_fact_rows,
  count(DISTINCT company_id) AS matched_companies,
  count(DISTINCT source) AS matched_sources
FROM matches
"""


def _locks(session: Session) -> int:
    return int(
        session.execute(
            text("SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid()")
        ).scalar()
        or 0
    )


def _wal_lsn(session: Session) -> str | None:
    try:
        return str(session.execute(text("SELECT pg_current_wal_lsn()")).scalar())
    except DBAPIError:
        session.rollback()
        return None


def _wal_delta(session: Session, before: str | None, after: str | None) -> int | None:
    if before is None or after is None:
        return None
    return int(
        session.execute(
            text("SELECT pg_wal_lsn_diff(CAST(:after AS pg_lsn), CAST(:before AS pg_lsn))"),
            {"after": after, "before": before},
        ).scalar()
        or 0
    )


def benchmark_set_based_batches(
    session: Session,
    *,
    batch_sizes: Sequence[int] = (100, 500, 1_000, 5_000),
) -> list[dict[str, Any]]:
    """Execute SELECT-only exact-identity joins and return PostgreSQL evidence."""

    results: list[dict[str, Any]] = []
    for requested_size in batch_sizes:
        if requested_size <= 0 or requested_size > 5_000:
            raise ValueError("benchmark batch sizes must be between 1 and 5000")
        company_ids = list(
            session.execute(
                text("SELECT id FROM companies ORDER BY id LIMIT :limit"),
                {"limit": requested_size},
            ).scalars()
        )
        if not company_ids:
            results.append(
                {"requested_batch_size": requested_size, "sample_size": 0}
            )
            continue
        parameters = {"company_ids": company_ids}
        locks_before = _locks(session)
        wal_before = _wal_lsn(session)
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        cpu_before = time.process_time()
        wall_before = time.perf_counter()
        row = dict(session.execute(text(SET_BASED_MATCH_SQL), parameters).mappings().one())
        wall_ms = (time.perf_counter() - wall_before) * 1000.0
        cpu_ms = (time.process_time() - cpu_before) * 1000.0
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        plan_value = session.execute(
            text("EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) " + SET_BASED_MATCH_SQL),
            parameters,
        ).scalar()
        plan = plan_value[0] if isinstance(plan_value, list) else plan_value
        wal_after = _wal_lsn(session)
        locks_after = _locks(session)
        records = int(row.get("target_count") or 0)
        results.append(
            {
                "requested_batch_size": requested_size,
                "sample_size": len(company_ids),
                "target_records": records,
                "matched_fact_rows": int(row.get("matched_fact_rows") or 0),
                "matched_companies": int(row.get("matched_companies") or 0),
                "matched_sources": int(row.get("matched_sources") or 0),
                "records_per_second": round(
                    records / max(wall_ms / 1000.0, 0.000001), 3
                ),
                "client_wall_ms": round(wall_ms, 3),
                "client_cpu_ms": round(cpu_ms, 3),
                "database_planning_ms": round(
                    float(plan.get("Planning Time") or 0), 3
                ),
                "database_execution_ms": round(
                    float(plan.get("Execution Time") or 0), 3
                ),
                "memory_max_rss_delta": max(0, int(rss_after - rss_before)),
                "locks_before": locks_before,
                "locks_after": locks_after,
                "wal_bytes": _wal_delta(session, wal_before, wal_after),
                "plan": plan.get("Plan"),
            }
        )
    return results


def verify_benchmark_failure_recovery(session: Session) -> bool:
    """Prove a timed-out benchmark transaction recovers without persisted work."""

    try:
        session.execute(text("SET LOCAL statement_timeout = '1ms'"))
        session.execute(text("SELECT pg_sleep(0.02)"))
    except DBAPIError:
        session.rollback()
    else:
        session.rollback()
        return False
    return int(session.execute(text("SELECT 1")).scalar() or 0) == 1
