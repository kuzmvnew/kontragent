"""Worker Foundation adapter for on-demand CBR financial-organisation checks."""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.models.cbr_finorg import CbrFinorgCheck
from app.models.company import Company
from app.models.source import DataSet
from app.models.worker import WorkerHandlerRegistration
from app.providers.cbr_finorg_provider import (
    SERVICE_URL,
    CbrFinorgProvider,
    CbrFinorgProviderError,
    normalize_inn,
)
from app.worker.contracts import (
    ExecutionCounters,
    HandlerContext,
    HandlerResult,
    RawArtifactReference,
    SourceChangeSummary,
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


SOURCE_ID = "cbr_finorg"
DATASET_CODE = SOURCE_ID
HANDLER_VERSION = "cbr-finorg-on-demand-official-v1"
RAW_FILE_NAME = "soap-exchange.json"
NORMALIZED_FILE_NAME = "normalized-result.json"
CHECK_INTERVAL = timedelta(days=1)
MAX_SCHEDULED_MASTER_COMPANIES = 100


@dataclass(frozen=True)
class CbrFinorgSweepSchedule:
    scheduled_at: datetime
    request_date: date
    cohort_inns: tuple[str, ...]
    job_ids: tuple[str, ...]
    created_jobs: int
    existing_jobs: int
    next_expected_update_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": SOURCE_ID,
            "scheduled_at": self.scheduled_at.isoformat(),
            "request_date": self.request_date.isoformat(),
            "cohort_size": len(self.cohort_inns),
            "cohort_inns": list(self.cohort_inns),
            "job_ids": list(self.job_ids),
            "created_jobs": self.created_jobs,
            "existing_jobs": self.existing_jobs,
            "next_expected_update_at": self.next_expected_update_at.isoformat(),
            "bounded_master_limit": MAX_SCHEDULED_MASTER_COMPANIES,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must contain a timezone")
    return value.astimezone(timezone.utc)


def _hash_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_once(path: Path, content: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        if path.read_bytes() != content:
            raise InvalidDataError(f"immutable CBR FINORG RAW member differs: {path}")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _file_path(reference: str) -> Path:
    parsed = urlparse(reference)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise InvalidDataError("accepted CBR FINORG result must use a local file URI")
    path = Path(unquote(parsed.path))
    if not path.is_absolute() or not path.is_file():
        raise InvalidDataError("accepted CBR FINORG result is unavailable")
    return path


def _provider_error(error: CbrFinorgProviderError) -> Exception:
    if error.kind in {"timeout", "network_error", "service_unavailable"}:
        return WorkerNetworkError(f"CBR FINORG request failed: {error.kind}")
    if error.http_status is not None and (
        error.http_status == 429 or error.http_status >= 500
    ):
        return WorkerNetworkError(
            f"CBR FINORG request failed: HTTP {error.http_status}"
        )
    return SchemaMismatchError(f"CBR FINORG response rejected: {error.kind}")


def _validate_job_metadata(metadata: Mapping[str, Any]) -> tuple[str, date, datetime, Path]:
    if metadata.get("source_url") != SERVICE_URL:
        raise InvalidDataError("CBR FINORG source URL differs from handler pin")
    inn = normalize_inn(metadata.get("inn"))
    if len(inn) not in {10, 12}:
        raise InvalidDataError("CBR FINORG job requires an exact 10/12 digit INN")
    try:
        request_date = date.fromisoformat(str(metadata["request_date"]))
        requested_at = _utc(datetime.fromisoformat(str(metadata["requested_at"])))
    except (KeyError, ValueError) as error:
        raise InvalidDataError("CBR FINORG job dates are invalid") from error
    raw_value = str(metadata.get("raw_root") or "").strip()
    if not raw_value:
        raise InvalidDataError("raw_root is required")
    return inn, request_date, requested_at, Path(raw_value).resolve()


def _raw_exchange_payload(
    *,
    inn: str,
    request_date: date,
    observations: tuple[dict, ...],
) -> bytes:
    exchanges = []
    for item in observations:
        request_content = bytes(item["request_content"])
        response_content = bytes(item["response_content"])
        exchanges.append(
            {
                "action": str(item["action"]),
                "request_base64": base64.b64encode(request_content).decode("ascii"),
                "request_sha256": _hash_bytes(request_content),
                "response_base64": base64.b64encode(response_content).decode("ascii"),
                "response_sha256": _hash_bytes(response_content),
                "http_status": int(item["http_status"]),
                "response_headers": {
                    str(key).lower(): str(value)
                    for key, value in dict(item.get("response_headers") or {}).items()
                    if str(key).lower() in {
                        "content-type",
                        "content-length",
                        "date",
                        "etag",
                        "last-modified",
                    }
                },
            }
        )
    return (
        json.dumps(
            {
                "manifest_version": 1,
                "source_id": SOURCE_ID,
                "source_url": SERVICE_URL,
                "inn": inn,
                "request_date": request_date.isoformat(),
                "exchanges": exchanges,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def run_cbr_finorg_handler(
    context: HandlerContext,
    *,
    provider: CbrFinorgProvider | None = None,
) -> HandlerResult:
    inn, request_date, requested_at, raw_root = _validate_job_metadata(
        context.schedule_metadata
    )
    context.ensure_active(now=utc_now())
    try:
        response = (provider or CbrFinorgProvider()).check_inn_with_raw(inn)
    except CbrFinorgProviderError as error:
        raise _provider_error(error) from error
    observations = tuple(response.pop("observations", ()))
    if not observations:
        raise SchemaMismatchError("CBR FINORG provider returned no SOAP evidence")
    participant = response.get("participant") or None
    search_record = response.get("search_record") or None
    if response.get("found"):
        response_inn = normalize_inn((participant or {}).get("inn"))
        if response_inn != inn:
            raise SchemaMismatchError("CBR FINORG participant INN differs")
        response_ogrn = normalize_inn((participant or {}).get("ogrn"))
        search_ogrn = normalize_inn((search_record or {}).get("ogrn"))
        if response_ogrn and len(response_ogrn) not in {13, 15}:
            raise SchemaMismatchError("CBR FINORG participant OGRN is invalid")
        if search_ogrn and len(search_ogrn) not in {13, 15}:
            raise SchemaMismatchError("CBR FINORG search OGRN is invalid")
        if response_ogrn and search_ogrn and response_ogrn != search_ogrn:
            raise SchemaMismatchError("CBR FINORG search/full OGRN differs")
        if search_ogrn and not response_ogrn:
            participant = {**dict(participant or {}), "ogrn": search_ogrn}

    raw_payload = _raw_exchange_payload(
        inn=inn, request_date=request_date, observations=observations
    )
    artifact_checksum = _hash_bytes(raw_payload)
    artifact_dir = raw_root / SOURCE_ID / artifact_checksum
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / RAW_FILE_NAME
    _write_once(artifact_path, raw_payload)
    normalized = {
        "inn": inn,
        "request_date": request_date.isoformat(),
        "found": bool(response.get("found")),
        "participant": participant,
        "search_record": search_record,
        "http_status": int(response.get("http_status") or 200),
        "raw_reference": artifact_path.as_uri(),
        "raw_sha256": artifact_checksum,
    }
    normalized_payload = (
        json.dumps(normalized, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    normalized_checksum = _hash_bytes(normalized_payload)
    normalized_path = artifact_dir / NORMALIZED_FILE_NAME
    _write_once(normalized_path, normalized_payload)
    stable_manifest = {
        "manifest_version": 1,
        "source_id": SOURCE_ID,
        "dataset_code": DATASET_CODE,
        "source_url": SERVICE_URL,
        "inn": inn,
        "request_date": request_date.isoformat(),
        "artifact_reference": artifact_path.as_uri(),
        "artifact_sha256": artifact_checksum,
        "artifact_size": len(raw_payload),
        "normalized_reference": normalized_path.as_uri(),
        "normalized_sha256": normalized_checksum,
        "immutable": True,
    }
    _write_once(
        artifact_dir / "manifest.json",
        (json.dumps(stable_manifest, ensure_ascii=False, sort_keys=True) + "\n").encode(),
    )
    context.heartbeat()
    counters = ExecutionCounters(records_seen=1, records_written=1)
    context.report_counters(counters)
    return HandlerResult(
        raw_artifacts=(
            RawArtifactReference(
                artifact_reference=artifact_path.as_uri(),
                checksum=artifact_checksum,
                manifest={
                    **stable_manifest,
                    "requested_at": requested_at.isoformat(),
                    "soap_actions": [item["action"] for item in observations],
                },
            ),
        ),
        staging_result=StagingResult(
            staging_pointer=normalized_path.as_uri(),
            checksum=normalized_checksum,
            validation=ValidationResult(
                accepted=True,
                metadata={
                    "inn": inn,
                    "request_date": request_date.isoformat(),
                    "found": bool(response.get("found")),
                    "matching_method": "inn_exact_with_ogrn_consistency",
                },
            ),
            metadata={
                "source_id": SOURCE_ID,
                "dataset_code": DATASET_CODE,
                "api_projection": "cbr_finorg_check",
                "card_projection": "company_card.cbr_finorg",
                "publication_mode": "on_demand_cache_upsert",
            },
        ),
        checksum_metadata={
            "artifact_sha256": artifact_checksum,
            "normalized_sha256": normalized_checksum,
            "inn": inn,
            "request_date": request_date.isoformat(),
        },
        counters=counters,
    )


def cbr_finorg_worker_handler(context: HandlerContext) -> HandlerResult:
    return run_cbr_finorg_handler(context)


def _date_value(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as error:
        raise SchemaMismatchError(f"CBR FINORG date is invalid: {value}") from error


def _row_values(
    *,
    dataset_id: int,
    normalized: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    participant = dict(normalized.get("participant") or {})
    return {
        "dataset_id": dataset_id,
        "inn": str(normalized["inn"]),
        "request_date": date.fromisoformat(str(normalized["request_date"])),
        "result_status": "success",
        "is_participant": bool(normalized.get("found")),
        "cbr_id": participant.get("cbr_id"),
        "ogrn": participant.get("ogrn"),
        "short_name": participant.get("short_name"),
        "name": participant.get("name"),
        "status": participant.get("status"),
        "fo_types": participant.get("fo_types") or [],
        "licenses": participant.get("licenses") or [],
        "payment_systems": participant.get("payment_systems") or [],
        "mfo_history": participant.get("mfo_history") or [],
        "websites": participant.get("websites") or [],
        "address": participant.get("address"),
        "phones": participant.get("phones"),
        "email": participant.get("email"),
        "region": participant.get("region"),
        "regnum": participant.get("regnum"),
        "bic": participant.get("bic"),
        "is_sro_member": participant.get("is_sro_member"),
        "has_branches": participant.get("has_branches"),
        "registration_date": _date_value(participant.get("registration_date")),
        "http_status": int(normalized.get("http_status") or 200),
        "error_code": None,
        "error_message": None,
        "raw_payload": {
            "found": bool(normalized.get("found")),
            "search_record": normalized.get("search_record"),
            "participant": participant or None,
            "raw_reference": normalized.get("raw_reference"),
            "raw_sha256": normalized.get("raw_sha256"),
        },
        "checked_at": now,
        "updated_at": now,
    }


def _row_semantics(row: CbrFinorgCheck | None, values: Mapping[str, Any]) -> str:
    if row is None:
        return "new"
    ignored = {"checked_at", "updated_at"}
    for key, value in values.items():
        if key in ignored:
            continue
        if getattr(row, key) != value:
            return "changed"
    return "unchanged"


def publish_cbr_finorg_worker_result(
    session: Session, claim: Any, result: HandlerResult
) -> HandlerResult:
    if result.staging_result is None:
        raise InvalidDataError("CBR FINORG publisher requires normalized staging")
    dataset = session.scalar(
        select(DataSet).where(DataSet.code == DATASET_CODE).with_for_update()
    )
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {DATASET_CODE}")
    path = _file_path(result.staging_result.staging_pointer)
    if _hash_file(path) != result.staging_result.checksum:
        raise InvalidDataError("CBR FINORG normalized checksum differs")
    try:
        normalized = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InvalidDataError("CBR FINORG normalized result cannot be read") from error
    inn, request_date, _requested_at, _raw_root = _validate_job_metadata(
        claim.schedule_metadata
    )
    if normalized.get("inn") != inn or normalized.get("request_date") != request_date.isoformat():
        raise InvalidDataError("CBR FINORG normalized request identity differs")
    participant = dict(normalized.get("participant") or {})
    companies = list(session.scalars(select(Company).where(Company.inn == inn)))
    response_ogrn = normalize_inn(participant.get("ogrn"))
    if normalized.get("found") and response_ogrn:
        for company in companies:
            master_ogrn = normalize_inn(company.ogrn)
            if master_ogrn and master_ogrn != response_ogrn:
                raise InvalidDataError(
                    "CBR FINORG response OGRN differs from Master for exact INN"
                )

    now = utc_now()
    previous_date = dataset.last_data_date
    existing = session.scalar(
        select(CbrFinorgCheck)
        .where(
            CbrFinorgCheck.dataset_id == dataset.id,
            CbrFinorgCheck.inn == inn,
            CbrFinorgCheck.request_date == request_date,
        )
        .with_for_update()
    )
    values = _row_values(dataset_id=dataset.id, normalized=normalized, now=now)
    disposition = _row_semantics(existing, values)
    is_fact = bool(normalized.get("found"))
    previous_is_fact = bool(existing and existing.is_participant)
    statement = pg_insert(CbrFinorgCheck).values(**values)
    session.execute(
        statement.on_conflict_do_update(
            constraint="uq_cbr_finorg_dataset_inn_request_date",
            set_={
                key: value
                for key, value in values.items()
                if key not in {"dataset_id", "inn", "request_date"}
            },
        )
    )
    total = int(
        session.scalar(
            select(func.count())
            .select_from(CbrFinorgCheck)
            .where(CbrFinorgCheck.dataset_id == dataset.id)
        )
        or 0
    )
    checked_inns = int(
        session.scalar(
            select(func.count(func.distinct(CbrFinorgCheck.inn))).where(
                CbrFinorgCheck.dataset_id == dataset.id,
                CbrFinorgCheck.result_status == "success",
            )
        )
        or 0
    )
    found_count = int(
        session.scalar(
            select(func.count()).select_from(CbrFinorgCheck).where(
                CbrFinorgCheck.dataset_id == dataset.id,
                CbrFinorgCheck.result_status == "success",
                CbrFinorgCheck.is_participant.is_(True),
            )
        )
        or 0
    )
    not_found_count = int(
        session.scalar(
            select(func.count()).select_from(CbrFinorgCheck).where(
                CbrFinorgCheck.dataset_id == dataset.id,
                CbrFinorgCheck.result_status == "success",
                CbrFinorgCheck.is_participant.is_(False),
            )
        )
        or 0
    )
    summary = SourceChangeSummary(
        matched_companies=(len(companies) if normalized.get("found") else 0),
        new_facts=int(is_fact and not previous_is_fact),
        changed_facts=int(is_fact and previous_is_fact and disposition == "changed"),
        removed_or_expired_facts=int(previous_is_fact and not is_fact),
        unchanged_facts=int(is_fact and previous_is_fact and disposition == "unchanged"),
        replayed_facts=0,
        quarantined_records=0,
        source_records=int(bool(normalized.get("found"))),
        source_data_date=request_date,
        previous_source_data_date=previous_date,
        unavailable_reasons=(
            {"previous_source_data_date": "first successful publication has no previous source date"}
            if previous_date is None
            else {}
        ),
    )
    dataset.enabled = True
    dataset.dataset_kind = "on_demand_api"
    dataset.freshness_policy = "on_demand"
    scheduled = claim.schedule_metadata.get("execution_mode") == "scheduled_master_sweep"
    previously_scheduled = dataset.auto_update_status == AutoUpdateStatus.CONFIGURED.value
    dataset.auto_update_status = (
        AutoUpdateStatus.CONFIGURED
        if scheduled or previously_scheduled
        else AutoUpdateStatus.USER_TRIGGERED
    )
    dataset.last_success_at = now
    dataset.last_data_date = request_date
    dataset.source_as_of = datetime.combine(
        request_date, datetime.min.time(), tzinfo=timezone.utc
    )
    dataset.retrieved_at = now
    dataset.checked_at = now
    dataset.official_actual_until = request_date
    dataset.published_at = now
    dataset.record_count = found_count
    dataset.operational_status = (
        OperationalStatus.CURRENT
        if now.date() <= request_date
        else OperationalStatus.STALE
    )
    dataset.last_error = None
    dataset.last_error_at = None
    dataset.retry_count = 0
    dataset.next_retry_at = None
    dataset.next_expected_update_at = (
        max(dataset.next_expected_update_at or now, now + CHECK_INTERVAL)
        if scheduled or previously_scheduled
        else None
    )
    dataset.coverage = {
        "checked_inns": checked_inns,
        "cached_checks": total,
        "published_facts": found_count,
        "found_checks": found_count,
        "not_found_checks": not_found_count,
        "matching_method": "inn_exact_with_ogrn_consistency",
        "api_projection": "cbr_finorg_check",
        "card_projection": "company_card.cbr_finorg",
        "on_demand": True,
        "scheduled_master_sweep": scheduled or previously_scheduled,
        "check_frequency": "daily" if scheduled or previously_scheduled else "on_demand",
        "change_summary": summary.as_dict(),
    }
    validation = replace(
        result.staging_result.validation,
        metadata={
            **result.staging_result.validation.metadata,
            "cache_disposition": disposition,
            "freshness": dataset.operational_status.value,
            "official_actual_until": request_date.isoformat(),
        },
    )
    return replace(
        result,
        staging_result=replace(result.staging_result, validation=validation),
        counters=ExecutionCounters(
            records_seen=1,
            records_written=int(disposition != "unchanged"),
            records_published=int(is_fact and disposition != "unchanged"),
        ),
        change_summary=summary,
    )


def register_cbr_finorg_worker(session: Session, registry: HandlerRegistry) -> Any:
    return register_handler(
        session,
        registry,
        source_id=SOURCE_ID,
        version=HANDLER_VERSION,
        handler=cbr_finorg_worker_handler,
        publisher=publish_cbr_finorg_worker_result,
        approved=True,
        live=False,
        fixture=False,
        metadata={
            "mode": "official_on_demand_api",
            "source_url": SERVICE_URL,
            "matching_method": "inn_exact_with_ogrn_consistency",
            "mass_schedule": False,
        },
    )


def enqueue_cbr_finorg_check(
    session: Session,
    *,
    inn: str,
    request_date: date,
    raw_root: Path,
    now: datetime | None = None,
    execution_mode: str = "on_demand",
    sweep_id: str | None = None,
    cohort_index: int | None = None,
    cohort_size: int | None = None,
) -> JobCreation:
    now = _utc(now or utc_now())
    clean_inn = normalize_inn(inn)
    if len(clean_inn) not in {10, 12}:
        raise ValueError("CBR FINORG requires an exact 10/12 digit INN")
    if execution_mode not in {"on_demand", "scheduled_master_sweep"}:
        raise ValueError("unsupported CBR FINORG execution mode")
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
    return create_job(
        session,
        source_id=SOURCE_ID,
        job_type="cbr_finorg_inn_check",
        handler_version=HANDLER_VERSION,
        idempotency_key=(
            f"{SOURCE_ID}:inn:{clean_inn}:{request_date.isoformat()}:{HANDLER_VERSION}"
        ),
        schedule_metadata={
            "source_url": SERVICE_URL,
            "inn": clean_inn,
            "request_date": request_date.isoformat(),
            "requested_at": now.isoformat(),
            "raw_root": str(Path(raw_root).resolve()),
            "execution_mode": execution_mode,
            "publication_frequency": "per_inn_per_utc_date",
            "check_frequency": "daily" if execution_mode == "scheduled_master_sweep" else "on_demand",
            **({"sweep_id": sweep_id} if sweep_id else {}),
            **({"cohort_index": cohort_index} if cohort_index is not None else {}),
            **({"cohort_size": cohort_size} if cohort_size is not None else {}),
        },
        max_attempts=3,
        timeout_seconds=180,
        now=now,
    )


def schedule_cbr_finorg_master_sweep(
    session: Session,
    *,
    raw_root: Path,
    now: datetime | None = None,
    cohort_inns: tuple[str, ...] | None = None,
    max_companies: int = MAX_SCHEDULED_MASTER_COMPANIES,
) -> CbrFinorgSweepSchedule:
    """Schedule one bounded daily pass over Master without replacing on-demand.

    The per-INN/date idempotency key is shared with user-triggered checks, so a
    same-day on-demand lookup is reused rather than redownloaded.  No company
    outside the explicit/deterministic Master cohort is enqueued.
    """

    now = _utc(now or utc_now())
    if max_companies < 1 or max_companies > MAX_SCHEDULED_MASTER_COMPANIES:
        raise ValueError("CBR FINORG scheduled cohort must be between 1 and 100")
    dataset = session.scalar(
        select(DataSet).where(DataSet.code == DATASET_CODE).with_for_update()
    )
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {DATASET_CODE}")

    query = select(Company).order_by(Company.id).limit(max_companies)
    if cohort_inns is not None:
        normalized = tuple(dict.fromkeys(normalize_inn(value) for value in cohort_inns))
        if (
            not normalized
            or len(normalized) > max_companies
            or any(len(value) not in {10, 12} for value in normalized)
        ):
            raise ValueError("explicit CBR FINORG cohort is invalid or exceeds bound")
        query = select(Company).where(Company.inn.in_(normalized)).order_by(Company.id)
    companies = list(session.scalars(query))
    inns = tuple(
        normalize_inn(company.inn)
        for company in companies
        if len(normalize_inn(company.inn)) in {10, 12}
    )
    if cohort_inns is not None and set(inns) != set(normalized):
        raise InvalidDataError("explicit CBR FINORG cohort differs from Master")
    if not inns:
        raise InvalidDataError("CBR FINORG scheduled Master cohort is empty")
    if len(inns) > max_companies:
        raise InvalidDataError("CBR FINORG scheduled Master cohort exceeds bound")

    sweep_id = f"{SOURCE_ID}:{now.date().isoformat()}:master:{len(inns)}"
    creations = [
        enqueue_cbr_finorg_check(
            session,
            inn=inn,
            request_date=now.date(),
            raw_root=raw_root,
            now=now,
            execution_mode="scheduled_master_sweep",
            sweep_id=sweep_id,
            cohort_index=index,
            cohort_size=len(inns),
        )
        for index, inn in enumerate(inns, start=1)
    ]
    next_expected = now + CHECK_INTERVAL
    summary = CbrFinorgSweepSchedule(
        scheduled_at=now,
        request_date=now.date(),
        cohort_inns=inns,
        job_ids=tuple(str(item.job.id) for item in creations),
        created_jobs=sum(item.created for item in creations),
        existing_jobs=sum(not item.created for item in creations),
        next_expected_update_at=next_expected,
    )
    dataset.auto_update_status = AutoUpdateStatus.CONFIGURED
    dataset.next_expected_update_at = next_expected
    dataset.coverage = {
        **dict(dataset.coverage or {}),
        "scheduled_master_sweep": summary.as_dict(),
        "check_frequency": "daily",
        "on_demand": True,
    }
    return summary
