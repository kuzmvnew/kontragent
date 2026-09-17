from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.database.postgres import get_session
from app.models.source import (
    DataSet,
    DataSource,
    IngestionRun,
)
from app.contracts.data_readiness import OperationalStatus


# =========================================================
# TIME
# =========================================================


def utc_now():
    return datetime.now(
        timezone.utc
    )


# =========================================================
# FILE CHECKSUM
# =========================================================


def calculate_file_checksum(
    file_path: str | Path,
    chunk_size: int = 1024 * 1024,
):
    """
    Считает SHA-256 файла.

    Это нужно, чтобы один и тот же
    файл случайно не импортировался
    повторно.
    """

    path = Path(
        file_path
    )

    if not path.exists():
        raise FileNotFoundError(
            f"Файл не найден: {path}"
        )

    digest = sha256()

    with path.open(
        "rb"
    ) as file:

        while True:

            chunk = file.read(
                chunk_size
            )

            if not chunk:
                break

            digest.update(
                chunk
            )

    return digest.hexdigest()


# =========================================================
# HELPERS
# =========================================================


def _merge_details(
    old_details,
    new_details,
):
    result = dict(
        old_details
        or {}
    )

    if new_details:
        result.update(
            new_details
        )

    return (
        result
        if result
        else None
    )


def _run_to_dict(
    run,
):
    return {
        "id": run.id,
        "dataset_id": (
            run.dataset_id
        ),
        "status": (
            run.status
        ),
        "started_at": (
            run.started_at
        ),
        "finished_at": (
            run.finished_at
        ),
        "data_date": (
            run.data_date
        ),
        "source_file_name": (
            run.source_file_name
        ),
        "source_url": (
            run.source_url
        ),
        "file_checksum": (
            run.file_checksum
        ),
        "rows_read": (
            run.rows_read
        ),
        "rows_inserted": (
            run.rows_inserted
        ),
        "rows_updated": (
            run.rows_updated
        ),
        "rows_skipped": (
            run.rows_skipped
        ),
        "errors_count": (
            run.errors_count
        ),
        "error_message": (
            run.error_message
        ),
        "details": (
            run.details
        ),
    }


# =========================================================
# START INGESTION
# =========================================================


def start_ingestion(
    dataset_code: str,
    source_file_name: str | None = None,
    source_url: str | None = None,
    data_date: date | None = None,
    file_checksum: str | None = None,
    details: dict | None = None,
):
    """
    Создаёт новую запись ingestion_runs.

    status = running

    Возвращает ID новой загрузки.
    """

    session = get_session()

    try:
        dataset = (
            session.execute(
                select(DataSet)
                .where(
                    DataSet.code
                    == dataset_code
                )
            )
            .scalar_one_or_none()
        )

        if dataset is None:
            raise ValueError(
                "Dataset не найден: "
                f"{dataset_code}"
            )

        run = IngestionRun(
            dataset_id=dataset.id,
            status="running",
            started_at=utc_now(),
            data_date=data_date,
            source_file_name=(
                source_file_name
            ),
            source_url=(
                source_url
            ),
            file_checksum=(
                file_checksum
            ),
            rows_read=0,
            rows_inserted=0,
            rows_updated=0,
            rows_skipped=0,
            errors_count=0,
            error_message=None,
            details=details,
            run_uuid=str(uuid4()),
            trigger="manual",
            retrieved_at=utc_now(),
            records_seen=0,
            records_written=0,
            records_rejected=0,
            duplicates=0,
            conflicts=0,
        )

        dataset.last_attempt_at = run.started_at
        dataset.operational_status = OperationalStatus.UPDATING

        session.add(
            run
        )

        session.flush()

        run_id = run.id

        session.commit()

        return run_id

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


# =========================================================
# SUCCESS
# =========================================================


def finish_ingestion_success(
    run_id: int,
    rows_read: int = 0,
    rows_inserted: int = 0,
    rows_updated: int = 0,
    rows_skipped: int = 0,
    errors_count: int = 0,
    data_date: date | None = None,
    details: dict | None = None,
):
    """
    Завершает ingestion успешно.

    Одновременно обновляет:

    data_sets.last_success_at
    data_sets.last_data_date
    """

    session = get_session()

    try:
        run = session.get(
            IngestionRun,
            run_id,
        )

        if run is None:
            raise ValueError(
                "IngestionRun не найден: "
                f"{run_id}"
            )

        dataset = session.get(
            DataSet,
            run.dataset_id,
        )

        if dataset is None:
            raise ValueError(
                "Dataset для ingestion "
                "не найден"
            )

        now = utc_now()

        run.status = "success"
        run.finished_at = now

        run.rows_read = (
            rows_read
        )

        run.rows_inserted = (
            rows_inserted
        )

        run.rows_updated = (
            rows_updated
        )

        run.rows_skipped = (
            rows_skipped
        )

        run.errors_count = (
            errors_count
        )

        run.error_message = None

        if data_date is not None:
            run.data_date = data_date

        run.details = _merge_details(
            run.details,
            details,
        )

        dataset.last_success_at = (
            now
        )
        dataset.retrieved_at = now
        dataset.checked_at = now
        dataset.published_at = now
        dataset.record_count = rows_inserted + rows_updated
        dataset.operational_status = OperationalStatus.CURRENT
        dataset.last_error = None
        dataset.last_error_at = None
        dataset.retry_count = 0
        dataset.next_retry_at = None
        run.records_seen = rows_read
        run.records_written = rows_inserted + rows_updated
        run.records_rejected = errors_count
        run.duration_ms = max(0, int((now - run.started_at).total_seconds() * 1000))

        if run.data_date is not None:
            dataset.last_data_date = (
                run.data_date
            )
            dataset.source_as_of = datetime.combine(
                run.data_date,
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            run.source_as_of = dataset.source_as_of

        session.commit()

        return _run_to_dict(
            run
        )

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


# =========================================================
# FAILURE
# =========================================================


def finish_ingestion_failure(
    run_id: int,
    error_message: str,
    rows_read: int = 0,
    rows_inserted: int = 0,
    rows_updated: int = 0,
    rows_skipped: int = 0,
    errors_count: int = 1,
    details: dict | None = None,
):
    """
    Завершает ingestion ошибкой.

    last_success_at dataset
    при ошибке НЕ изменяется.
    """

    session = get_session()

    try:
        run = session.get(
            IngestionRun,
            run_id,
        )

        if run is None:
            raise ValueError(
                "IngestionRun не найден: "
                f"{run_id}"
            )

        run.status = "failed"

        run.finished_at = (
            utc_now()
        )

        run.rows_read = (
            rows_read
        )

        run.rows_inserted = (
            rows_inserted
        )

        run.rows_updated = (
            rows_updated
        )

        run.rows_skipped = (
            rows_skipped
        )

        run.errors_count = (
            errors_count
        )

        run.error_message = str(
            error_message
        )

        dataset = session.get(DataSet, run.dataset_id)
        if dataset is not None:
            dataset.operational_status = OperationalStatus.ERROR
            dataset.last_error = str(error_message)[:1000]
            dataset.last_error_at = run.finished_at
            dataset.retry_count += 1
        run.error_code = (details or {}).get("error_code", "ingestion_failed")
        run.records_seen = rows_read
        run.records_written = rows_inserted + rows_updated
        run.records_rejected = errors_count
        run.duration_ms = max(0, int((run.finished_at - run.started_at).total_seconds() * 1000))

        run.details = _merge_details(
            run.details,
            details,
        )

        session.commit()

        return _run_to_dict(
            run
        )

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


# =========================================================
# DUPLICATE FILE CHECK
# =========================================================


def was_file_successfully_imported(
    dataset_code: str,
    file_checksum: str,
):
    """
    True, если файл с таким SHA-256
    уже успешно импортировался
    для конкретного dataset.
    """

    session = get_session()

    try:
        statement = (
            select(
                IngestionRun.id
            )
            .join(
                DataSet,
                IngestionRun.dataset_id
                == DataSet.id,
            )
            .where(
                DataSet.code
                == dataset_code,
                IngestionRun.file_checksum
                == file_checksum,
                IngestionRun.status
                == "success",
            )
            .limit(1)
        )

        run_id = session.execute(
            statement
        ).scalar_one_or_none()

        return (
            run_id is not None
        )

    finally:
        session.close()


# =========================================================
# GET ONE RUN
# =========================================================


def get_ingestion_run(
    run_id: int,
):
    session = get_session()

    try:
        run = session.get(
            IngestionRun,
            run_id,
        )

        if run is None:
            return None

        return _run_to_dict(
            run
        )

    finally:
        session.close()


# =========================================================
# RECENT RUNS
# =========================================================


def list_recent_ingestion_runs(
    limit: int = 20,
):
    session = get_session()

    try:
        statement = (
            select(
                IngestionRun,
                DataSet,
                DataSource,
            )
            .join(
                DataSet,
                IngestionRun.dataset_id
                == DataSet.id,
            )
            .join(
                DataSource,
                DataSet.source_id
                == DataSource.id,
            )
            .order_by(
                IngestionRun.started_at.desc()
            )
            .limit(
                limit
            )
        )

        rows = (
            session.execute(
                statement
            )
            .all()
        )

        result = []

        for (
            run,
            dataset,
            source,
        ) in rows:

            result.append(
                {
                    "id": run.id,
                    "source": (
                        source.code
                    ),
                    "dataset": (
                        dataset.code
                    ),
                    "status": (
                        run.status
                    ),
                    "started_at": (
                        run.started_at
                    ),
                    "finished_at": (
                        run.finished_at
                    ),
                    "data_date": (
                        run.data_date
                    ),
                    "source_file_name": (
                        run.source_file_name
                    ),
                    "rows_read": (
                        run.rows_read
                    ),
                    "rows_inserted": (
                        run.rows_inserted
                    ),
                    "rows_updated": (
                        run.rows_updated
                    ),
                    "rows_skipped": (
                        run.rows_skipped
                    ),
                    "errors_count": (
                        run.errors_count
                    ),
                    "error_message": (
                        run.error_message
                    ),
                }
            )

        return result

    finally:
        session.close()
