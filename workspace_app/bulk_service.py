"""Customer Workspace Bulk Check over accepted canonical/public data only."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.workspace import WorkspaceAuditEvent, WorkspaceBulkItem, WorkspaceBulkJob
from public_app.contracts import valid_legal_inn
from workspace_app.report_service import (
    ReportExportValidationError,
    _csv_safe_text,
    canonical_json_bytes,
)
from workspace_app.service import ActionDenied, authorize


JOB_SCHEMA_VERSION = "workspace-bulk-check-v1"
RESULT_SCHEMA_VERSION = "workspace-bulk-result-v1"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_BULK_MULTIPART_BODY_BYTES = MAX_FILE_BYTES + 64 * 1024
MAX_TOTAL_ROWS = 5_000
MAX_BULK_PAGE = MAX_TOTAL_ROWS
MAX_NORMALIZED_INN_LENGTH = 32
DEFAULT_CHUNK_SIZE = 75
MAX_CHUNK_SIZE = 100
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
JOB_STATUSES = frozenset(("READY", "RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELLED", "FAILED"))
ITEM_STATUSES = frozenset(("PENDING", "INVALID_INN", "DUPLICATE", "READY", "NOT_RESOLVED", "NOT_READY", "PROCESSING_ERROR", "CANCELLED"))
TERMINAL_JOB_STATUSES = frozenset(("COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELLED", "FAILED"))
PROCESSED_ITEM_STATUSES = frozenset(("READY", "NOT_RESOLVED", "NOT_READY", "PROCESSING_ERROR"))
SECTION_KEYS = (
    "identity", "status", "registration", "address", "activity", "management",
    "founders", "contacts", "capital", "finances", "employees", "tax",
    "enforcement", "licenses", "courts", "bankruptcy", "procurement",
    "restrictions", "inspections", "connections", "events", "risk", "summary",
    "source_coverage", "freshness", "limitations",
)
BULK_CSV_COLUMNS = (
    "row_number", "input_inn", "inn", "status", "error_code",
    "duplicate_of_row", "company_name", "legal_status", "risk_state",
    "risk_status", "summary", "assessment_date", "section_states",
    "public_release_id", "checked_at", "company_url",
)


@dataclass(frozen=True)
class ParsedBulkRow:
    row_number: int
    raw_inn: str
    normalized_inn: str | None
    status: str
    duplicate_of_row: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class BulkItemsPage:
    items: tuple[WorkspaceBulkItem, ...]
    page: int
    page_size: int
    total: int
    pages: int


class BulkExportValidationError(ActionDenied):
    def __init__(self) -> None:
        super().__init__(
            "bulk_export_invalid_value",
            "Экспорт CSV остановлен: результаты содержат недопустимый управляющий символ.",
            status_code=422,
        )


def _file_error(code: str, message: str, status_code: int = 422) -> ActionDenied:
    return ActionDenied(code, message, status_code=status_code)


def safe_filename(filename: str | None) -> str:
    name = str(filename or "upload.csv").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf", "Cs"} else character
        for character in name
    )
    cleaned = " ".join(cleaned.split()).strip(" .")
    return (cleaned or "upload.csv")[:240]


def _delimiter_for(text: str) -> str:
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    if not first_line:
        raise _file_error("bulk_empty_file", "CSV-файл пуст.")
    present = [delimiter for delimiter in (",", ";", "\t") if delimiter in first_line]
    if len(present) > 1:
        raise _file_error("bulk_ambiguous_delimiter", "Не удалось однозначно определить разделитель CSV.")
    return present[0] if present else ","


def parse_bulk_csv(content: bytes) -> tuple[ParsedBulkRow, ...]:
    if len(content) > MAX_FILE_BYTES:
        raise _file_error("bulk_file_too_large", "CSV-файл превышает лимит 2 МиБ.", 413)
    if not content:
        raise _file_error("bulk_empty_file", "CSV-файл пуст.")
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise _file_error("bulk_invalid_utf8", "CSV должен быть в кодировке UTF-8.") from exc
    if "\x00" in text:
        raise _file_error("bulk_invalid_character", "CSV содержит недопустимый NUL-символ.")
    delimiter = _delimiter_for(text)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        header = next(reader)
    except (csv.Error, StopIteration) as exc:
        raise _file_error("bulk_malformed_csv", "CSV имеет некорректную структуру.") from exc
    normalized_header = [str(value).strip().casefold() for value in header]
    inn_indexes = [index for index, value in enumerate(normalized_header) if value in {"inn", "инн"}]
    if not inn_indexes:
        raise _file_error("bulk_missing_inn_header", "В CSV отсутствует колонка INN/ИНН.")
    if len(inn_indexes) > 1:
        raise _file_error("bulk_duplicate_inn_header", "В CSV несколько колонок INN/ИНН.")
    if len(set(normalized_header)) != len(normalized_header):
        raise _file_error("bulk_duplicate_header", "В CSV повторяются названия колонок.")
    inn_index = inn_indexes[0]
    rows: list[ParsedBulkRow] = []
    first_valid_row: dict[str, int] = {}
    try:
        for values in reader:
            row_number = reader.line_num
            if not values:
                continue
            if len(values) != len(header):
                raise _file_error("bulk_malformed_row", f"Строка {row_number} не соответствует заголовку CSV.")
            if len(rows) >= MAX_TOTAL_ROWS:
                raise _file_error("bulk_too_many_rows", f"CSV превышает технический лимит {MAX_TOTAL_ROWS} строк.", 413)
            raw = str(values[inn_index])
            normalized = raw.strip()
            if not normalized:
                rows.append(ParsedBulkRow(row_number, raw, None, "INVALID_INN", error_code="empty_inn", error_message="ИНН не указан."))
            elif len(normalized) == 12 and normalized.isdigit():
                rows.append(ParsedBulkRow(row_number, raw, normalized, "INVALID_INN", error_code="ip_inn_unsupported", error_message="12-значный ИНН ИП не поддерживается в Bulk Check P0."))
            elif len(normalized) > MAX_NORMALIZED_INN_LENGTH:
                rows.append(ParsedBulkRow(row_number, raw, None, "INVALID_INN", error_code="inn_too_long", error_message="Значение ИНН превышает допустимую длину."))
            elif not valid_legal_inn(normalized):
                rows.append(ParsedBulkRow(row_number, raw, normalized, "INVALID_INN", error_code="invalid_legal_inn", error_message="Требуется корректный 10-значный ИНН юридического лица."))
            elif normalized in first_valid_row:
                rows.append(ParsedBulkRow(row_number, raw, normalized, "DUPLICATE", duplicate_of_row=first_valid_row[normalized], error_code="duplicate_inn", error_message="Повтор ранее загруженного ИНН."))
            else:
                first_valid_row[normalized] = row_number
                rows.append(ParsedBulkRow(row_number, raw, normalized, "PENDING"))
    except csv.Error as exc:
        raise _file_error("bulk_malformed_csv", "CSV имеет некорректное экранирование или кавычки.") from exc
    return tuple(rows)


def _audit(session: Session, *, workspace_id: UUID, user_id: UUID, action: str, job_id: UUID, outcome: str) -> None:
    session.add(WorkspaceAuditEvent(id=uuid4(), workspace_id=workspace_id, actor_user_id=user_id, action=action, target_type="workspace_bulk_job", target_ref=str(job_id), outcome=outcome))


def create_bulk_job(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
    filename: str | None,
    content: bytes,
) -> WorkspaceBulkJob:
    context = authorize(session, user_id=user_id, workspace_id=workspace_id, permission_key="bulk.create", lock_entitlement=True)
    parsed = parse_bulk_csv(content)
    unique_valid_count = sum(row.status == "PENDING" for row in parsed)
    if context.quota_limit is not None and unique_valid_count > context.quota_limit:
        raise ActionDenied("bulk_quota_exceeded", f"Лимит Bulk Check: {context.quota_limit} уникальных корректных ИНН.", status_code=422)
    job = WorkspaceBulkJob(
        id=uuid4(), workspace_id=workspace_id, created_by_user_id=user_id,
        schema_version=JOB_SCHEMA_VERSION,
        status="READY" if unique_valid_count else "COMPLETED",
        original_filename=safe_filename(filename),
        input_sha256=hashlib.sha256(content).hexdigest(),
        total_rows=len(parsed), unique_valid_count=unique_valid_count,
        invalid_count=sum(row.status == "INVALID_INN" for row in parsed),
        duplicate_count=sum(row.status == "DUPLICATE" for row in parsed),
        completed_at=datetime.now(timezone.utc) if not unique_valid_count else None,
    )
    session.add(job)
    # The composite tenant FK is intentional; flush its parent before adding
    # row objects so SQLAlchemy never leaves ordering to mapper heuristics.
    session.flush()
    session.add_all(
        WorkspaceBulkItem(
            id=uuid4(), workspace_id=workspace_id, job_id=job.id,
            row_number=row.row_number, raw_inn=row.raw_inn,
            normalized_inn=row.normalized_inn, status=row.status,
            duplicate_of_row=row.duplicate_of_row, error_code=row.error_code,
            error_message=row.error_message,
        )
        for row in parsed
    )
    _audit(session, workspace_id=workspace_id, user_id=user_id, action="bulk.create", job_id=job.id, outcome="success")
    session.flush()
    return job


def _load_job(session: Session, *, workspace_id: UUID, job_id: UUID, lock: bool = False) -> WorkspaceBulkJob:
    query = sa.select(WorkspaceBulkJob).where(WorkspaceBulkJob.id == job_id, WorkspaceBulkJob.workspace_id == workspace_id)
    if lock:
        query = query.with_for_update()
    job = session.scalar(query)
    if job is None:
        raise ActionDenied("bulk_job_not_found", "Задание Bulk Check не найдено.", status_code=404)
    return job


def get_bulk_job(session: Session, *, user_id: UUID, workspace_id: UUID, job_id: UUID, permission_key: str = "bulk.view") -> WorkspaceBulkJob:
    authorize(session, user_id=user_id, workspace_id=workspace_id, permission_key=permission_key)
    return _load_job(session, workspace_id=workspace_id, job_id=job_id)


def list_bulk_jobs(session: Session, *, user_id: UUID, workspace_id: UUID, limit: int = 50) -> tuple[WorkspaceBulkJob, ...]:
    authorize(session, user_id=user_id, workspace_id=workspace_id, permission_key="bulk.view")
    bounded = max(1, min(int(limit), 100))
    return tuple(session.scalars(sa.select(WorkspaceBulkJob).where(WorkspaceBulkJob.workspace_id == workspace_id).order_by(WorkspaceBulkJob.created_at.desc(), WorkspaceBulkJob.id.desc()).limit(bounded)).all())


def list_bulk_items(
    session: Session, *, user_id: UUID, workspace_id: UUID, job_id: UUID,
    status: str | None = None, query: str = "", page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE, permission_key: str = "bulk.view",
) -> BulkItemsPage:
    authorize(session, user_id=user_id, workspace_id=workspace_id, permission_key=permission_key)
    job = _load_job(session, workspace_id=workspace_id, job_id=job_id)
    if status and status not in ITEM_STATUSES:
        raise ActionDenied("bulk_status_invalid", "Неизвестный статус строки.", status_code=422)
    normalized_query = " ".join(str(query or "").split())[:160]
    filters = [WorkspaceBulkItem.workspace_id == workspace_id, WorkspaceBulkItem.job_id == job_id]
    if status:
        filters.append(WorkspaceBulkItem.status == status)
    if normalized_query:
        filters.append(sa.or_(WorkspaceBulkItem.raw_inn.contains(normalized_query, autoescape=True), WorkspaceBulkItem.normalized_inn.contains(normalized_query, autoescape=True)))
    try:
        requested_page = int(page)
        requested_size = int(page_size)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ActionDenied("bulk_page_invalid", "Некорректная страница результатов.", status_code=422) from exc
    if requested_page > MAX_BULK_PAGE:
        raise ActionDenied("bulk_page_invalid", "Номер страницы превышает допустимый предел.", status_code=422)
    bounded_page = max(1, requested_page)
    bounded_size = max(1, min(requested_size, MAX_PAGE_SIZE))
    total = int(session.scalar(sa.select(sa.func.count()).select_from(WorkspaceBulkItem).where(*filters)) or 0)
    pages = max(1, (total + bounded_size - 1) // bounded_size)
    if bounded_page > pages:
        items: tuple[WorkspaceBulkItem, ...] = ()
    else:
        offset = (bounded_page - 1) * bounded_size
        if offset > MAX_TOTAL_ROWS:
            raise ActionDenied("bulk_page_invalid", "Смещение страницы превышает допустимый предел.", status_code=422)
        items = tuple(session.scalars(sa.select(WorkspaceBulkItem).where(*filters).order_by(WorkspaceBulkItem.row_number).offset(offset).limit(bounded_size)).all())
    _validate_bulk_items_result_integrity(items, job)
    return BulkItemsPage(items, bounded_page, bounded_size, total, pages)


def _pin_release(job: WorkspaceBulkJob, repository: Any) -> None:
    if job.public_release_id:
        return
    try:
        release = repository.active_release()
    except Exception as exc:
        raise ActionDenied("bulk_public_release_unavailable", "Публичный выпуск временно недоступен; повторите обработку позже.", status_code=409) from exc
    if not release:
        raise ActionDenied("bulk_public_release_unavailable", "Нет готового публичного выпуска для проверки.", status_code=409)
    expected = int(release.get("record_count") or 0)
    actual = int(release.get("actual_record_count") or 0)
    if expected <= 0 or expected != actual:
        raise ActionDenied("bulk_public_release_incoherent", "Публичный выпуск ещё не готов; повторите обработку позже.", status_code=409)
    job.public_release_id = str(release["release_id"])
    job.public_schema_version = str(release.get("schema_version") or "public-projection-v1")


def _section_states(projection: Any) -> dict[str, str]:
    states: dict[str, str] = {"identity": "FOUND"}
    if projection.company_view is not None:
        for section in projection.company_view.sections:
            if section.section_key in SECTION_KEYS:
                states[section.section_key] = section.state
    states["risk"] = projection.risk.state.value
    states["summary"] = "FOUND"
    states["source_coverage"] = "FOUND" if projection.sources else "NOT_CHECKED"
    return {key: states[key] for key in SECTION_KEYS if key in states}


def _result_payload(projection: Any, *, release_id: str, checked_at: datetime) -> dict[str, Any]:
    if projection.publication.release_id != release_id:
        raise ValueError("projection release mismatch")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "release_id": release_id,
        "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
        "company": {
            "inn": projection.company.inn,
            "name": projection.company.name,
            "legal_status": projection.company.legal_status,
        },
        "risk": {
            "state": projection.risk.state.value,
            "public_status": projection.risk.public_status,
            "title": projection.risk.public_title,
            "explanation": projection.risk.public_explanation,
            "assessment_date": projection.risk.assessment_date.isoformat(),
        },
        "summary": {"short_conclusion": projection.public_conclusion},
        "section_states": _section_states(projection),
        "limitations_count": len(projection.public_limitations),
        "result_date": projection.publication.result_date.isoformat(),
    }


def _refresh_counters(session: Session, job: WorkspaceBulkJob) -> dict[str, int]:
    counts = {status: 0 for status in ITEM_STATUSES}
    for status, count in session.execute(sa.select(WorkspaceBulkItem.status, sa.func.count()).where(WorkspaceBulkItem.workspace_id == job.workspace_id, WorkspaceBulkItem.job_id == job.id).group_by(WorkspaceBulkItem.status)):
        counts[str(status)] = int(count)
    job.invalid_count = counts["INVALID_INN"]
    job.duplicate_count = counts["DUPLICATE"]
    job.ready_count = counts["READY"]
    job.not_resolved_count = counts["NOT_RESOLVED"]
    job.not_ready_count = counts["NOT_READY"]
    job.failed_count = counts["PROCESSING_ERROR"]
    job.cancelled_count = counts["CANCELLED"]
    job.processed_count = sum(counts[status] for status in PROCESSED_ITEM_STATUSES)
    return counts


def _resolve_bulk_job_status(
    job: WorkspaceBulkJob,
    counts: dict[str, int],
    *,
    processing: bool = False,
    now: datetime | None = None,
) -> str:
    """Resolve lifecycle state from item truth without stranding cancelled work."""

    resolved_at = now or datetime.now(timezone.utc)
    if counts["PENDING"]:
        job.status = "RUNNING" if processing else "READY"
        job.completed_at = None
    elif counts["CANCELLED"]:
        job.status = "CANCELLED"
        job.completed_at = resolved_at
    elif counts["PROCESSING_ERROR"]:
        job.status = "COMPLETED_WITH_ERRORS"
        job.completed_at = resolved_at
    else:
        job.status = "COMPLETED"
        job.completed_at = resolved_at
    return job.status


def _bulk_result_integrity_error() -> ActionDenied:
    return ActionDenied(
        "bulk_result_integrity_failed",
        "Сохранённый результат Bulk Check не прошёл проверку целостности.",
        status_code=409,
    )


def _validate_bulk_item_result_integrity(
    item: WorkspaceBulkItem,
    job: WorkspaceBulkJob,
) -> None:
    """Fail closed when a stored result is incomplete, altered, or rebound."""

    if item.status != "READY":
        if item.result_payload is not None or item.result_sha256 is not None:
            raise _bulk_result_integrity_error()
        return
    payload = item.result_payload
    stored_hash = item.result_sha256
    if not isinstance(payload, dict) or not isinstance(stored_hash, str):
        raise _bulk_result_integrity_error()
    if len(stored_hash) != 64 or any(character not in "0123456789abcdef" for character in stored_hash):
        raise _bulk_result_integrity_error()
    try:
        actual_hash = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    except Exception as exc:
        raise _bulk_result_integrity_error() from exc
    if not hmac.compare_digest(actual_hash, stored_hash):
        raise _bulk_result_integrity_error()
    company = payload.get("company")
    if (
        payload.get("schema_version") != RESULT_SCHEMA_VERSION
        or not job.public_release_id
        or payload.get("release_id") != job.public_release_id
        or not isinstance(company, dict)
        or not item.normalized_inn
        or company.get("inn") != item.normalized_inn
    ):
        raise _bulk_result_integrity_error()


def _validate_bulk_items_result_integrity(
    items: tuple[WorkspaceBulkItem, ...] | list[WorkspaceBulkItem],
    job: WorkspaceBulkJob,
) -> None:
    for item in items:
        _validate_bulk_item_result_integrity(item, job)


def process_bulk_job_chunk(
    session: Session, *, user_id: UUID, workspace_id: UUID, job_id: UUID,
    projection_repository: Any, chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> WorkspaceBulkJob:
    authorize(session, user_id=user_id, workspace_id=workspace_id, permission_key="bulk.manage")
    job = _load_job(session, workspace_id=workspace_id, job_id=job_id, lock=True)
    if job.status in TERMINAL_JOB_STATUSES:
        return job
    _pin_release(job, projection_repository)
    now = datetime.now(timezone.utc)
    if job.started_at is None:
        job.started_at = now
    job.status = "RUNNING"
    bounded = max(1, min(int(chunk_size), MAX_CHUNK_SIZE))
    items = list(session.scalars(sa.select(WorkspaceBulkItem).where(WorkspaceBulkItem.workspace_id == workspace_id, WorkspaceBulkItem.job_id == job_id, WorkspaceBulkItem.status == "PENDING").order_by(WorkspaceBulkItem.row_number).limit(bounded).with_for_update()).all())
    if items:
        inns = [item.normalized_inn for item in items if item.normalized_inn]
        companies = session.scalars(sa.select(Company).where(Company.inn.in_(inns), Company.entity_type == "legal")).all()
        company_by_inn = {company.inn: company for company in companies}
        matched_inns = [inn for inn in inns if inn in company_by_inn]
        try:
            projections = projection_repository.get_companies(matched_inns, release_id=job.public_release_id)
            projection_by_inn = {projection.company.inn: projection for projection in projections}
            projection_error = None
        except Exception:
            projection_by_inn = {}
            projection_error = "public_batch_read_failed"
        for item in items:
            inn = item.normalized_inn or ""
            company = company_by_inn.get(inn)
            item.processed_at = now
            if company is None:
                item.status = "NOT_RESOLVED"
                item.error_code = "company_not_resolved"
                item.error_message = "Компания не найдена в текущем наборе данных next.company."
                continue
            item.company_id = company.id
            if projection_error:
                item.status = "PROCESSING_ERROR"
                item.error_code = projection_error
                item.error_message = "Не удалось прочитать зафиксированный публичный выпуск."
                continue
            projection = projection_by_inn.get(inn)
            if projection is None:
                item.status = "NOT_READY"
                item.error_code = "public_projection_not_ready"
                item.error_message = "Карточка компании недоступна в зафиксированном публичном выпуске."
                continue
            try:
                payload = _result_payload(projection, release_id=job.public_release_id, checked_at=now)
                item.result_payload = payload
                item.result_sha256 = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
                item.status = "READY"
                item.error_code = None
                item.error_message = None
                if job.public_result_date is None:
                    job.public_result_date = date.fromisoformat(payload["result_date"])
            except Exception:
                item.status = "PROCESSING_ERROR"
                item.error_code = "result_snapshot_failed"
                item.error_message = "Не удалось зафиксировать безопасный результат строки."
    session.flush()
    counts = _refresh_counters(session, job)
    resolved_status = _resolve_bulk_job_status(job, counts, processing=True, now=now)
    if resolved_status in TERMINAL_JOB_STATUSES:
        _audit(session, workspace_id=workspace_id, user_id=user_id, action="bulk.process", job_id=job.id, outcome=job.status.lower())
    session.flush()
    return job


def cancel_bulk_job(session: Session, *, user_id: UUID, workspace_id: UUID, job_id: UUID) -> WorkspaceBulkJob:
    authorize(session, user_id=user_id, workspace_id=workspace_id, permission_key="bulk.manage")
    job = _load_job(session, workspace_id=workspace_id, job_id=job_id, lock=True)
    if job.status == "CANCELLED":
        return job
    if job.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"}:
        return job
    now = datetime.now(timezone.utc)
    job.cancel_requested_at = now
    session.execute(sa.update(WorkspaceBulkItem).where(WorkspaceBulkItem.workspace_id == workspace_id, WorkspaceBulkItem.job_id == job_id, WorkspaceBulkItem.status == "PENDING").values(status="CANCELLED", processed_at=now, error_code="cancelled_by_user", error_message="Обработка строки отменена пользователем."))
    job.status = "CANCELLED"
    session.flush()
    counts = _refresh_counters(session, job)
    _resolve_bulk_job_status(job, counts, now=now)
    _audit(session, workspace_id=workspace_id, user_id=user_id, action="bulk.cancel", job_id=job.id, outcome="success")
    session.flush()
    return job


def resume_bulk_job(session: Session, *, user_id: UUID, workspace_id: UUID, job_id: UUID) -> WorkspaceBulkJob:
    authorize(session, user_id=user_id, workspace_id=workspace_id, permission_key="bulk.manage")
    job = _load_job(session, workspace_id=workspace_id, job_id=job_id, lock=True)
    if job.status in {"READY", "RUNNING"}:
        return job
    if job.status != "CANCELLED":
        raise ActionDenied("bulk_job_not_resumable", "Возобновить можно только отменённое задание.", status_code=409)
    session.execute(sa.update(WorkspaceBulkItem).where(WorkspaceBulkItem.workspace_id == workspace_id, WorkspaceBulkItem.job_id == job_id, WorkspaceBulkItem.status == "CANCELLED").values(status="PENDING", processed_at=None, error_code=None, error_message=None))
    job.cancel_requested_at = None
    session.flush()
    counts = _refresh_counters(session, job)
    _resolve_bulk_job_status(job, counts)
    _audit(session, workspace_id=workspace_id, user_id=user_id, action="bulk.resume", job_id=job.id, outcome="success")
    session.flush()
    return job


def retry_bulk_job(session: Session, *, user_id: UUID, workspace_id: UUID, job_id: UUID) -> WorkspaceBulkJob:
    authorize(session, user_id=user_id, workspace_id=workspace_id, permission_key="bulk.manage")
    job = _load_job(session, workspace_id=workspace_id, job_id=job_id, lock=True)
    retried = int(session.scalar(sa.select(sa.func.count()).select_from(WorkspaceBulkItem).where(WorkspaceBulkItem.workspace_id == workspace_id, WorkspaceBulkItem.job_id == job_id, WorkspaceBulkItem.status == "PROCESSING_ERROR")) or 0)
    if not retried:
        return job
    session.execute(sa.update(WorkspaceBulkItem).where(WorkspaceBulkItem.workspace_id == workspace_id, WorkspaceBulkItem.job_id == job_id, WorkspaceBulkItem.status == "PROCESSING_ERROR").values(status="PENDING", company_id=None, result_payload=sa.null(), result_sha256=None, processed_at=None, error_code=None, error_message=None))
    job.error_code = None
    job.error_message = None
    session.flush()
    counts = _refresh_counters(session, job)
    _resolve_bulk_job_status(job, counts)
    _audit(session, workspace_id=workspace_id, user_id=user_id, action="bulk.retry", job_id=job.id, outcome="success")
    session.flush()
    return job


def job_payload(job: WorkspaceBulkJob) -> dict[str, Any]:
    terminal = job.status in TERMINAL_JOB_STATUSES
    done = job.processed_count + job.cancelled_count
    return {
        "job_id": str(job.id), "schema_version": job.schema_version,
        "status": job.status, "filename": job.original_filename,
        "input_sha256": job.input_sha256,
        "public_release_id": job.public_release_id,
        "public_schema_version": job.public_schema_version,
        "public_result_date": job.public_result_date.isoformat() if job.public_result_date else None,
        "counts": {
            "total": job.total_rows, "processable": job.unique_valid_count,
            "processed": job.processed_count, "invalid": job.invalid_count,
            "duplicates": job.duplicate_count, "ready": job.ready_count,
            "not_resolved": job.not_resolved_count, "not_ready": job.not_ready_count,
            "failed": job.failed_count, "cancelled": job.cancelled_count,
        },
        "progress": {
            "completed": done,
            "total": job.unique_valid_count,
            "percent": 100 if not job.unique_valid_count else min(100, int(done * 100 / job.unique_valid_count)),
            "more": not terminal and done < job.unique_valid_count,
        },
        "actions": {
            "process": job.status in {"READY", "RUNNING"},
            "cancel": job.status in {"READY", "RUNNING"},
            "resume": job.status == "CANCELLED" and job.cancelled_count > 0,
            "retry": job.failed_count > 0,
        },
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error": {"code": job.error_code, "message": job.error_message} if job.error_code else None,
    }


def item_payload(item: WorkspaceBulkItem) -> dict[str, Any]:
    return {
        "row_number": item.row_number, "input_inn": item.raw_inn,
        "inn": item.normalized_inn, "status": item.status,
        "duplicate_of_row": item.duplicate_of_row,
        "result": item.result_payload,
        "error": {"code": item.error_code, "message": item.error_message} if item.error_code else None,
        "company_url": f"/app/companies/{item.normalized_inn}" if item.status == "READY" else None,
        "processed_at": item.processed_at.isoformat() if item.processed_at else None,
    }


def _all_export_items(session: Session, *, job: WorkspaceBulkJob) -> tuple[WorkspaceBulkItem, ...]:
    items = tuple(session.scalars(sa.select(WorkspaceBulkItem).where(WorkspaceBulkItem.workspace_id == job.workspace_id, WorkspaceBulkItem.job_id == job.id).order_by(WorkspaceBulkItem.row_number).limit(MAX_TOTAL_ROWS)).all())
    _validate_bulk_items_result_integrity(items, job)
    return items


def bulk_json_bytes(session: Session, *, user_id: UUID, workspace_id: UUID, job_id: UUID) -> bytes:
    job = get_bulk_job(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id, permission_key="bulk.export")
    payload = {"job": job_payload(job), "items": [item_payload(item) for item in _all_export_items(session, job=job)]}
    return canonical_json_bytes(payload)


def bulk_csv_bytes(session: Session, *, user_id: UUID, workspace_id: UUID, job_id: UUID) -> bytes:
    job = get_bulk_job(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id, permission_key="bulk.export")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=BULK_CSV_COLUMNS, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    try:
        for item in _all_export_items(session, job=job):
            result = item.result_payload or {}
            company = result.get("company") or {}
            risk = result.get("risk") or {}
            summary = result.get("summary") or {}
            row = {
                "row_number": item.row_number, "input_inn": item.raw_inn,
                "inn": item.normalized_inn, "status": item.status,
                "error_code": item.error_code, "duplicate_of_row": item.duplicate_of_row,
                "company_name": company.get("name"), "legal_status": company.get("legal_status"),
                "risk_state": risk.get("state"), "risk_status": risk.get("public_status"),
                "summary": summary.get("short_conclusion"), "assessment_date": risk.get("assessment_date"),
                "section_states": result.get("section_states"),
                "public_release_id": result.get("release_id") or job.public_release_id,
                "checked_at": result.get("checked_at"),
                "company_url": f"/app/companies/{item.normalized_inn}" if item.status == "READY" else None,
            }
            writer.writerow({key: _csv_safe_text(row.get(key)) for key in BULK_CSV_COLUMNS})
    except ReportExportValidationError as exc:
        raise BulkExportValidationError() from exc
    return output.getvalue().encode("utf-8-sig")


def bulk_filename(job: WorkspaceBulkJob, extension: str) -> str:
    created = job.created_at.astimezone(timezone.utc).date().isoformat() if job.created_at else datetime.now(timezone.utc).date().isoformat()
    return f"next-company-bulk-{created}-{job.id}.{extension}"


def record_bulk_export(
    session: Session, *, user_id: UUID, workspace_id: UUID, job_id: UUID, format: str
) -> None:
    authorize(session, user_id=user_id, workspace_id=workspace_id, permission_key="bulk.export")
    _load_job(session, workspace_id=workspace_id, job_id=job_id)
    _audit(
        session, workspace_id=workspace_id, user_id=user_id,
        action="bulk.export", job_id=job_id, outcome=f"success_{format}",
    )
    session.flush()
