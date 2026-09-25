"""Official Roskomnadzor bulk channels on Worker Foundation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.ingestion.roskomnadzor import (
    parse_hosting_xlsx,
    parse_xml_snapshot,
    validate_complete_snapshot,
)
from app.models.roskomnadzor import RoskomnadzorCompanyFact, RoskomnadzorPrivatePersonRecord
from app.models.source import DataSet
from app.models.worker import WorkerHandlerRegistration, WorkerPublicationState
from app.worker.contracts import (
    ExecutionCounters,
    HandlerContext,
    HandlerResult,
    RawArtifactReference,
    SourceChangeSummary,
    StagingResult,
    ValidationResult,
)
from app.worker.errors import HandlerNotRegisteredError, InvalidDataError, SchemaMismatchError, WorkerNetworkError
from app.worker.execution import JobCreation, create_job, register_handler
from app.worker.registry import HandlerRegistry


HANDLER_VERSION = "roskomnadzor-official-bulk-v2"
CHECK_INTERVAL = timedelta(days=1)


@dataclass(frozen=True)
class RknBulkSpec:
    source_id: str
    channel: str
    listing_url: str
    data_format: str


SPECS = {
    "rkn_communications_licenses": RknBulkSpec(
        "rkn_communications_licenses", "communications",
        "https://rkn.gov.ru/opendata/7705846236-LicComm/", "xml",
    ),
    "rkn_broadcast_licenses": RknBulkSpec(
        "rkn_broadcast_licenses", "broadcast",
        "https://rkn.gov.ru/opendata/7705846236-LicBroadcast/", "xml",
    ),
    "rkn_registered_media": RknBulkSpec(
        "rkn_registered_media", "media",
        "https://rkn.gov.ru/opendata/7705846236-ResolutionSMI/", "xml",
    ),
    "rkn_information_distributors": RknBulkSpec(
        "rkn_information_distributors", "information_distributors",
        "https://rkn.gov.ru/opendata/7705846236-InformationDistributor/", "xml",
    ),
    "rkn_hosting_providers": RknBulkSpec(
        "rkn_hosting_providers", "hosting",
        "https://rkn.gov.ru/activity/connection/register/p1578/", "xlsx",
    ),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fetch(url: str) -> tuple[bytes, dict[str, str]]:
    if urlparse(url).scheme != "https" or urlparse(url).hostname != "rkn.gov.ru":
        raise InvalidDataError("Roskomnadzor artifact is outside pinned official host")
    request = Request(url, headers={"User-Agent": "next.company-source-worker/1.0", "Accept": "text/html,application/xml,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*;q=0.1"})
    try:
        with urlopen(request, timeout=120) as response:
            return response.read(), {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
                if str(key).lower() in {"content-type", "content-length", "date", "etag", "last-modified"}
            }
    except HTTPError as error:
        if error.code == 429 or error.code >= 500:
            raise WorkerNetworkError(f"Roskomnadzor returned HTTP {error.code}") from error
        raise SchemaMismatchError(f"Roskomnadzor returned HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError, IncompleteRead) as error:
        raise WorkerNetworkError("Roskomnadzor request failed") from error


def discover_release(spec: RknBulkSpec) -> dict[str, Any]:
    content, _headers = _fetch(spec.listing_url)
    html = content.decode("utf-8", errors="replace")
    hrefs = [urljoin(spec.listing_url, value) for value in re.findall(r"href=[\"']([^\"']+)", html, re.I)]
    if spec.data_format == "xml":
        candidates = []
        for url in hrefs:
            match = re.search(r"/data-(20\d{6})T\d+-structure-(20\d{6})T\d+\.xml(?:$|\?)", url)
            if match:
                stamp = match.group(1)
                candidates.append(
                    (date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:])), url)
                )
        xsd_urls = sorted({url.split("?", 1)[0] for url in hrefs if re.search(r"/structure-20\d{6}T\d+\.xsd(?:$|\?)", url)})
        if not candidates or not xsd_urls:
            raise SchemaMismatchError(f"{spec.source_id} listing has no XML/XSD release")
        source_date, artifact_url = max(candidates)
        xsd_url = xsd_urls[-1]
    else:
        candidates = []
        for url in hrefs:
            if not url.lower().split("?", 1)[0].endswith(".xlsx"):
                continue
            filename = Path(urlparse(url).path).name
            match = re.search(r"(\d{2})_(\d{2})_(20\d{2})", filename)
            if match:
                candidates.append((date(int(match.group(3)), int(match.group(2)), int(match.group(1))), url))
        if not candidates:
            raise SchemaMismatchError("rkn_hosting_providers listing has no dated XLSX")
        source_date, artifact_url = max(candidates)
        xsd_url = None
    identity = sha256(f"{artifact_url}|{xsd_url or ''}|{source_date.isoformat()}".encode()).hexdigest()
    return {
        "artifact_url": artifact_url,
        "xsd_url": xsd_url,
        "source_data_date": source_date.isoformat(),
        "release_identity": identity,
        "listing_url": spec.listing_url,
    }


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        if path.read_bytes() != content:
            raise InvalidDataError(f"immutable Roskomnadzor artifact differs: {path}")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _normalize(path: Path, parsed: dict[str, Any]) -> str:
    digest = sha256()
    descriptor, name = tempfile.mkstemp(prefix="rkn-normalized-", suffix=".jsonl", dir=path.parent)
    os.close(descriptor)
    temp = Path(name)
    try:
        with temp.open("wb") as output:
            for kind, rows in (("public", parsed["public_records"]), ("private", parsed["private_records"])):
                for row in rows:
                    line = (json.dumps({"kind": kind, **row}, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode()
                    output.write(line)
                    digest.update(line)
        if path.exists():
            if sha256(path.read_bytes()).hexdigest() != digest.hexdigest():
                raise InvalidDataError("immutable Roskomnadzor normalized snapshot differs")
        else:
            os.link(temp, path)
            path.chmod(0o444)
    finally:
        temp.unlink(missing_ok=True)
    return digest.hexdigest()


def run_rkn_bulk_handler(context: HandlerContext) -> HandlerResult:
    spec = SPECS.get(context.source_id)
    if spec is None:
        raise InvalidDataError("unknown Roskomnadzor bulk source")
    metadata = context.schedule_metadata
    if metadata.get("check_only"):
        return HandlerResult(
            checksum_metadata={"check_only": True, "release_identity": metadata["release_identity"]},
            counters=ExecutionCounters(),
        )
    raw_root = Path(str(metadata.get("raw_root") or "")).resolve()
    if not str(metadata.get("raw_root") or "").strip() or not raw_root.is_absolute():
        raise InvalidDataError("absolute raw_root is required")
    content, headers = _fetch(str(metadata["artifact_url"]))
    artifact_sha = sha256(content).hexdigest()
    artifact_dir = raw_root / spec.source_id / artifact_sha
    suffix = spec.data_format
    artifact_path = artifact_dir / f"artifact.{suffix}"
    _write_once(artifact_path, content)
    source_date = date.fromisoformat(str(metadata["source_data_date"]))
    try:
        parsed = (
            parse_xml_snapshot(content, channel=spec.channel, data_date=source_date)
            if spec.data_format == "xml"
            else parse_hosting_xlsx(content, data_date=source_date)
        )
        validate_complete_snapshot(parsed)
    except ValueError as error:
        raise SchemaMismatchError(str(error)) from error
    normalized_path = artifact_dir / "normalized.jsonl"
    normalized_sha = _normalize(normalized_path, parsed)
    retrieved_at = utc_now()
    manifest = {
        "source_id": spec.source_id, "source_owner": "Роскомнадзор",
        "listing_url": spec.listing_url, "source_url": metadata["artifact_url"],
        "source_data_date": source_date.isoformat(), "release_identity": metadata["release_identity"],
        "sha256": artifact_sha, "size": len(content), "content_type": headers.get("content-type"),
        "retrieved_at": retrieved_at.isoformat(), "immutable": True,
    }
    _write_once(artifact_dir / "manifest.json", (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode())
    raw_refs = [RawArtifactReference(artifact_path.as_uri(), artifact_sha, manifest)]
    xsd_sha = None
    if metadata.get("xsd_url"):
        xsd, xsd_headers = _fetch(str(metadata["xsd_url"]))
        xsd_sha = sha256(xsd).hexdigest()
        xsd_dir = raw_root / spec.source_id / xsd_sha
        xsd_path = xsd_dir / "schema.xsd"
        _write_once(xsd_path, xsd)
        xsd_manifest = {
            "source_id": spec.source_id, "artifact_kind": "xsd", "source_url": metadata["xsd_url"],
            "sha256": xsd_sha, "size": len(xsd), "content_type": xsd_headers.get("content-type"),
            "retrieved_at": retrieved_at.isoformat(), "immutable": True,
        }
        _write_once(xsd_dir / "manifest.json", (json.dumps(xsd_manifest, ensure_ascii=False, sort_keys=True) + "\n").encode())
        raw_refs.append(RawArtifactReference(xsd_path.as_uri(), xsd_sha, xsd_manifest))
    counters = ExecutionCounters(
        records_seen=int(parsed["source_records"]), records_written=int(parsed["imported_records"]),
        records_rejected=int(parsed["rejected_records"]), records_duplicated=int(parsed["duplicate_records"]),
    )
    context.report_counters(counters)
    context.heartbeat()
    validation = {
        **{key: int(parsed[key]) for key in ("source_records", "imported_records", "duplicate_records", "rejected_records")},
        "public_records": len(parsed["public_records"]), "private_records": len(parsed["private_records"]),
        "artifact_sha256": artifact_sha, "xsd_sha256": xsd_sha,
        "source_data_date": source_date.isoformat(), "release_identity": metadata["release_identity"],
    }
    return HandlerResult(
        raw_artifacts=tuple(raw_refs),
        staging_result=StagingResult(normalized_path.as_uri(), ValidationResult(accepted=True, metadata=validation), checksum=normalized_sha),
        checksum_metadata={"artifact_sha256": artifact_sha, "xsd_sha256": xsd_sha, "normalized_sha256": normalized_sha},
        counters=counters,
    )


def _iter_jsonl(uri: str) -> Iterator[dict[str, Any]]:
    parsed = urlparse(uri)
    path = Path(unquote(parsed.path))
    if parsed.scheme != "file" or not path.is_file():
        raise InvalidDataError("Roskomnadzor normalized snapshot is unavailable")
    with path.open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def publish_rkn_bulk_result(session: Session, claim: Any, result: HandlerResult) -> HandlerResult:
    spec = SPECS[claim.source_id]
    dataset = session.scalar(select(DataSet).where(DataSet.code == spec.source_id).with_for_update())
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {spec.source_id}")
    now = utc_now()
    previous_date = dataset.last_data_date
    state = session.scalar(select(WorkerPublicationState).where(WorkerPublicationState.source_id == spec.source_id))
    accepted_identity = (((state.validation_metadata or {}).get("validation") or {}).get("release_identity") if state else None)
    release_identity = str(claim.schedule_metadata["release_identity"])
    if result.staging_result is None or accepted_identity == release_identity:
        coverage = dict(dataset.coverage or {})
        successful = max(1, int(coverage.get("successful_scheduled_checks") or 0)) + 1
        coverage.update({"successful_scheduled_checks": successful, "operational_accepted": successful >= 2, "check_only": True})
        dataset.coverage = coverage
        dataset.checked_at = now
        dataset.official_actual_until = now.date()
        dataset.next_expected_update_at = now + CHECK_INTERVAL
        dataset.operational_status = OperationalStatus.CURRENT
        dataset.last_error = None
        summary = SourceChangeSummary(
            matched_companies=int(coverage.get("matched_companies") or 0), new_facts=0,
            changed_facts=0, removed_or_expired_facts=0,
            unchanged_facts=int(dataset.record_count or 0), replayed_facts=0,
            quarantined_records=0, source_records=int(coverage.get("source_records") or dataset.record_count or 0),
            source_data_date=dataset.last_data_date, previous_source_data_date=dataset.last_data_date,
            unavailable_reasons=({"source_data_date": "no accepted release", "previous_source_data_date": "no accepted release"} if dataset.last_data_date is None else {}),
        )
        return replace(result, change_summary=summary)
    validation = dict(result.staging_result.validation.metadata)
    old_keys = set(session.scalars(select(RoskomnadzorCompanyFact.record_key).where(RoskomnadzorCompanyFact.dataset_id == dataset.id)))
    session.execute(delete(RoskomnadzorCompanyFact).where(RoskomnadzorCompanyFact.dataset_id == dataset.id))
    session.execute(delete(RoskomnadzorPrivatePersonRecord).where(RoskomnadzorPrivatePersonRecord.dataset_id == dataset.id))
    public_batch: list[dict[str, Any]] = []
    private_batch: list[dict[str, Any]] = []
    new_keys: set[str] = set()
    matched_inns: set[str] = set()
    for row in _iter_jsonl(result.staging_result.staging_pointer):
        kind = row.pop("kind")
        for key in ("data_date", "issued_at", "valid_until"):
            if row.get(key):
                row[key] = date.fromisoformat(row[key])
        if kind == "public":
            matched_inns.add(row["inn"])
            new_keys.add(row["record_key"])
            public_batch.append({"dataset_id": dataset.id, **row})
            if len(public_batch) >= 1000:
                session.execute(pg_insert(RoskomnadzorCompanyFact), public_batch); public_batch = []
        else:
            private_batch.append({"dataset_id": dataset.id, **row})
            if len(private_batch) >= 1000:
                session.execute(pg_insert(RoskomnadzorPrivatePersonRecord), private_batch); private_batch = []
    if public_batch:
        session.execute(pg_insert(RoskomnadzorCompanyFact), public_batch)
    if private_batch:
        session.execute(pg_insert(RoskomnadzorPrivatePersonRecord), private_batch)
    source_date = date.fromisoformat(validation["source_data_date"])
    successful = int((dataset.coverage or {}).get("successful_scheduled_checks") or 0) + 1
    dataset.enabled = True
    dataset.dataset_kind = "bulk_snapshot"
    dataset.freshness_policy = "daily"
    dataset.auto_update_status = AutoUpdateStatus.CONFIGURED
    dataset.operational_status = OperationalStatus.CURRENT
    dataset.last_success_at = dataset.checked_at = dataset.published_at = now
    dataset.last_data_date = source_date
    dataset.source_as_of = datetime.combine(source_date, datetime.min.time(), tzinfo=timezone.utc)
    dataset.retrieved_at = now
    dataset.official_actual_until = now.date()
    dataset.record_count = int(validation["public_records"])
    dataset.next_expected_update_at = now + CHECK_INTERVAL
    dataset.coverage = {
        **validation, "matched_companies": len(matched_inns), "matching_method": "inn_exact",
        "privacy_boundary": "inn12_person_records_isolated_unpublished",
        "api_projection": f"roskomnadzor.{spec.channel}", "card_projection": f"company_card.roskomnadzor.{spec.channel}",
        "successful_scheduled_checks": successful, "operational_accepted": successful >= 2,
    }
    summary = SourceChangeSummary(
        matched_companies=len(matched_inns), new_facts=len(new_keys - old_keys),
        changed_facts=0, removed_or_expired_facts=len(old_keys - new_keys),
        unchanged_facts=len(old_keys & new_keys), replayed_facts=0,
        quarantined_records=int(validation["rejected_records"]), source_records=int(validation["source_records"]),
        source_data_date=source_date, previous_source_data_date=previous_date,
        unavailable_reasons=({"previous_source_data_date": "first successful publication"} if previous_date is None else {}),
    )
    return replace(result, counters=ExecutionCounters(
        records_seen=int(validation["source_records"]), records_written=int(validation["imported_records"]),
        records_rejected=int(validation["rejected_records"]), records_duplicated=int(validation["duplicate_records"]),
        records_published=int(validation["public_records"]),
    ), change_summary=summary)


def register_rkn_bulk_workers(session: Session, registry: HandlerRegistry) -> tuple[Any, ...]:
    return tuple(
        register_handler(
            session, registry, source_id=source_id, version=HANDLER_VERSION,
            handler=run_rkn_bulk_handler, publisher=publish_rkn_bulk_result,
            approved=True, live=False, fixture=False,
            metadata={"mode": "official_bulk_snapshot", "privacy_boundary": True, "channel": spec.channel},
        )
        for source_id, spec in SPECS.items()
    )


def schedule_rkn_bulk_check(
    session: Session,
    *,
    source_id: str,
    raw_root: Path,
    now: datetime | None = None,
) -> JobCreation:
    now = now or utc_now()
    approval = session.get(WorkerHandlerRegistration, (source_id, HANDLER_VERSION))
    if approval is None or not approval.approved or not approval.enabled or approval.live_mode:
        raise HandlerNotRegisteredError(f"durable handler approval is missing: {source_id}@{HANDLER_VERSION}")
    release = discover_release(SPECS[source_id])
    state = session.get(WorkerPublicationState, source_id)
    accepted = (((state.validation_metadata or {}).get("validation") or {}).get("release_identity") if state else None)
    check_only = bool(state and state.active_pointer and accepted == release["release_identity"])
    return create_job(
        session, source_id=source_id, job_type="roskomnadzor_official_bulk_check",
        handler_version=HANDLER_VERSION,
        idempotency_key=f"{source_id}:check:{now.date().isoformat()}:{release['release_identity']}:{HANDLER_VERSION}",
        schedule_metadata={**release, "raw_root": str(Path(raw_root).resolve()), "check_only": check_only, "check_frequency": "daily"},
        max_attempts=4, timeout_seconds=7200, now=now,
    )
