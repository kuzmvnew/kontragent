"""Worker Foundation adapters for bounded exact-INN source sweeps.

Each dataset remains a distinct source id, lease, run history and publication
state.  Shared mechanics here are transport plumbing, not merged source
semantics.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.models.company import Company
from app.models.corporate_disclosure import CorporateDisclosureCheck
from app.models.nostroy import NoprizMemberCheck, NostroyMemberCheck
from app.models.npd import NpdStatusCheck
from app.models.roskomnadzor import RoskomnadzorPdOperatorCheck
from app.models.source import DataSet
from app.models.worker import WorkerHandlerRegistration, WorkerJob
from app.providers.fns_npd_provider import API_URL as NPD_URL
from app.providers.nopriz_provider import NOPRIZ_API_URL, NOPRIZ_REGISTRY_URL
from app.providers.nostroy_provider import (
    NOSTROY_API_URL,
    NOSTROY_REGISTRY_URL,
    NostroyProviderError,
    parse_member_search,
)
from app.providers.prime_disclosure_provider import (
    COMPANY_URL,
    PrimeDisclosureProviderError,
    parse_prime_disclosure_html,
)
from app.providers.roskomnadzor_provider import PD_OPERATOR_URL, parse_pd_operator_html
from app.services.company_fact_service import sync_disclosure_profile_facts
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
    AccessRequiredError,
    HandlerNotRegisteredError,
    InvalidDataError,
    LegalBlockError,
    SchemaMismatchError,
    WorkerNetworkError,
)
from app.worker.execution import JobCreation, create_job, register_handler
from app.worker.registry import HandlerRegistry


HANDLER_VERSION = "exact-inn-source-sweep-v1"
CHECK_INTERVAL = timedelta(days=1)
SAFE_HEADERS = {"content-type", "content-length", "date", "etag", "last-modified", "retry-after"}
SPECS = {
    "fns_npd": {
        "kind": "npd", "url": NPD_URL, "entity_type": "individual_entrepreneur",
        "delay_seconds": 2, "cohort_limit": 100,
    },
    "nostroy_sro_members_on_demand": {
        "kind": "nostroy", "url": NOSTROY_API_URL, "entity_type": "legal",
        "delay_seconds": 3, "cohort_limit": 100,
    },
    "nopriz_sro_members_on_demand": {
        "kind": "nopriz", "url": NOPRIZ_API_URL, "entity_type": "legal",
        "delay_seconds": 3, "cohort_limit": 100,
    },
    "prime_corporate_disclosure": {
        "kind": "prime", "url": COMPANY_URL, "entity_type": "legal",
        "delay_seconds": 4, "cohort_limit": 100,
    },
    "rkn_personal_data_operators": {
        "kind": "rkn_pd", "url": PD_OPERATOR_URL, "entity_type": "legal",
        "delay_seconds": 4, "cohort_limit": 100,
    },
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        if path.read_bytes() != content:
            raise InvalidDataError(f"immutable source artifact differs: {path}")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _safe_headers(response: httpx.Response) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in response.headers.items()
        if str(key).lower() in SAFE_HEADERS
    }


def _request_error(source_id: str, error: Exception) -> Exception:
    if isinstance(error, (httpx.TimeoutException, httpx.RequestError)):
        return WorkerNetworkError(f"{source_id} exact-INN request failed")
    return error


def _validate_response_status(source_id: str, response: httpx.Response) -> None:
    if response.status_code == 403:
        raise LegalBlockError(f"{source_id} public source returned HTTP 403")
    if response.status_code == 429 or response.status_code >= 500:
        raise WorkerNetworkError(f"{source_id} public source returned HTTP {response.status_code}")
    if response.status_code != 200:
        raise SchemaMismatchError(f"{source_id} public source returned HTTP {response.status_code}")


def _perform_npd(inn: str, request_date: date) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    request_payload = {"inn": inn, "requestDate": request_date.isoformat()}
    try:
        with httpx.Client(timeout=65) as client:
            response = client.post(NPD_URL, json=request_payload)
    except Exception as error:
        raise _request_error("fns_npd", error) from error
    if response.status_code == 422:
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = {}
        code = error_payload.get("code")
        if code in {"taxpayer.status.service.limited.error", "taxpayer.status.service.unavailable.error"}:
            raise WorkerNetworkError(f"fns_npd temporarily unavailable: {code}")
    _validate_response_status("fns_npd", response)
    try:
        payload = response.json()
    except ValueError as error:
        raise SchemaMismatchError("fns_npd returned invalid JSON") from error
    if not isinstance(payload.get("status"), bool):
        raise SchemaMismatchError("fns_npd response misses boolean status")
    return (
        {"found": bool(payload["status"]), "is_npd": bool(payload["status"]), "message": payload.get("message"), "http_status": 200},
        [{"request": request_payload, "response": response.content, "headers": _safe_headers(response), "status": 200}],
    )


def _perform_sro(source_id: str, inn: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec = SPECS[source_id]
    referer = NOSTROY_REGISTRY_URL if spec["kind"] == "nostroy" else NOPRIZ_REGISTRY_URL
    observations: list[dict[str, Any]] = []
    combined: list[dict[str, Any]] = []
    total: int | None = None
    try:
        with httpx.Client(
            timeout=45,
            follow_redirects=True,
            http2=False,
            headers={"User-Agent": "next.company-source-worker/1.0 (low-load exact-INN lookup)", "Referer": referer, "Accept": "application/json"},
        ) as client:
            for page in range(1, 21):
                request_payload = {"filters": {}, "searchString": inn, "page": page, "pageCount": 100, "sortBy": {}}
                response = client.post(str(spec["url"]), json=request_payload)
                _validate_response_status(source_id, response)
                try:
                    payload = response.json()
                except ValueError as error:
                    raise SchemaMismatchError(f"{source_id} returned invalid JSON") from error
                data = payload.get("data") if isinstance(payload, dict) else None
                rows = data.get("data") if isinstance(data, dict) else None
                count = data.get("count") if isinstance(data, dict) else None
                if payload.get("success") is not True or not isinstance(rows, list) or not isinstance(count, int):
                    raise SchemaMismatchError(f"{source_id} response schema changed")
                total = count if total is None else total
                if count != total:
                    raise SchemaMismatchError(f"{source_id} pagination count changed during sweep")
                combined.extend(rows)
                observations.append({"request": request_payload, "response": response.content, "headers": _safe_headers(response), "status": 200})
                if len(combined) >= total or not rows:
                    break
    except (WorkerNetworkError, LegalBlockError, SchemaMismatchError):
        raise
    except Exception as error:
        raise _request_error(source_id, error) from error
    if total is None or len(combined) != total:
        raise SchemaMismatchError(f"{source_id} pagination did not reach declared total")
    try:
        parsed = parse_member_search({"success": True, "data": {"data": combined, "count": len(combined)}}, inn)
    except NostroyProviderError as error:
        raise SchemaMismatchError(f"{source_id}: {error.kind}") from error
    return {**parsed, "http_status": 200}, observations


def _perform_prime(inn: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    url = COMPANY_URL.format(inn=inn)
    try:
        with httpx.Client(timeout=30, follow_redirects=True, http2=False, headers={"User-Agent": "next.company-source-worker/1.0 (low-load exact-INN lookup)"}) as client:
            response = client.get(url)
    except Exception as error:
        raise _request_error("prime_corporate_disclosure", error) from error
    _validate_response_status("prime_corporate_disclosure", response)
    try:
        decoded = response.content.decode("windows-1251")
        parsed = parse_prime_disclosure_html(decoded, inn)
    except (UnicodeDecodeError, PrimeDisclosureProviderError) as error:
        raise SchemaMismatchError("prime_corporate_disclosure response rejected") from error
    return (
        {**parsed, "http_status": 200, "source_url": url},
        [{"request": {"url": url}, "response": response.content, "headers": _safe_headers(response), "status": 200}],
    )


def _perform_rkn_pd(inn: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    request_params = {"act": "search", "name_full": "", "inn": inn, "regn": ""}
    try:
        with httpx.Client(
            timeout=30,
            follow_redirects=True,
            http2=False,
            headers={"User-Agent": "next.company-source-worker/1.0 (low-load exact-INN lookup)", "Referer": PD_OPERATOR_URL},
        ) as client:
            response = client.get(PD_OPERATOR_URL, params=request_params)
    except Exception as error:
        raise _request_error("rkn_personal_data_operators", error) from error
    _validate_response_status("rkn_personal_data_operators", response)
    try:
        parsed = parse_pd_operator_html(response.text, inn)
    except Exception as error:
        kind = getattr(error, "kind", "invalid_response")
        if kind == "source_protection":
            raise LegalBlockError("rkn_personal_data_operators protection page detected") from error
        raise SchemaMismatchError("rkn_personal_data_operators response rejected") from error
    return (
        {**parsed, "http_status": 200},
        [{"request": request_params, "response": response.content, "headers": _safe_headers(response), "status": 200}],
    )


def exact_source_handler(context: HandlerContext) -> HandlerResult:
    source_id = context.source_id
    spec = SPECS.get(source_id)
    if spec is None:
        raise InvalidDataError(f"unknown exact-INN source: {source_id}")
    metadata = context.schedule_metadata
    inn = re.sub(r"\D", "", str(metadata.get("inn") or ""))
    expected_length = 12 if spec["entity_type"] == "individual_entrepreneur" else 10
    if len(inn) != expected_length:
        raise InvalidDataError(f"{source_id} job has invalid exact INN")
    try:
        request_date = date.fromisoformat(str(metadata["request_date"]))
    except (KeyError, ValueError) as error:
        raise InvalidDataError(f"{source_id} request date is invalid") from error
    raw_root = Path(str(metadata.get("raw_root") or "")).resolve()
    if not str(metadata.get("raw_root") or "").strip():
        raise InvalidDataError("raw_root is required")
    if spec["kind"] == "npd":
        normalized, observations = _perform_npd(inn, request_date)
    elif spec["kind"] in {"nostroy", "nopriz"}:
        normalized, observations = _perform_sro(source_id, inn)
    elif spec["kind"] == "rkn_pd":
        normalized, observations = _perform_rkn_pd(inn)
    else:
        normalized, observations = _perform_prime(inn)
    exchange = {
        "manifest_version": 1,
        "source_id": source_id,
        "source_url": str(spec["url"]),
        "inn": inn,
        "request_date": request_date.isoformat(),
        "matching_method": "inn_exact",
        "exchanges": [
            {
                "request": item["request"],
                "response_base64": base64.b64encode(item["response"]).decode("ascii"),
                "response_sha256": sha256(item["response"]).hexdigest(),
                "http_status": item["status"],
                "response_headers": item["headers"],
            }
            for item in observations
        ],
    }
    raw_bytes = (json.dumps(exchange, ensure_ascii=False, sort_keys=True) + "\n").encode()
    raw_sha = sha256(raw_bytes).hexdigest()
    artifact_dir = raw_root / source_id / raw_sha
    raw_path = artifact_dir / "exchange.json"
    _write_once(raw_path, raw_bytes)
    normalized_payload = {
        "source_id": source_id,
        "inn": inn,
        "request_date": request_date.isoformat(),
        "result": normalized,
    }
    normalized_bytes = (json.dumps(normalized_payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
    normalized_path = artifact_dir / "normalized.json"
    _write_once(normalized_path, normalized_bytes)
    manifest = {
        "source_id": source_id,
        "source_url": str(spec["url"]),
        "inn": inn,
        "request_date": request_date.isoformat(),
        "sha256": raw_sha,
        "size": len(raw_bytes),
        "immutable": True,
        "matching_method": "inn_exact",
        "retrieved_at": utc_now().isoformat(),
    }
    _write_once(artifact_dir / "manifest.json", (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode())
    counters = ExecutionCounters(records_seen=1, records_written=1, records_published=0)
    context.report_counters(counters)
    context.heartbeat()
    return HandlerResult(
        raw_artifacts=(RawArtifactReference(raw_path.as_uri(), raw_sha, manifest),),
        staging_result=StagingResult(
            normalized_path.as_uri(),
            ValidationResult(accepted=True, metadata=normalized_payload),
            checksum=sha256(normalized_bytes).hexdigest(),
        ),
        checksum_metadata={"raw_sha256": raw_sha, "inn": inn, "request_date": request_date.isoformat()},
        counters=counters,
    )


def _publish_npd(session: Session, *, inn: str, request_date: date, payload: dict[str, Any], now: datetime) -> int:
    session.execute(
        pg_insert(NpdStatusCheck)
        .values(
            inn=inn, request_date=request_date, result_status="success",
            is_npd=bool(payload["is_npd"]), message=payload.get("message"),
            http_status=payload["http_status"], error_code=None, checked_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_npd_status_inn_request_date",
            set_={"result_status": "success", "is_npd": bool(payload["is_npd"]), "message": payload.get("message"), "http_status": 200, "error_code": None, "checked_at": now},
        )
    )
    return 1


def _publish_sro(session: Session, *, source_id: str, inn: str, request_date: date, payload: dict[str, Any], now: datetime) -> int:
    model = NostroyMemberCheck if source_id.startswith("nostroy") else NoprizMemberCheck
    constraint = "uq_nostroy_member_check_inn_date" if model is NostroyMemberCheck else "uq_nopriz_member_check_inn_date"
    values = {
        "inn": inn, "request_date": request_date, "result_status": "success",
        "is_found": bool(payload["found"]), "record_count": int(payload["total"]),
        "public_records": list(payload["records"]), "http_status": 200,
        "error_code": None, "error_message": None, "checked_at": now,
    }
    session.execute(
        pg_insert(model).values(**values).on_conflict_do_update(
            constraint=constraint,
            set_={key: value for key, value in values.items() if key not in {"inn", "request_date"}},
        )
    )
    return int(payload["total"])


def _publish_prime(session: Session, *, dataset: DataSet, inn: str, request_date: date, payload: dict[str, Any], now: datetime) -> int:
    company_id = session.scalar(select(Company.id).where(Company.inn == inn))
    if company_id is None:
        raise InvalidDataError("Prime exact-INN company disappeared from Master")
    values = {
        "company_id": company_id, "dataset_id": dataset.id, "inn": inn,
        "request_date": request_date, "result_status": "success",
        "is_found": bool(payload["found"]), "profile": payload.get("profile"),
        "documents": list(payload.get("documents") or ()),
        "document_count": int(payload.get("document_count") or 0),
        "source_url": payload["source_url"], "http_status": 200,
        "error_code": None, "error_message": None, "checked_at": now,
    }
    session.execute(
        pg_insert(CorporateDisclosureCheck).values(**values).on_conflict_do_update(
            constraint="uq_corporate_disclosure_check",
            set_={key: value for key, value in values.items() if key not in {"company_id", "dataset_id", "inn", "request_date"}},
        )
    )
    if payload["found"] and payload.get("profile"):
        sync_disclosure_profile_facts(
            company_id=company_id, dataset_id=dataset.id, inn=inn,
            profile=payload["profile"], source_url=payload["source_url"], session=session,
        )
    return int(bool(payload["found"])) + int(payload.get("document_count") or 0)


def _publish_rkn_pd(session: Session, *, dataset: DataSet, inn: str, request_date: date, payload: dict[str, Any], now: datetime) -> int:
    values = {
        "dataset_id": dataset.id, "inn": inn, "request_date": request_date,
        "result_status": "success", "is_found": bool(payload["found"]),
        "record_count": int(payload["total"]), "public_records": list(payload["records"]),
        "http_status": 200, "error_code": None, "error_message": None, "checked_at": now,
    }
    session.execute(
        pg_insert(RoskomnadzorPdOperatorCheck).values(**values).on_conflict_do_update(
            constraint="uq_rkn_pd_operator_check",
            set_={key: value for key, value in values.items() if key not in {"dataset_id", "inn", "request_date"}},
        )
    )
    return int(payload["total"])


def publish_exact_source_result(session: Session, claim: Any, result: HandlerResult) -> HandlerResult:
    metadata = dict(result.staging_result.validation.metadata if result.staging_result else {})
    source_id = claim.source_id
    dataset = session.scalar(select(DataSet).where(DataSet.code == source_id).with_for_update())
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {source_id}")
    inn = str(metadata["inn"])
    request_date = date.fromisoformat(metadata["request_date"])
    payload = dict(metadata["result"])
    now = utc_now()
    previous_source_date = dataset.last_data_date
    if source_id == "fns_npd":
        published = _publish_npd(session, inn=inn, request_date=request_date, payload=payload, now=now)
    elif source_id in {"nostroy_sro_members_on_demand", "nopriz_sro_members_on_demand"}:
        published = _publish_sro(session, source_id=source_id, inn=inn, request_date=request_date, payload=payload, now=now)
    elif source_id == "prime_corporate_disclosure":
        published = _publish_prime(session, dataset=dataset, inn=inn, request_date=request_date, payload=payload, now=now)
    else:
        published = _publish_rkn_pd(session, dataset=dataset, inn=inn, request_date=request_date, payload=payload, now=now)
    found = bool(payload.get("found"))
    coverage = dict(dataset.coverage or {})
    sweep_id = str(claim.schedule_metadata["sweep_id"])
    cohort_size = int(claim.schedule_metadata["cohort_size"])
    completed_before = int(
        session.scalar(
            select(func.count()).select_from(WorkerJob).where(
                WorkerJob.source_id == source_id,
                WorkerJob.status == "succeeded",
                WorkerJob.schedule_metadata["sweep_id"].astext == sweep_id,
            )
        ) or 0
    )
    completed = completed_before + 1
    sweep = dict(coverage.get("active_sweep") or {})
    sweep.update(
        {
            "sweep_id": sweep_id,
            "request_date": request_date.isoformat(),
            "cohort_size": cohort_size,
            "completed": completed,
            "found": int(sweep.get("found") or 0) + int(found),
            "not_found": int(sweep.get("not_found") or 0) + int(not found),
            "published_facts": int(sweep.get("published_facts") or 0) + published,
            "matching_method": "inn_exact",
        }
    )
    coverage["active_sweep"] = sweep
    cycle_complete = completed >= cohort_size
    if cycle_complete:
        successful = int(coverage.get("successful_scheduled_checks") or 0) + 1
        coverage.update(
            {
                "last_sweep": sweep,
                "successful_scheduled_checks": successful,
                "operational_accepted": successful >= 2,
                "api_projection": source_id,
                "card_projection": f"company_card.{source_id}",
            }
        )
        dataset.last_success_at = dataset.published_at = now
        dataset.next_expected_update_at = now + CHECK_INTERVAL
    dataset.coverage = coverage
    dataset.enabled = True
    dataset.dataset_kind = "on_demand_api"
    dataset.freshness_policy = "daily"
    dataset.auto_update_status = AutoUpdateStatus.CONFIGURED
    dataset.operational_status = OperationalStatus.CURRENT
    dataset.checked_at = now
    dataset.last_data_date = request_date
    dataset.source_as_of = datetime.combine(request_date, datetime.min.time(), tzinfo=timezone.utc)
    dataset.retrieved_at = now
    dataset.official_actual_until = request_date
    dataset.record_count = int(coverage.get("last_sweep", sweep).get("cohort_size") or 0)
    dataset.last_error = None
    dataset.last_error_at = None
    summary = SourceChangeSummary(
        matched_companies=1,
        new_facts=published,
        changed_facts=0,
        removed_or_expired_facts=0,
        unchanged_facts=int(not found),
        replayed_facts=0,
        quarantined_records=0,
        source_records=1,
        source_data_date=request_date,
        previous_source_data_date=previous_source_date,
        unavailable_reasons=(
            {"previous_source_data_date": "first successful exact-INN sweep"}
            if previous_source_date is None
            else {}
        ),
    )
    return replace(
        result,
        counters=ExecutionCounters(records_seen=1, records_written=1, records_published=published),
        change_summary=summary,
    )


def register_exact_source_workers(session: Session, registry: HandlerRegistry) -> tuple[Any, ...]:
    return tuple(
        register_handler(
            session, registry, source_id=source_id, version=HANDLER_VERSION,
            handler=exact_source_handler, publisher=publish_exact_source_result,
            approved=True, live=False, fixture=False,
            metadata={"mode": "bounded_daily_master_exact_inn_sweep", "source_specific_lease": True},
        )
        for source_id in SPECS
    )


def schedule_exact_source_sweep(
    session: Session,
    *,
    source_id: str,
    raw_root: Path,
    now: datetime | None = None,
) -> tuple[JobCreation, ...]:
    now = now or utc_now()
    spec = SPECS[source_id]
    approval = session.get(WorkerHandlerRegistration, (source_id, HANDLER_VERSION))
    if approval is None or not approval.approved or not approval.enabled or approval.live_mode:
        raise HandlerNotRegisteredError(f"durable handler approval is missing: {source_id}@{HANDLER_VERSION}")
    active_job = session.scalar(
        select(WorkerJob)
        .where(
            WorkerJob.source_id == source_id,
            WorkerJob.status.in_(("queued", "running", "retry_scheduled")),
        )
        .order_by(WorkerJob.created_at, WorkerJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if active_job is not None:
        return (JobCreation(job=active_job, created=False),)
    companies = tuple(
        session.scalars(
            select(Company)
            .where(Company.entity_type == spec["entity_type"])
            .order_by(Company.id)
            .limit(int(spec["cohort_limit"]))
        )
    )
    if not companies:
        raise InvalidDataError(f"{source_id} has no applicable Master cohort")
    request_date = now.date()
    sweep_id = f"{source_id}:{request_date.isoformat()}:{sha256('|'.join(item.inn for item in companies).encode()).hexdigest()[:16]}"
    creations: list[JobCreation] = []
    for index, company in enumerate(companies):
        eligible_at = now + timedelta(seconds=index * int(spec["delay_seconds"]))
        creations.append(
            create_job(
                session, source_id=source_id, job_type="exact_inn_scheduled_sweep",
                handler_version=HANDLER_VERSION,
                idempotency_key=f"{sweep_id}:{company.inn}:{HANDLER_VERSION}",
                schedule_metadata={
                    "sweep_id": sweep_id, "cohort_size": len(companies),
                    "inn": company.inn, "company_id": company.id,
                    "request_date": request_date.isoformat(),
                    "raw_root": str(Path(raw_root).resolve()),
                    "matching_method": "inn_exact", "scheduled_index": index,
                },
                max_attempts=4, timeout_seconds=120, now=eligible_at,
            )
        )
    dataset = session.scalar(select(DataSet).where(DataSet.code == source_id).with_for_update())
    if dataset is not None:
        coverage = dict(dataset.coverage or {})
        coverage["scheduled_sweep"] = {"sweep_id": sweep_id, "cohort_size": len(companies), "request_date": request_date.isoformat()}
        dataset.coverage = coverage
        # The durable jobs and their own retry policy own this sweep until it
        # reaches terminal state.  Do not keep the scheduler immediately due:
        # Master growth during a running sweep must not fan out another full
        # cohort on every poll.
        dataset.next_expected_update_at = now + CHECK_INTERVAL
    return tuple(creations)


def eis_rnp_access_handler(_context: HandlerContext) -> HandlerResult:
    if not os.environ.get("EIS_IP_TOKEN", "").strip():
        raise AccessRequiredError("EIS/RNP requires EIS_IP_TOKEN and an approved baseline channel")
    raise AccessRequiredError("EIS/RNP token exists but baseline archive schema must be accepted before publication")


def register_eis_rnp_worker(session: Session, registry: HandlerRegistry) -> Any:
    return register_handler(
        session, registry, source_id="eis_rnp", version=HANDLER_VERSION,
        handler=eis_rnp_access_handler, publisher=None, approved=True, live=False,
        fixture=False, metadata={"mode": "official_soap_access_gate", "secrets_in_raw": False},
    )


def schedule_eis_rnp_check(session: Session, *, raw_root: Path, now: datetime | None = None) -> JobCreation:
    if not os.environ.get("EIS_IP_TOKEN", "").strip():
        raise AccessRequiredError("EIS/RNP requires EIS_IP_TOKEN")
    now = now or utc_now()
    return create_job(
        session, source_id="eis_rnp", job_type="eis_rnp_schema_probe",
        handler_version=HANDLER_VERSION,
        idempotency_key=f"eis_rnp:schema-probe:{now.date().isoformat()}:{HANDLER_VERSION}",
        schedule_metadata={"raw_root": str(Path(raw_root).resolve()), "secret_redaction_required": True},
        max_attempts=1, timeout_seconds=300, now=now,
    )
