"""Read-only public projections for Source Factory facts.

Negative answers are emitted only while the accepted official snapshot remains
current.  A stale, failed, or never-published source returns ``unavailable``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.contracts.data_readiness import OperationalStatus
from app.database.postgres import get_session
from app.models.girbo import GirboAccountingReport
from app.models.mintrans_ted import TransportForwardingRegistryListing
from app.models.source import DataSet


def _is_current(dataset: DataSet | None, *, now: datetime) -> bool:
    return bool(
        dataset is not None
        and dataset.last_success_at is not None
        and dataset.operational_status == OperationalStatus.CURRENT
        and dataset.official_actual_until is not None
        and dataset.official_actual_until >= now.date()
    )


def get_mintrans_ted_check_for_company(
    *, company_id: int, now: datetime | None = None
) -> dict:
    now = now or datetime.now(timezone.utc)
    session = get_session()
    try:
        dataset = session.scalar(
            select(DataSet).where(DataSet.code == "mintrans_ted_registry")
        )
        if not _is_current(dataset, now=now):
            return {
                "result": "unavailable",
                "dataset_code": "mintrans_ted_registry",
                "reason": "official_snapshot_not_current",
            }
        rows = session.scalars(
            select(TransportForwardingRegistryListing)
            .where(
                TransportForwardingRegistryListing.dataset_id == dataset.id,
                TransportForwardingRegistryListing.company_id == company_id,
            )
            .order_by(
                TransportForwardingRegistryListing.effective_from.desc(),
                TransportForwardingRegistryListing.id,
            )
        ).all()
        return {
            "result": "found" if rows else "not_found",
            "dataset_id": dataset.id,
            "dataset_code": dataset.code,
            "data_date": dataset.last_data_date,
            "checked_at": dataset.checked_at,
            "official_actual_until": dataset.official_actual_until,
            "listings": [
                {
                    **row.value,
                    "fact_code": row.fact_code,
                    "effective_from": row.effective_from,
                    "evidence": row.evidence,
                }
                for row in rows
            ],
        }
    finally:
        session.close()


def get_girbo_accounting_check_for_company(
    *, company_id: int, now: datetime | None = None
) -> dict:
    now = now or datetime.now(timezone.utc)
    session = get_session()
    try:
        dataset = session.scalar(
            select(DataSet).where(DataSet.code == "girbo_accounting")
        )
        rows = (
            session.scalars(
                select(GirboAccountingReport)
                .where(
                    GirboAccountingReport.dataset_id == dataset.id,
                    GirboAccountingReport.company_id == company_id,
                    GirboAccountingReport.is_current.is_(True),
                )
                .order_by(GirboAccountingReport.reporting_year.desc())
            ).all()
            if dataset is not None
            else []
        )
        if not rows:
            return {
                "result": "unavailable",
                "dataset_code": "girbo_accounting",
                "reason": (
                    "official_snapshot_not_current"
                    if not _is_current(dataset, now=now)
                    else "company_check_evidence_unavailable"
                ),
            }
        return {
            "result": "found",
            "dataset_id": dataset.id,
            "dataset_code": dataset.code,
            "data_date": dataset.last_data_date,
            "checked_at": dataset.checked_at,
            "official_actual_until": dataset.official_actual_until,
            "freshness": "current" if _is_current(dataset, now=now) else "stale",
            "reports": [
                {
                    "report_id": row.report_id,
                    "reporting_year": row.reporting_year,
                    "publication_date": row.publication_date,
                    "correction_date": row.correction_date,
                    "revenue": row.revenue,
                    "expenses": row.expenses,
                    "profit_loss": row.profit_loss,
                    "assets": row.assets,
                    "liabilities": row.liabilities,
                    "equity": row.equity,
                    "unit_code": str(
                        ((row.statement_values or {}).get("_document") or {}).get(
                            "unit_code"
                        )
                        or ""
                    ),
                    "source_data_date": row.source_data_date,
                }
                for row in rows
            ],
        }
    finally:
        session.close()
