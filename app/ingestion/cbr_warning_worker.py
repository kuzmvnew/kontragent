"""Worker Foundation adapter for the Bank of Russia Warning List.

The official channel is a complete JSON snapshot intended for automated
systems.  It exposes no ETag, Last-Modified header, or separate release
passport, so a daily source check downloads the snapshot once and uses a
deterministic normalized SHA-256 as the release identity.  The existing
parser, table, exact-INN semantics, product projection, and atomic
full-snapshot publication remain in place; this module only supplies Worker
Foundation orchestration.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.ingestion.cbr_warning_list import (
    cbr_warning_source_data_date,
    parse_cbr_warning_payload,
    validate_cbr_warning_snapshot,
)
from app.models.cbr_warning_list import CbrWarningListEntry
from app.models.company import Company
from app.models.source import DataSet
from app.models.worker import WorkerHandlerRegistration, WorkerPublicationState
from app.providers.cbr_warning_list_provider import (
    API_DOC_URL,
    FULL_LIST_JSON_URL,
    CbrWarningListProvider,
    CbrWarningListProviderError,
)
from app.worker.contracts import (
    ExecutionCounters,
    HandlerContext,
    HandlerResult,
    RawArtifactReference,
    StagingResult,
    ValidationResult,
)
from app.worker.errors import (
    HandlerNotRegisteredError,
    InvalidDataError,
    SchemaMismatchError,
    WorkerNetworkError,
)
from app.worker.execution import JobCreation, create_job, register_handler
from app.worker.registry import HandlerRegistry


SOURCE_ID = "cbr_warning_list"
DATASET_CODE = SOURCE_ID
HANDLER_VERSION = "cbr-warning-official-v1"
CHECK_INTERVAL = timedelta(days=1)
DOWNLOAD_NAME = "black-list-json"
NORMALIZED_BATCH_SIZE = 1000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must contain a timezone")
    return value.astimezone(timezone.utc)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _write_once(path: Path, content: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        if path.read_bytes() != content:
            raise InvalidDataError(f"immutable CBR RAW member differs: {path}")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _persist_temp(temp_path: Path, target: Path, *, checksum: str) -> None:
    if target.exists():
        actual, _size = _hash_file(target)
        temp_path.unlink(missing_ok=True)
        if actual != checksum:
            raise InvalidDataError(f"immutable CBR normalized snapshot differs: {target}")
        return
    try:
        os.link(temp_path, target)
        target.chmod(0o444)
    except FileExistsError:
        actual, _size = _hash_file(target)
        if actual != checksum:
            raise InvalidDataError(f"immutable CBR normalized snapshot differs: {target}")
    finally:
        temp_path.unlink(missing_ok=True)


def _file_path(reference: str) -> Path:
    parsed = urlparse(reference)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise InvalidDataError("accepted CBR snapshot must use a local file URI")
    path = Path(unquote(parsed.path))
    if not path.is_absolute() or not path.is_file():
        raise InvalidDataError("accepted CBR snapshot is unavailable")
    return path


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _normalize_to_jsonl(
    records: list[dict[str, Any]],
    *,
    artifact_dir: Path,
) -> tuple[Path, str]:
    descriptor, name = tempfile.mkstemp(
        prefix="normalized-", suffix=".jsonl.tmp", dir=artifact_dir
    )
    os.close(descriptor)
    temp_path = Path(name)
    try:
        with temp_path.open("w", encoding="utf-8") as output:
            # The official endpoint does not guarantee array order.  Sort by
            # stable CBR id so harmless response reordering cannot create a
            # false release or publication generation.
            for record in sorted(records, key=lambda item: str(item["cbr_id"])):
                output.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=_json_default,
                    )
                    + "\n"
                )
        checksum, _size = _hash_file(temp_path)
        target = artifact_dir / f"normalized-{checksum}.jsonl"
        _persist_temp(temp_path, target, checksum=checksum)
        return target, checksum
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _provider_error(error: CbrWarningListProviderError) -> Exception:
    if error.kind in {"timeout", "network_error", "service_unavailable"}:
        return WorkerNetworkError(f"CBR warning list request failed: {error.kind}")
    return SchemaMismatchError(f"CBR warning list response rejected: {error.kind}")


def run_cbr_warning_handler(
    context: HandlerContext,
    *,
    provider: CbrWarningListProvider | None = None,
) -> HandlerResult:
    """Fetch, validate, and stage one complete official JSON snapshot."""

    metadata = context.schedule_metadata
    if metadata.get("source_url") != FULL_LIST_JSON_URL:
        raise InvalidDataError("CBR source URL differs from handler pin")
    raw_value = str(metadata.get("raw_root") or "").strip()
    if not raw_value:
        raise InvalidDataError("raw_root is required")
    checked_at = _utc(datetime.fromisoformat(str(metadata["discovered_at"])))
    context.ensure_active(now=utc_now())
    try:
        response = (provider or CbrWarningListProvider()).fetch_full_list()
    except CbrWarningListProviderError as error:
        raise _provider_error(error) from error

    raw_content = bytes(response["raw_content"])
    headers = dict(response.get("headers") or {})
    declared_length = headers.get("content-length")
    if declared_length is not None and int(declared_length) != len(raw_content):
        raise SchemaMismatchError("CBR content length differs from HTTP metadata")
    artifact_checksum = sha256(raw_content).hexdigest()
    try:
        source_data_date = cbr_warning_source_data_date(response["payload"])
        parsed = parse_cbr_warning_payload(
            response["payload"], data_date=source_data_date
        )
        validate_cbr_warning_snapshot(parsed)
    except (TypeError, ValueError) as error:
        raise SchemaMismatchError(str(error)) from error
    if source_data_date > checked_at.date():
        raise SchemaMismatchError("CBR source data date is in the future")

    raw_root = Path(raw_value).resolve()
    artifact_dir = raw_root / SOURCE_ID / artifact_checksum
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / DOWNLOAD_NAME
    _write_once(artifact_path, raw_content)
    normalized_path, normalized_checksum = _normalize_to_jsonl(
        parsed["records"], artifact_dir=artifact_dir
    )
    stable_manifest = {
        "manifest_version": 1,
        "source_id": SOURCE_ID,
        "dataset_code": DATASET_CODE,
        "source_url": FULL_LIST_JSON_URL,
        "api_documentation_url": API_DOC_URL,
        "artifact_reference": artifact_path.as_uri(),
        "artifact_sha256": artifact_checksum,
        "artifact_size": len(raw_content),
        "normalized_reference": normalized_path.as_uri(),
        "normalized_sha256": normalized_checksum,
        "source_data_date": source_data_date.isoformat(),
        "immutable": True,
    }
    _write_once(
        artifact_dir / "manifest.json",
        (json.dumps(stable_manifest, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )
    context.heartbeat()
    counters = ExecutionCounters(
        records_seen=int(parsed["source_records"]),
        records_written=int(parsed["imported_records"]),
        records_rejected=int(parsed["rejected_records"]),
        records_duplicated=int(parsed["duplicate_records"]),
    )
    context.report_counters(counters)
    # The official page defines the snapshot as current on its download date.
    # A successful next-day check extends this boundary without conflating it
    # with the newest row's source data date.
    official_actual_until = checked_at.date()
    observation_manifest = {
        **stable_manifest,
        "retrieved_at": checked_at.isoformat(),
        "official_actual_until": official_actual_until.isoformat(),
        "http_status": int(response["http_status"]),
        "http_headers": headers,
    }
    return HandlerResult(
        raw_artifacts=(
            RawArtifactReference(
                artifact_reference=artifact_path.as_uri(),
                checksum=artifact_checksum,
                manifest=observation_manifest,
            ),
        ),
        staging_result=StagingResult(
            staging_pointer=normalized_path.as_uri(),
            checksum=normalized_checksum,
            validation=ValidationResult(
                accepted=True,
                metadata={
                    "release_identity": normalized_checksum,
                    "source_data_date": source_data_date.isoformat(),
                    "official_actual_until": official_actual_until.isoformat(),
                    "source_records": int(parsed["source_records"]),
                    "imported_records": int(parsed["imported_records"]),
                    "with_inn": int(parsed["with_inn"]),
                    "without_inn": int(parsed["without_inn"]),
                },
            ),
            metadata={
                "source_id": SOURCE_ID,
                "dataset_code": DATASET_CODE,
                "source_url": FULL_LIST_JSON_URL,
                "api_projection": "cbr_warning_list_check",
                "card_projection": "company_card.cbr_warning_list",
            },
        ),
        checksum_metadata={
            "artifact_sha256": artifact_checksum,
            "normalized_sha256": normalized_checksum,
            "release_identity": normalized_checksum,
        },
        counters=counters,
    )


def cbr_warning_worker_handler(context: HandlerContext) -> HandlerResult:
    return run_cbr_warning_handler(context)


def _iter_jsonl(path: Path) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                batch.append(json.loads(line))
            if len(batch) >= NORMALIZED_BATCH_SIZE:
                yield batch
                batch = []
    if batch:
        yield batch


def _apply_successful_check(
    dataset: DataSet,
    *,
    actual_until: date,
    now: datetime,
) -> None:
    dataset.checked_at = now
    dataset.official_actual_until = actual_until
    dataset.operational_status = (
        OperationalStatus.CURRENT
        if now.date() <= actual_until
        else OperationalStatus.STALE
    )
    dataset.next_expected_update_at = now + CHECK_INTERVAL
    dataset.last_error = None
    dataset.last_error_at = None
    dataset.retry_count = 0
    dataset.next_retry_at = None


def _validation_metadata(result: HandlerResult) -> dict[str, Any]:
    if result.staging_result is None:
        raise InvalidDataError("CBR publisher requires normalized staging")
    return dict(result.staging_result.validation.metadata)


def publish_cbr_warning_worker_result(
    session: Session,
    claim: Any,
    result: HandlerResult,
) -> HandlerResult:
    """Atomically replace the complete CBR snapshot or record a no-op check."""

    dataset = session.scalar(
        select(DataSet)
        .where(DataSet.code == DATASET_CODE)
        .with_for_update()
    )
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {DATASET_CODE}")
    validation = _validation_metadata(result)
    release_identity = str(validation["release_identity"])
    source_data_date = date.fromisoformat(str(validation["source_data_date"]))
    actual_until = date.fromisoformat(str(validation["official_actual_until"]))
    source_records = int(validation["source_records"])
    snapshot_records = int(validation["imported_records"])
    now = utc_now()
    state = session.scalar(
        select(WorkerPublicationState)
        .where(WorkerPublicationState.source_id == SOURCE_ID)
        .with_for_update()
    )
    previous_identity = (
        ((state.validation_metadata or {}).get("validation") or {}).get(
            "release_identity"
        )
        if state is not None
        else None
    )
    existing_count = int(
        session.scalar(
            select(func.count())
            .select_from(CbrWarningListEntry)
            .where(CbrWarningListEntry.dataset_id == dataset.id)
        )
        or 0
    )
    if previous_identity == release_identity:
        if existing_count != int(dataset.record_count or 0) or existing_count == 0:
            raise InvalidDataError("accepted CBR snapshot rows are incomplete")
        _apply_successful_check(dataset, actual_until=actual_until, now=now)
        coverage = dict(dataset.coverage or {})
        coverage["last_check"] = {
            "checked_at": now.isoformat(),
            "release_identity": release_identity,
            "changed": False,
        }
        dataset.coverage = coverage
        return replace(
            result,
            staging_result=None,
            checksum_metadata={
                **result.checksum_metadata,
                "check_only": True,
                "freshness": dataset.operational_status.value,
                "official_actual_until": actual_until.isoformat(),
            },
            counters=ExecutionCounters(records_seen=source_records),
        )

    staging_path = _file_path(result.staging_result.staging_pointer)
    session.execute(
        delete(CbrWarningListEntry).where(
            CbrWarningListEntry.dataset_id == dataset.id
        )
    )
    published = 0
    for batch in _iter_jsonl(staging_path):
        values = []
        for row in batch:
            values.append(
                {
                    "dataset_id": dataset.id,
                    "data_date": date.fromisoformat(row["data_date"]),
                    "cbr_id": str(row["cbr_id"]),
                    "inn": row.get("inn"),
                    "name": row.get("name"),
                    "entry_date": date.fromisoformat(row["entry_date"])
                    if row.get("entry_date")
                    else None,
                    "update_date": date.fromisoformat(row["update_date"])
                    if row.get("update_date")
                    else None,
                    "address": row.get("address"),
                    "sites": row.get("sites") or [],
                    "signs": row.get("signs") or [],
                    "regions": row.get("regions") or [],
                    "additional_info": row.get("additional_info"),
                    "liquidation_status": row.get("liquidation_status"),
                    "comment": row.get("comment"),
                    "org_type": row.get("org_type"),
                    "raw_payload": row.get("raw_payload") or {},
                }
            )
        if values:
            session.execute(pg_insert(CbrWarningListEntry).values(values))
            published += len(values)
    if published != snapshot_records:
        raise InvalidDataError("CBR normalized snapshot count differs from source")

    matched = int(
        session.scalar(
            select(func.count())
            .select_from(CbrWarningListEntry)
            .join(Company, Company.inn == CbrWarningListEntry.inn)
            .where(CbrWarningListEntry.dataset_id == dataset.id)
        )
        or 0
    )
    matched_companies = int(
        session.scalar(
            select(func.count(func.distinct(Company.id)))
            .select_from(CbrWarningListEntry)
            .join(Company, Company.inn == CbrWarningListEntry.inn)
            .where(CbrWarningListEntry.dataset_id == dataset.id)
        )
        or 0
    )
    dataset.enabled = True
    dataset.auto_update_status = AutoUpdateStatus.CONFIGURED
    dataset.last_success_at = now
    dataset.last_data_date = source_data_date
    dataset.source_as_of = datetime.combine(
        source_data_date, datetime.min.time(), tzinfo=timezone.utc
    )
    dataset.retrieved_at = now
    dataset.published_at = now
    dataset.record_count = published
    dataset.coverage = {
        "source_records": source_records,
        "source_records_with_inn": int(validation["with_inn"]),
        "source_records_without_inn": int(validation["without_inn"]),
        "matched": matched,
        "unmatched": snapshot_records - matched,
        "published_facts": published,
        "risk_summary_candidate_companies": matched_companies,
        "matching_method": "inn_exact",
        "api_projection": "cbr_warning_list_check",
        "card_projection": "company_card.cbr_warning_list",
        "release_identity": release_identity,
    }
    _apply_successful_check(dataset, actual_until=actual_until, now=now)
    updated_validation = replace(
        result.staging_result.validation,
        metadata={
            **validation,
            "matched": matched,
            "unmatched": snapshot_records - matched,
            "published_facts": published,
            "risk_summary_candidate_companies": matched_companies,
            "matching_method": "inn_exact",
            "freshness": dataset.operational_status.value,
        },
    )
    return replace(
        result,
        staging_result=replace(
            result.staging_result, validation=updated_validation
        ),
        counters=ExecutionCounters(
            records_seen=source_records,
            records_written=published,
            records_rejected=result.counters.records_rejected
            if result.counters
            else 0,
            records_duplicated=result.counters.records_duplicated
            if result.counters
            else 0,
            records_published=published,
        ),
    )


def register_cbr_warning_worker(
    session: Session,
    registry: HandlerRegistry,
) -> Any:
    return register_handler(
        session,
        registry,
        source_id=SOURCE_ID,
        version=HANDLER_VERSION,
        handler=cbr_warning_worker_handler,
        publisher=publish_cbr_warning_worker_result,
        approved=True,
        live=False,
        fixture=False,
        metadata={
            "mode": "official_bulk_snapshot",
            "source_url": FULL_LIST_JSON_URL,
            "api_documentation_url": API_DOC_URL,
            "matching_method": "inn_exact",
        },
    )


def schedule_cbr_warning_check(
    session: Session,
    *,
    raw_root: Path,
    now: datetime | None = None,
) -> JobCreation:
    now = _utc(now or utc_now())
    approval = session.get(
        WorkerHandlerRegistration, (SOURCE_ID, HANDLER_VERSION)
    )
    if (
        approval is None
        or not approval.approved
        or not approval.enabled
        or approval.live_mode
    ):
        raise HandlerNotRegisteredError(
            f"durable handler approval is missing: {SOURCE_ID}@{HANDLER_VERSION}"
        )
    check_date = now.date().isoformat()
    return create_job(
        session,
        source_id=SOURCE_ID,
        job_type="cbr_warning_list_check",
        handler_version=HANDLER_VERSION,
        idempotency_key=(
            f"{SOURCE_ID}:check:{check_date}:{HANDLER_VERSION}"
        ),
        schedule_metadata={
            "source_url": FULL_LIST_JSON_URL,
            "api_documentation_url": API_DOC_URL,
            "raw_root": str(Path(raw_root).resolve()),
            "discovered_at": now.isoformat(),
            "check_frequency": "daily",
            "publication_frequency": "continuous_official_snapshot",
        },
        max_attempts=3,
        timeout_seconds=900,
        now=now,
    )
