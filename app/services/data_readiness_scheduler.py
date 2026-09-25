"""Small restart-safe scheduler for a solo/MVP deployment.

The database is the schedule and run-history source of truth. No dataset is marked
configured merely because this loop exists: a runnable handler must be registered.
"""

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import time

from sqlalchemy import select

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus, retry_delay
from app.database.postgres import SessionLocal
from app.models.source import DataSet
from app.models.worker import WorkerJob, WorkerRun
from app.services.data_readiness_service import due_dataset_codes, safe_error_message


logger = logging.getLogger("kontragent.data_readiness.scheduler")
UpdateHandler = Callable[[], object]


def _raw_root() -> Path:
    return Path(os.environ.get("FNS_RAW_ROOT", "var/raw/fns")).resolve()


def _enqueue_tax_offence() -> object:
    from app.ingestion.fns_tax_offence import schedule_fns_tax_offence_check

    with SessionLocal() as session:
        creation = schedule_fns_tax_offence_check(session, raw_root=_raw_root())
        session.commit()
        if creation.job.status in {"failed", "cancelled"}:
            raise RuntimeError(f"S04 release job is terminal: {creation.job.id}")
        return creation


def _enqueue_revenue_expense() -> object:
    from app.ingestion.fns_revenue_expense import schedule_fns_revenue_expense_check

    with SessionLocal() as session:
        creation = schedule_fns_revenue_expense_check(session, raw_root=_raw_root())
        session.commit()
        if creation.job.status in {"failed", "cancelled"}:
            raise RuntimeError(f"S03 release job is terminal: {creation.job.id}")
        return creation


def _enqueue_cbr_warning() -> object:
    from app.ingestion.cbr_warning_worker import schedule_cbr_warning_check

    with SessionLocal() as session:
        creation = schedule_cbr_warning_check(session, raw_root=_raw_root())
        session.commit()
        if creation.job.status in {"failed", "cancelled"}:
            raise RuntimeError(
                f"CBR Warning List job is terminal: {creation.job.id}"
            )
        return creation


def _enqueue_tax_payment() -> object:
    from app.ingestion.fns_tax_payment import schedule_fns_tax_payment_check

    with SessionLocal() as session:
        creation = schedule_fns_tax_payment_check(session, raw_root=_raw_root())
        session.commit()
        if creation.job.status in {"failed", "cancelled"}:
            raise RuntimeError(f"PAYTAX release job is terminal: {creation.job.id}")
        return creation


def _enqueue_tax_debt() -> object:
    from app.ingestion.fns_tax_debt_pipeline import schedule_fns_tax_debt_check

    with SessionLocal() as session:
        creation = schedule_fns_tax_debt_check(session, raw_root=_raw_root())
        session.commit()
        if creation.job.status in {"failed", "cancelled"}:
            raise RuntimeError(f"S02 release job is terminal: {creation.job.id}")
        return creation


def _enqueue_headcount() -> object:
    from app.ingestion.fns_headcount import schedule_fns_headcount_check

    with SessionLocal() as session:
        creation = schedule_fns_headcount_check(session, raw_root=_raw_root())
        session.commit()
        if creation.job.status in {"failed", "cancelled"}:
            raise RuntimeError(f"HEADCOUNT release job is terminal: {creation.job.id}")
        return creation


def _enqueue_msp() -> object:
    from app.ingestion.fns_msp import schedule_fns_msp_check

    with SessionLocal() as session:
        creation = schedule_fns_msp_check(session, raw_root=_raw_root())
        session.commit()
        if creation.job.status in {"failed", "cancelled"}:
            raise RuntimeError(f"MSP release job is terminal: {creation.job.id}")
        return creation


def _enqueue_tax_regime() -> object:
    from app.ingestion.fns_tax_regime import schedule_fns_tax_regime_check

    with SessionLocal() as session:
        creation = schedule_fns_tax_regime_check(session, raw_root=_raw_root())
        session.commit()
        if creation.job.status in {"failed", "cancelled"}:
            raise RuntimeError(f"TAX-REGIME release job is terminal: {creation.job.id}")
        return creation


# Production handlers are explicit and non-empty.  They only discover and
# enqueue into Worker Foundation; execution remains lease/fencing controlled.
HANDLERS: dict[str, UpdateHandler] = {
    "fns_tax_offence": _enqueue_tax_offence,
    "fns_revenue_expenses": _enqueue_revenue_expense,
    "cbr_warning_list": _enqueue_cbr_warning,
    "fns_tax_paid": _enqueue_tax_payment,
    "fns_tax_debt": _enqueue_tax_debt,
    "fns_headcount": _enqueue_headcount,
    "fns_msp": _enqueue_msp,
    "fns_tax_regime": _enqueue_tax_regime,
}
FNS_BULK_DATASET_CODES = frozenset(
    {
        "fns_tax_offence",
        "fns_revenue_expenses",
        "fns_tax_debt",
        "fns_tax_paid",
        "fns_headcount",
        "fns_msp",
        "fns_tax_regime",
    }
)
SCHEDULED_SOURCE_DATASET_CODES = frozenset(HANDLERS)


def register_handler(dataset_code: str, handler: UpdateHandler) -> None:
    HANDLERS[dataset_code] = handler


def configure_fns_bulk_schedules(
    *,
    enabled: bool,
    dataset_codes: Iterable[str] | None = None,
    now: datetime | None = None,
) -> None:
    """Explicit, source-scoped gate for FNS official-release checks."""

    now = now or datetime.now(timezone.utc)
    requested = tuple(
        dict.fromkeys(
            FNS_BULK_DATASET_CODES if dataset_codes is None else dataset_codes
        )
    )
    unknown = set(requested) - FNS_BULK_DATASET_CODES
    if unknown:
        raise ValueError("unsupported FNS bulk datasets: " + ", ".join(sorted(unknown)))
    if not requested:
        return
    with SessionLocal() as session:
        datasets = list(
            session.scalars(
                select(DataSet).where(
                    DataSet.code.in_(requested)
                ).with_for_update()
            )
        )
        found = {dataset.code for dataset in datasets}
        missing = set(requested) - found
        if missing:
            raise ValueError("datasets are not registered: " + ", ".join(sorted(missing)))
        for dataset in datasets:
            dataset.enabled = enabled
            dataset.auto_update_status = (
                AutoUpdateStatus.CONFIGURED
                if enabled
                else AutoUpdateStatus.NOT_CONFIGURED
            )
            dataset.next_expected_update_at = now if enabled else None
            if not enabled and dataset.last_success_at is None:
                dataset.operational_status = OperationalStatus.NOT_CONFIGURED
        session.commit()


def configure_source_schedules(
    *,
    enabled: bool,
    dataset_codes: Iterable[str],
    now: datetime | None = None,
) -> None:
    """Enable or disable explicitly supported source schedules."""

    requested = tuple(dict.fromkeys(dataset_codes))
    unknown = set(requested) - SCHEDULED_SOURCE_DATASET_CODES
    if unknown:
        raise ValueError(
            "unsupported scheduled datasets: " + ", ".join(sorted(unknown))
        )
    fns = tuple(code for code in requested if code in FNS_BULK_DATASET_CODES)
    if fns:
        configure_fns_bulk_schedules(
            enabled=enabled, dataset_codes=fns, now=now
        )
    non_fns = tuple(code for code in requested if code not in FNS_BULK_DATASET_CODES)
    if not non_fns:
        return
    now = now or datetime.now(timezone.utc)
    with SessionLocal() as session:
        datasets = list(
            session.scalars(
                select(DataSet)
                .where(DataSet.code.in_(non_fns))
                .with_for_update()
            )
        )
        found = {dataset.code for dataset in datasets}
        missing = set(non_fns) - found
        if missing:
            raise ValueError(
                "datasets are not registered: " + ", ".join(sorted(missing))
            )
        for dataset in datasets:
            dataset.enabled = enabled
            dataset.auto_update_status = (
                AutoUpdateStatus.CONFIGURED
                if enabled
                else AutoUpdateStatus.NOT_CONFIGURED
            )
            dataset.next_expected_update_at = now if enabled else None
            if not enabled and dataset.last_success_at is None:
                dataset.operational_status = OperationalStatus.NOT_CONFIGURED
        session.commit()


def _record_schedule_failure(dataset_code: str, error: Exception) -> None:
    """Expose a scheduler error while preserving the last successful release."""

    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        dataset = session.scalar(
            select(DataSet).where(DataSet.code == dataset_code).with_for_update()
        )
        if dataset is None:
            session.rollback()
            return
        dataset.checked_at = now
        dataset.last_error = safe_error_message(error)
        dataset.last_error_at = now
        dataset.operational_status = OperationalStatus.ERROR
        dataset.retry_count = int(dataset.retry_count or 0) + 1
        dataset.next_retry_at = now + retry_delay(dataset.retry_count)
        session.commit()


def sync_worker_failure_signals() -> int:
    """Project terminal/retrying WorkerRun errors onto dataset readiness.

    WorkerRun remains the detailed error source of truth.  This projection
    intentionally does not alter ``last_success_at``, source date, published
    facts or the active WorkerPublicationState pointer.
    """

    changed = 0
    with SessionLocal() as session:
        rows = session.execute(
            select(WorkerRun, WorkerJob)
            .join(WorkerJob, WorkerRun.job_id == WorkerJob.id)
            .where(
                WorkerRun.status.in_(
                    ("succeeded", "failed", "timed_out", "interrupted")
                )
            )
            .order_by(
                WorkerRun.finished_at.desc().nulls_last(),
                WorkerRun.started_at.desc(),
                WorkerRun.attempt_no.desc(),
                WorkerRun.id.desc(),
            )
        ).all()
        seen: set[str] = set()
        for run, job in rows:
            if job.source_id in seen:
                continue
            seen.add(job.source_id)
            dataset = session.scalar(
                select(DataSet).where(DataSet.code == job.source_id).with_for_update()
            )
            if dataset is None or not run.finished_at:
                continue
            if run.status == "succeeded":
                if dataset.last_error_at and dataset.last_error_at < run.finished_at:
                    from app.ingestion.fns_bulk_worker import (
                        release_operational_status,
                    )

                    dataset.last_error = None
                    dataset.last_error_at = None
                    dataset.retry_count = 0
                    dataset.next_retry_at = None
                    dataset.operational_status = release_operational_status(
                        dataset.official_actual_until,
                        now=datetime.now(timezone.utc),
                    )
                    changed += 1
                continue
            if dataset.last_success_at and dataset.last_success_at >= run.finished_at:
                continue
            if dataset.last_error_at and dataset.last_error_at >= run.finished_at:
                continue
            errors = list(run.errors or [])
            message = errors[-1].get("message") if errors else "worker execution failed"
            dataset.last_error = safe_error_message(message)
            dataset.last_error_at = run.finished_at
            dataset.operational_status = OperationalStatus.ERROR
            dataset.retry_count = int(dataset.retry_count or 0) + 1
            dataset.next_retry_at = (
                job.next_attempt_at
                if job.status == "retry_scheduled"
                else run.finished_at + retry_delay(dataset.retry_count)
            )
            changed += 1
        session.commit()
    return changed


def run_due_updates(*, due_codes: Iterable[str] | None = None) -> dict[str, str]:
    results: dict[str, str] = {}
    codes = list(due_codes if due_codes is not None else due_dataset_codes())
    priority = {
        "fns_tax_offence": 0,
        "fns_revenue_expenses": 1,
        "fns_tax_debt": 2,
        "fns_tax_paid": 3,
        "cbr_warning_list": 4,
        "fns_headcount": 5,
        "fns_msp": 6,
        "fns_tax_regime": 7,
    }
    codes.sort(key=lambda code: (priority.get(code, 100), code))
    for dataset_code in codes:
        handler = HANDLERS.get(dataset_code)
        if handler is None:
            results[dataset_code] = "not_configured"
            logger.error("scheduled_dataset_has_no_handler", extra={"dataset_code": dataset_code})
            continue
        try:
            handler()
        except Exception as error:
            results[dataset_code] = "failed"
            logger.exception("scheduled_dataset_failed", extra={"dataset_code": dataset_code})
            _record_schedule_failure(dataset_code, error)
        else:
            results[dataset_code] = "success"
    return results


def worker_loop(*, poll_seconds: int = 60, after_poll: Callable[[], object] | None = None) -> None:
    if poll_seconds < 5:
        raise ValueError("poll_seconds должен быть не меньше 5")
    while True:
        run_due_updates()
        if after_poll is not None:
            after_poll()
        sync_worker_failure_signals()
        time.sleep(poll_seconds)
