"""Persistence and projections for operational dataset readiness."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import logging
import re
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.contracts.data_readiness import (
    AutoUpdateStatus,
    OperationalStatus,
    is_dataset_stale,
    retry_delay,
)
from app.database.postgres import get_session
from app.models.source import DataSet, DataSource, DatasetUpdateLock, IngestionRun


logger = logging.getLogger("kontragent.data_readiness")
SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|token|cookie|authorization)(\s*[:=]\s*)([^\s,;]+)")


class DatasetLockedError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def safe_error_message(value: object, *, limit: int = 1000) -> str:
    text = SECRET_PATTERN.sub(r"\1\2[REDACTED]", str(value))
    return text[:limit]


def acquire_dataset_lock(
    dataset_code: str,
    owner: str,
    *,
    now: datetime | None = None,
    timeout: timedelta = timedelta(hours=2),
) -> bool:
    """Acquire a committed cross-process lock or recover an expired one."""
    now = now or utc_now()
    session = get_session()
    try:
        dataset_id = session.scalar(select(DataSet.id).where(DataSet.code == dataset_code))
        if dataset_id is None:
            raise ValueError(f"Dataset не найден: {dataset_code}")
        lock = session.get(DatasetUpdateLock, dataset_id, with_for_update=True)
        if lock is not None and lock.expires_at > now and lock.owner != owner:
            session.rollback()
            return False
        if lock is None:
            session.add(DatasetUpdateLock(
                dataset_id=dataset_id, owner=owner, acquired_at=now, expires_at=now + timeout,
            ))
        else:
            lock.owner = owner
            lock.acquired_at = now
            lock.expires_at = now + timeout
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def release_dataset_lock(dataset_code: str, owner: str) -> None:
    session = get_session()
    try:
        dataset_id = session.scalar(select(DataSet.id).where(DataSet.code == dataset_code))
        if dataset_id is not None:
            session.execute(delete(DatasetUpdateLock).where(
                DatasetUpdateLock.dataset_id == dataset_id,
                DatasetUpdateLock.owner == owner,
            ))
            session.commit()
    finally:
        session.close()


def start_update_run(
    dataset_code: str,
    *,
    trigger: str,
    lock_owner: str,
    now: datetime | None = None,
) -> int:
    now = now or utc_now()
    session = get_session()
    try:
        dataset = session.scalar(select(DataSet).where(DataSet.code == dataset_code).with_for_update())
        if dataset is None:
            raise ValueError(f"Dataset не найден: {dataset_code}")
        run = IngestionRun(
            dataset_id=dataset.id,
            run_uuid=str(uuid4()),
            status="running",
            trigger=trigger,
            lock_owner=lock_owner,
            started_at=now,
            retrieved_at=now,
            rows_read=0,
            rows_inserted=0,
            rows_updated=0,
            rows_skipped=0,
            errors_count=0,
            records_seen=0,
            records_written=0,
            records_rejected=0,
            duplicates=0,
            conflicts=0,
        )
        dataset.last_attempt_at = now
        dataset.operational_status = OperationalStatus.UPDATING
        session.add(run)
        session.flush()
        run_id = run.id
        session.commit()
        logger.info("dataset_update_started", extra={
            "run_id": run.run_uuid, "source_code": None, "dataset_code": dataset_code,
            "started_at": now.isoformat(), "status": "running",
        })
        return run_id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def finish_update_run(
    run_id: int,
    *,
    source_as_of: datetime | None = None,
    retrieved_at: datetime | None = None,
    published_at: datetime | None = None,
    record_count: int = 0,
    coverage: dict | None = None,
    records_rejected: int = 0,
    duplicates: int = 0,
    conflicts: int = 0,
    checksum: str | None = None,
    version: str | None = None,
    now: datetime | None = None,
) -> None:
    now = now or utc_now()
    session = get_session()
    try:
        run = session.get(IngestionRun, run_id, with_for_update=True)
        if run is None:
            raise ValueError(f"IngestionRun не найден: {run_id}")
        dataset = session.get(DataSet, run.dataset_id, with_for_update=True)
        run.status = "success"
        run.finished_at = now
        run.duration_ms = max(0, int((now - run.started_at).total_seconds() * 1000))
        run.source_as_of = source_as_of
        run.retrieved_at = retrieved_at or now
        run.records_seen = record_count + records_rejected
        run.records_written = record_count
        run.records_rejected = records_rejected
        run.duplicates = duplicates
        run.conflicts = conflicts
        run.rows_read = run.records_seen
        run.rows_inserted = record_count
        run.file_checksum = checksum
        run.version = version
        dataset.last_success_at = now
        dataset.source_as_of = source_as_of
        dataset.retrieved_at = retrieved_at or now
        dataset.checked_at = now
        dataset.published_at = published_at or now
        dataset.record_count = record_count
        dataset.coverage = coverage
        dataset.operational_status = OperationalStatus.CURRENT
        dataset.last_error = None
        dataset.last_error_at = None
        dataset.retry_count = 0
        dataset.next_retry_at = None
        dataset.next_expected_update_at = _next_expected(dataset.freshness_policy, now)
        session.commit()
        logger.info("dataset_update_finished", extra={
            "run_id": run.run_uuid, "dataset_code": dataset.code, "duration": run.duration_ms,
            "status": "success", "records": record_count,
        })
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def fail_update_run(
    run_id: int,
    *,
    error_code: str,
    error_message: object,
    retryable: bool = False,
    source_blocked: bool = False,
    retry_after_seconds: int | None = None,
    now: datetime | None = None,
) -> None:
    now = now or utc_now()
    message = safe_error_message(error_message)
    session = get_session()
    try:
        run = session.get(IngestionRun, run_id, with_for_update=True)
        if run is None:
            raise ValueError(f"IngestionRun не найден: {run_id}")
        dataset = session.get(DataSet, run.dataset_id, with_for_update=True)
        run.status = "failed"
        run.finished_at = now
        run.duration_ms = max(0, int((now - run.started_at).total_seconds() * 1000))
        run.errors_count = 1
        run.error_code = error_code
        run.error_message = message
        dataset.last_error = message
        dataset.last_error_at = now
        dataset.retry_count += 1
        if source_blocked:
            dataset.operational_status = OperationalStatus.SOURCE_BLOCKED
            dataset.auto_update_status = AutoUpdateStatus.SOURCE_BLOCKED
            dataset.next_retry_at = now + retry_delay(
                dataset.retry_count, base_seconds=6 * 60 * 60, maximum_seconds=7 * 24 * 60 * 60,
            )
        else:
            dataset.operational_status = OperationalStatus.ERROR
            dataset.next_retry_at = (
                now + retry_delay(dataset.retry_count, retry_after_seconds=retry_after_seconds)
                if retryable else None
            )
        session.commit()
        logger.warning("dataset_update_failed", extra={
            "run_id": run.run_uuid, "dataset_code": dataset.code, "duration": run.duration_ms,
            "status": dataset.operational_status, "error_type": error_code,
        })
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _next_expected(policy: str, reference: datetime) -> datetime | None:
    return {
        "daily": reference + timedelta(days=1),
        "weekly": reference + timedelta(days=7),
        "monthly": reference + timedelta(days=31),
    }.get(policy)


def clean_negative_blocker(
    dataset: DataSet,
    *,
    now: datetime | None = None,
) -> str | None:
    """Explain why an official snapshot cannot prove a clean negative."""

    now = now or utc_now()
    stored = str(
        getattr(dataset, "operational_status", OperationalStatus.CURRENT.value)
    )
    if stored != OperationalStatus.CURRENT.value:
        return f"dataset_{stored}"
    actual_until = getattr(dataset, "official_actual_until", None)
    if actual_until is None:
        return "dataset_freshness_unavailable"
    if now.astimezone(timezone.utc).date() > actual_until:
        return "dataset_stale"
    return None


def effective_status(dataset: DataSet, *, now: datetime) -> str:
    fixed = {
        OperationalStatus.UPDATING,
        OperationalStatus.ERROR,
        OperationalStatus.UNAVAILABLE,
        OperationalStatus.SOURCE_BLOCKED,
        OperationalStatus.ACCESS_PENDING,
        OperationalStatus.MANUAL,
        OperationalStatus.USER_TRIGGERED,
        OperationalStatus.NOT_CONFIGURED,
    }
    try:
        stored = OperationalStatus(dataset.operational_status)
    except ValueError:
        stored = OperationalStatus.NOT_CONFIGURED
    if stored in fixed:
        return stored.value
    # Official validity is an inclusive date boundary.  It is independent of
    # when our scheduler last checked the passport and takes precedence over
    # policy-based age thresholds.
    if (
        dataset.official_actual_until is not None
        and now.astimezone(timezone.utc).date() > dataset.official_actual_until
    ):
        return OperationalStatus.STALE.value
    stale = is_dataset_stale(
        now=now,
        freshness_policy=dataset.freshness_policy,
        source_as_of=dataset.source_as_of,
        last_success_at=dataset.last_success_at,
        threshold_seconds=dataset.freshness_threshold_seconds,
    )
    return (OperationalStatus.STALE if stale else OperationalStatus.CURRENT).value


def get_data_readiness(*, now: datetime | None = None) -> dict:
    now = now or utc_now()
    session = get_session()
    try:
        rows = session.execute(
            select(DataSet, DataSource).join(DataSource, DataSet.source_id == DataSource.id)
            .order_by(DataSource.name, DataSet.name)
        ).all()
        datasets = []
        counts = Counter()
        review_candidates = []
        for dataset, source in rows:
            status = effective_status(dataset, now=now)
            counts[status] += 1
            if dataset.checked_at:
                review_candidates.append(dataset.checked_at)
            datasets.append({
                "source_code": source.code,
                "source": source.name,
                "dataset_code": dataset.code,
                "dataset": dataset.name,
                "mode": dataset.dataset_kind,
                "update_mode": dataset.update_mode,
                "status": status,
                "source_as_of": dataset.source_as_of,
                "last_attempt_at": dataset.last_attempt_at,
                "last_success_at": dataset.last_success_at,
                "retrieved_at": dataset.retrieved_at,
                "official_actual_until": dataset.official_actual_until,
                "published_at": dataset.published_at,
                "freshness_policy": dataset.freshness_policy,
                "record_count": dataset.record_count,
                "coverage": dataset.coverage,
                "auto_update": dataset.auto_update_status,
                "next_expected_update_at": dataset.next_expected_update_at,
                "next_retry_at": dataset.next_retry_at,
                "last_error": safe_error_message(dataset.last_error) if dataset.last_error else None,
            })
        summary = {"total_datasets": len(datasets)}
        for status in OperationalStatus:
            summary[status.value] = counts[status.value]
        # This is an operational review timestamp, never labelled a full data update.
        summary["last_full_operational_review"] = min(review_candidates) if len(review_candidates) == len(datasets) else None
        history_rows = session.execute(
            select(IngestionRun, DataSet, DataSource)
            .join(DataSet, IngestionRun.dataset_id == DataSet.id)
            .join(DataSource, DataSet.source_id == DataSource.id)
            .order_by(IngestionRun.started_at.desc())
            .limit(50)
        ).all()
        recent_runs = [{
            "run_id": run.run_uuid or str(run.id),
            "source_code": source.code,
            "dataset_code": dataset.code,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "status": run.status,
            "source_as_of": run.source_as_of,
            "retrieved_at": run.retrieved_at,
            "records_seen": run.records_seen or run.rows_read,
            "records_written": run.records_written or (run.rows_inserted + run.rows_updated),
            "records_rejected": run.records_rejected or run.errors_count,
            "duplicates": run.duplicates,
            "conflicts": run.conflicts,
            "checksum": run.file_checksum,
            "version": run.version,
            "error_code": run.error_code,
            "error_message": safe_error_message(run.error_message) if run.error_message else None,
            "duration_ms": run.duration_ms,
        } for run, dataset, source in history_rows]
        return {
            "generated_at": now,
            "summary": summary,
            "datasets": datasets,
            "recent_runs": recent_runs,
        }
    finally:
        session.close()


def due_dataset_codes(*, now: datetime | None = None) -> list[str]:
    now = now or utc_now()
    session = get_session()
    try:
        return list(session.scalars(
            select(DataSet.code).where(
                DataSet.enabled.is_(True),
                DataSet.auto_update_status == AutoUpdateStatus.CONFIGURED,
                (DataSet.next_retry_at.is_(None) | (DataSet.next_retry_at <= now)),
                (DataSet.next_expected_update_at.is_(None) | (DataSet.next_expected_update_at <= now)),
            ).order_by(DataSet.code)
        ))
    finally:
        session.close()
