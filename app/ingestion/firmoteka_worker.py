"""Authorized, low-rate Firmoteka public-catalog crawl on Worker Foundation.

This is intentionally one source adapter, not a second supervisor.  PostgreSQL
owns crawl/checkpoint state, Worker Foundation owns leases/retry/run history,
and every public response is retained in content-addressed RAW storage.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.ingestion.firmoteka_parser import PARSER_VERSION, parse_firmoteka_page, source_as_of
from app.ingestion.mintrans_ted_registry import is_valid_inn, is_valid_ogrn
from app.models.company import Company, CompanyManager
from app.models.firmoteka import (
    FirmotekaCatalogPage,
    FirmotekaCompanySnapshot,
    FirmotekaCrawlItem,
    FirmotekaCrawlRun,
    FirmotekaQuarantineRecord,
    FirmotekaRawArtifact,
)
from app.models.registry_master import CompanyRegistryChange, MasterReplaySignal
from app.models.source import CompanySourceData, DataSet, DataSource
from app.models.worker import WorkerHandlerRegistration, WorkerJob
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
    LegalBlockError,
    SchemaMismatchError,
    WorkerNetworkError,
)
from app.worker.execution import JobCreation, create_job, register_handler
from app.worker.registry import HandlerRegistry


SOURCE_ID = DATASET_CODE = "firmoteka"
HANDLER_VERSION = "firmoteka-authorized-public-catalog-v1"
BASE_URL = "https://firmoteka.ru/"
ROBOTS_URL = urljoin(BASE_URL, "robots.txt")
SITEMAP_URL = urljoin(BASE_URL, "sitemap.xml")
TOP_CATALOG_URL = urljoin(BASE_URL, "top-5000-by-revenue")
CHECK_INTERVAL = timedelta(days=1)
MIN_REQUEST_DELAY_SECONDS = 4
COMPANY_BATCH_SIZE = 10
DAILY_REFRESH_LIMIT = 500
USER_AGENT = "next.company-source-worker/1.0 (authorized low-rate catalog crawl)"
CHALLENGE_MARKERS = (
    "captcha",
    "cloudflare",
    "access denied",
    "verify you are human",
    "слишком много запросов",
    "подтвердите, что вы не робот",
)
DISALLOWED_PATH_PREFIXES = ("/api/", "/p/", "/my/", "/_nuxt/builds/")
SAFE_HEADERS = {"content-type", "content-length", "date", "etag", "last-modified", "retry-after"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidDataError("Firmoteka timestamp must contain a timezone")
    return value.astimezone(timezone.utc)


def _allowed_public_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "firmoteka.ru"
        and not any(parsed.path.startswith(prefix) for prefix in DISALLOWED_PATH_PREFIXES)
        and parsed.username is None
        and parsed.password is None
    )


def _company_inn_from_url(url: str) -> str | None:
    if not _allowed_public_url(url):
        return None
    match = re.fullmatch(r"/(\d{10}|\d{12})/?", urlparse(url).path)
    inn = match.group(1) if match else None
    return inn if inn and is_valid_inn(inn) else None


def _extract_locs(content: bytes) -> tuple[str, ...]:
    text = content.decode("utf-8", errors="replace")
    return tuple(
        dict.fromkeys(
            value.strip()
            for value in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text, re.I)
            if _allowed_public_url(value.strip())
        )
    )


def _extract_company_links(content: bytes, base_url: str) -> tuple[tuple[str, str], ...]:
    text = content.decode("utf-8", errors="replace")
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href in re.findall(r"href=[\"']([^\"'#]+)", text, re.I):
        url = urljoin(base_url, href)
        inn = _company_inn_from_url(url)
        if inn and inn not in seen:
            seen.add(inn)
            rows.append((inn, url))
    return tuple(rows)


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        if path.read_bytes() != content:
            raise InvalidDataError(f"immutable Firmoteka artifact differs: {path}")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _write_json_once(path: Path, value: Mapping[str, Any]) -> None:
    _write_once(
        path,
        (json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode(),
    )


def _fetch(url: str, *, not_before: datetime | None) -> tuple[bytes, int, dict[str, str], datetime]:
    if not _allowed_public_url(url) and url != ROBOTS_URL:
        raise InvalidDataError("Firmoteka URL is outside the approved public surface")
    if not_before is not None:
        wait = (_utc(not_before) - utc_now()).total_seconds()
        if wait > 0:
            time.sleep(wait)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml,text/plain;q=0.9,*/*;q=0.1"})
    try:
        with urlopen(request, timeout=45) as response:
            content = response.read()
            status = int(response.status)
            headers = {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
                if str(key).lower() in SAFE_HEADERS
            }
    except HTTPError as error:
        if error.code == 403:
            raise LegalBlockError("Firmoteka public surface returned HTTP 403") from error
        if error.code == 429 or error.code >= 500:
            raise WorkerNetworkError(f"Firmoteka public surface returned HTTP {error.code}") from error
        raise SchemaMismatchError(f"Firmoteka public surface returned HTTP {error.code}") from error
    except (URLError, TimeoutError) as error:
        raise WorkerNetworkError("Firmoteka public surface request failed") from error
    head = content[:100_000].decode("utf-8", errors="ignore").casefold()
    if any(marker in head for marker in CHALLENGE_MARKERS):
        raise LegalBlockError("Firmoteka challenge/protection page detected")
    return content, status, headers, utc_now()


def _store_raw(
    *,
    raw_root: Path,
    content: bytes,
    kind: str,
    url: str,
    status: int,
    headers: dict[str, str],
    retrieved_at: datetime,
) -> tuple[RawArtifactReference, dict[str, Any]]:
    digest = sha256(content).hexdigest()
    directory = raw_root / SOURCE_ID / digest
    suffix = "html" if "html" in headers.get("content-type", "") or kind in {"catalog", "company"} else "xml"
    artifact = directory / f"response.{suffix}.gz"
    compressed = gzip.compress(content, compresslevel=6, mtime=0)
    _write_once(artifact, compressed)
    manifest = {
        "manifest_version": 1,
        "source_id": SOURCE_ID,
        "source_class": "authorized_bridge",
        "artifact_kind": kind,
        "source_url": url,
        "retrieved_at": retrieved_at.isoformat(),
        "http_status": status,
        "response_headers": headers,
        "response_sha256": digest,
        "stored_sha256": sha256(compressed).hexdigest(),
        "response_size": len(content),
        "stored_size": len(compressed),
        "parser_version": PARSER_VERSION,
        "immutable": True,
    }
    _write_json_once(
        directory / f"manifest-{sha256(url.encode()).hexdigest()}.json", manifest
    )
    return RawArtifactReference(artifact.as_uri(), digest, manifest), manifest


def _metadata(context: HandlerContext) -> tuple[str, UUID, Path, datetime | None]:
    metadata = context.schedule_metadata
    phase = str(metadata.get("phase") or "")
    if phase not in {"discovery", "catalog", "companies"}:
        raise InvalidDataError("invalid Firmoteka crawl phase")
    try:
        crawl_id = UUID(str(metadata["crawl_run_id"]))
    except (KeyError, ValueError) as error:
        raise InvalidDataError("Firmoteka crawl id is invalid") from error
    raw_value = str(metadata.get("raw_root") or "").strip()
    if not raw_value:
        raise InvalidDataError("Firmoteka raw_root is required")
    not_before = metadata.get("not_before")
    try:
        parsed_not_before = datetime.fromisoformat(str(not_before)) if not_before else None
    except ValueError as error:
        raise InvalidDataError("Firmoteka not_before is invalid") from error
    return phase, crawl_id, Path(raw_value).resolve(), parsed_not_before


def firmoteka_worker_handler(context: HandlerContext) -> HandlerResult:
    phase, crawl_id, raw_root, not_before = _metadata(context)
    raw_root.mkdir(parents=True, exist_ok=True)
    artifacts: list[RawArtifactReference] = []
    observations: list[dict[str, Any]] = []
    last_request_at: datetime | None = None

    if phase == "discovery":
        urls = (ROBOTS_URL, SITEMAP_URL)
        discovered: list[str] = [TOP_CATALOG_URL]
        for index, url in enumerate(urls):
            gate = not_before if index == 0 else last_request_at + timedelta(seconds=MIN_REQUEST_DELAY_SECONDS)
            content, status, headers, retrieved_at = _fetch(url, not_before=gate)
            last_request_at = retrieved_at
            artifact, manifest = _store_raw(
                raw_root=raw_root, content=content, kind="discovery", url=url,
                status=status, headers=headers, retrieved_at=retrieved_at,
            )
            artifacts.append(artifact)
            observations.append({"url": url, "manifest": manifest})
            if url == ROBOTS_URL:
                text = content.decode("utf-8", errors="replace")
                if "Sitemap: https://firmoteka.ru/sitemap.xml" not in text:
                    raise SchemaMismatchError("Firmoteka robots no longer advertises pinned sitemap")
            else:
                discovered.extend(_extract_locs(content))
        validation = {
            "phase": phase,
            "crawl_run_id": str(crawl_id),
            "documents": list(dict.fromkeys(discovered)),
            "observations": observations,
            "last_request_at": last_request_at.isoformat(),
        }
        pointer = artifacts[-1].artifact_reference
        counters = ExecutionCounters(records_seen=len(discovered), records_written=len(discovered))

    elif phase == "catalog":
        page_id = int(context.schedule_metadata["catalog_page_id"])
        url = str(context.schedule_metadata["url"])
        content, status, headers, retrieved_at = _fetch(url, not_before=not_before)
        last_request_at = retrieved_at
        artifact, manifest = _store_raw(
            raw_root=raw_root, content=content, kind="catalog", url=url,
            status=status, headers=headers, retrieved_at=retrieved_at,
        )
        artifacts.append(artifact)
        locs = _extract_locs(content) if urlparse(url).path.endswith(".xml") else ()
        companies = list(_extract_company_links(content, url))
        for loc in locs:
            inn = _company_inn_from_url(loc)
            if inn:
                companies.append((inn, loc))
        companies = list(dict.fromkeys(companies))
        documents = [loc for loc in locs if _company_inn_from_url(loc) is None]
        validation = {
            "phase": phase,
            "crawl_run_id": str(crawl_id),
            "catalog_page_id": page_id,
            "url": url,
            "documents": documents,
            "companies": [{"inn": inn, "url": item_url} for inn, item_url in companies],
            "manifest": manifest,
            "last_request_at": last_request_at.isoformat(),
        }
        pointer = artifact.artifact_reference
        counters = ExecutionCounters(records_seen=len(documents) + len(companies), records_written=len(documents) + len(companies))

    else:
        items = list(context.schedule_metadata.get("items") or ())
        if not items or len(items) > COMPANY_BATCH_SIZE:
            raise InvalidDataError("Firmoteka company batch is empty or unbounded")
        projections: list[dict[str, Any]] = []
        rejected = 0
        for index, item in enumerate(items):
            inn, url = str(item.get("inn") or ""), str(item.get("url") or "")
            if not is_valid_inn(inn) or _company_inn_from_url(url) != inn:
                raise InvalidDataError("Firmoteka company batch contains non-exact INN URL")
            gate = not_before if index == 0 else last_request_at + timedelta(seconds=MIN_REQUEST_DELAY_SECONDS)
            content, status, headers, retrieved_at = _fetch(url, not_before=gate)
            last_request_at = retrieved_at
            artifact, manifest = _store_raw(
                raw_root=raw_root, content=content, kind="company", url=url,
                status=status, headers=headers, retrieved_at=retrieved_at,
            )
            artifacts.append(artifact)
            projection = parse_firmoteka_page(
                content, requested_inn=inn, url=url, fetched_at=retrieved_at
            )
            content_hash = sha256(
                json.dumps(projection, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            normalized_path = raw_root / SOURCE_ID / artifact.checksum / "normalized.json"
            _write_json_once(normalized_path, projection)
            normalized_sha = sha256(normalized_path.read_bytes()).hexdigest()
            accepted = bool(
                projection.get("identity_match")
                and projection.get("name")
                and (
                    not projection.get("ogrn")
                    or is_valid_ogrn(str(projection["ogrn"]))
                )
            )
            rejected += int(not accepted)
            projections.append(
                {
                    "item_id": int(item["id"]),
                    "inn": inn,
                    "url": url,
                    "accepted": accepted,
                    "reject_reason": None if accepted else "identity_or_required_field_invalid",
                    "projection": projection if accepted else {},
                    "raw_sha256": artifact.checksum,
                    "raw_manifest": manifest,
                    "normalized_sha256": normalized_sha,
                    "normalized_path": str(normalized_path),
                    "content_hash": content_hash,
                    "retrieved_at": retrieved_at.isoformat(),
                }
            )
            context.report_counters(
                ExecutionCounters(records_seen=index + 1, records_written=index + 1 - rejected, records_rejected=rejected)
            )
            context.heartbeat()
        validation = {
            "phase": phase,
            "crawl_run_id": str(crawl_id),
            "projections": projections,
            "last_request_at": last_request_at.isoformat(),
        }
        pointer = Path(projections[0]["normalized_path"]).as_uri()
        counters = ExecutionCounters(records_seen=len(items), records_written=len(items) - rejected, records_rejected=rejected)

    return HandlerResult(
        raw_artifacts=tuple(artifacts),
        staging_result=StagingResult(
            pointer,
            ValidationResult(accepted=True, metadata=validation),
            checksum=sha256(json.dumps(validation, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        ),
        checksum_metadata={"phase": phase, "crawl_run_id": str(crawl_id), "last_request_at": last_request_at.isoformat()},
        counters=counters,
    )


def _upsert_document(session: Session, crawl: FirmotekaCrawlRun, url: str, *, position: int) -> bool:
    if not _allowed_public_url(url) or _company_inn_from_url(url):
        return False
    result = session.execute(
        pg_insert(FirmotekaCatalogPage)
        .values(
            crawl_run_id=crawl.id,
            position=position,
            url=url,
            url_hash=sha256(url.encode()).hexdigest(),
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["crawl_run_id", "url_hash"])
    )
    return bool(result.rowcount)


def _upsert_company_item(
    session: Session,
    crawl: FirmotekaCrawlRun,
    *,
    inn: str,
    url: str,
    discovered_from: str,
    position: int,
) -> bool:
    if not is_valid_inn(inn) or _company_inn_from_url(url) != inn:
        return False
    already_known = session.scalar(
        select(FirmotekaCompanySnapshot.id).where(
            FirmotekaCompanySnapshot.inn == inn,
            FirmotekaCompanySnapshot.is_current.is_(True),
        ).limit(1)
    )
    initial_status = "succeeded" if crawl.run_kind == "daily" and already_known else "pending"
    result = session.execute(
        pg_insert(FirmotekaCrawlItem)
        .values(
            crawl_run_id=crawl.id,
            position=position,
            inn=inn,
            source_url=url,
            discovered_from=discovered_from,
            status=initial_status,
        )
        .on_conflict_do_nothing(index_elements=["crawl_run_id", "inn"])
    )
    return bool(result.rowcount)


def _source_row(session: Session) -> DataSource:
    source = session.scalar(select(DataSource).where(DataSource.code == SOURCE_ID))
    if source is None:
        raise InvalidDataError("Firmoteka source metadata is not registered")
    return source


def _apply_master(
    session: Session,
    *,
    dataset: DataSet,
    source: DataSource,
    claim: Any,
    row: dict[str, Any],
    now: datetime,
) -> tuple[bool, bool, bool]:
    projection = dict(row["projection"])
    inn = str(row["inn"])
    company = session.scalar(select(Company).where(Company.inn == inn).with_for_update())
    created = company is None
    changed = False
    if company is None:
        company = Company(
            inn=inn,
            name=str(projection["name"]),
            entity_type=str(projection["entity_type"]),
            master_dataset_id=dataset.id,
            master_data_date=now.date(),
            master_authority="secondary",
            master_source=SOURCE_ID,
            official_registry_verified=False,
            source=SOURCE_ID,
            source_updated_at=now,
        )
        session.add(company)
        session.flush()
    bridge_provenance = {
        "source": SOURCE_ID,
        "source_class": "authorized_bridge",
        "source_url": row["url"],
        "raw_sha256": row["raw_sha256"],
        "content_hash": row["content_hash"],
        "retrieved_at": row["retrieved_at"],
    }
    provenance = dict(company.master_provenance or {})
    provenance["firmoteka"] = bridge_provenance
    company.master_provenance = provenance
    bridge_is_master = company.master_source == SOURCE_ID and not company.official_registry_verified
    for field in (
        "name", "full_name", "kpp", "ogrn", "status", "registration_date",
        "termination_date", "address", "okved", "activity",
    ):
        source_field = {"status": "status_normalized", "activity": "okved_name"}.get(field, field)
        value = projection.get(source_field)
        if field in {"registration_date", "termination_date"} and value:
            value = date.fromisoformat(value)
        if value is None:
            continue
        current = getattr(company, field)
        if current is None or bridge_is_master:
            changed = changed or current != value
            setattr(company, field, value)
    if bridge_is_master:
        company.master_data_date = now.date()
        company.source_updated_at = now
    session.execute(
        pg_insert(CompanySourceData)
        .values(
            company_id=company.id,
            source_id=source.id,
            normalized_payload=projection,
            source_updated_at=(
                datetime.combine(source_as_of(projection), datetime.min.time(), tzinfo=timezone.utc)
                if source_as_of(projection)
                else now
            ),
            fetched_at=now,
            status="success",
            error_message=None,
        )
        .on_conflict_do_update(
            index_elements=["company_id", "source_id"],
            set_={
                "normalized_payload": projection,
                "source_updated_at": (
                    datetime.combine(source_as_of(projection), datetime.min.time(), tzinfo=timezone.utc)
                    if source_as_of(projection)
                    else now
                ),
                "fetched_at": now,
                "status": "success",
                "error_message": None,
            },
        )
    )
    manager_name = str(projection.get("manager") or "").strip()
    if manager_name:
        manager = session.scalar(
            select(CompanyManager).where(
                CompanyManager.company_id == company.id,
                CompanyManager.full_name == manager_name,
                CompanyManager.is_current.is_(True),
            )
        )
        if manager is None:
            session.add(
                CompanyManager(
                    company_id=company.id,
                    full_name=manager_name,
                    position=projection.get("manager_position"),
                    is_current=True,
                    source=SOURCE_ID,
                    source_dataset_id=dataset.id,
                    source_data_date=now.date(),
                    source_record_key=f"{inn}:{row['content_hash']}",
                    observed_at=now,
                    official_registry_verified=False,
                )
            )
            changed = True
        elif manager.source == SOURCE_ID and not manager.official_registry_verified:
            manager.position = projection.get("manager_position")
            manager.observed_at = now
    if created or changed:
        event_type = "created" if created else "identity_changed"
        record_key = f"{inn}:{row['content_hash']}:{event_type}"
        change = CompanyRegistryChange(
            source_id=SOURCE_ID,
            company_id=company.id,
            run_id=claim.run_id,
            inn=inn,
            event_type=event_type,
            changed_fields={"bridge_enrichment": True, "official_registry_verified": False},
            source_data_date=now.date(),
            source_record_key=record_key,
        )
        session.add(change)
        session.flush()
        targets = tuple(
            session.scalars(
                select(DataSet.code).where(
                    DataSet.enabled.is_(True),
                    DataSet.code.notin_((SOURCE_ID, "fns_egrul", "fns_egrip")),
                )
            )
        )
        for target in targets:
            session.execute(
                pg_insert(MasterReplaySignal)
                .values(company_id=company.id, target_source_id=target, registry_change_id=change.id, status="pending")
                .on_conflict_do_nothing()
            )
    return created, changed, company.entity_type == "legal"


def _create_next_job(
    session: Session,
    *,
    crawl: FirmotekaCrawlRun,
    raw_root: str,
    now: datetime,
) -> JobCreation | None:
    not_before = (crawl.last_request_at + timedelta(seconds=crawl.request_delay_seconds)) if crawl.last_request_at else now
    items = tuple(
        session.scalars(
            select(FirmotekaCrawlItem)
            .where(FirmotekaCrawlItem.crawl_run_id == crawl.id, FirmotekaCrawlItem.status == "pending")
            .order_by(FirmotekaCrawlItem.position, FirmotekaCrawlItem.id)
            .limit(COMPANY_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
    )
    if items:
        crawl.phase = "companies"
        ids = "-".join(str(item.id) for item in items)
        return create_job(
            session,
            source_id=SOURCE_ID,
            job_type="firmoteka_company_batch",
            handler_version=HANDLER_VERSION,
            idempotency_key=f"{SOURCE_ID}:{crawl.id}:companies:{ids}:{HANDLER_VERSION}",
            schedule_metadata={
                "phase": "companies",
                "crawl_run_id": str(crawl.id),
                "raw_root": raw_root,
                "not_before": not_before.isoformat(),
                "items": [{"id": item.id, "inn": item.inn, "url": item.source_url} for item in items],
                "request_delay_seconds": crawl.request_delay_seconds,
                "concurrency": 1,
            },
            max_attempts=4,
            timeout_seconds=COMPANY_BATCH_SIZE * (MIN_REQUEST_DELAY_SECONDS + 50),
            now=not_before,
        )
    page = session.scalar(
        select(FirmotekaCatalogPage)
        .where(FirmotekaCatalogPage.crawl_run_id == crawl.id, FirmotekaCatalogPage.status == "pending")
        .order_by(FirmotekaCatalogPage.position, FirmotekaCatalogPage.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if page is not None:
        crawl.phase = "catalog"
        return create_job(
            session,
            source_id=SOURCE_ID,
            job_type="firmoteka_catalog_page",
            handler_version=HANDLER_VERSION,
            idempotency_key=f"{SOURCE_ID}:{crawl.id}:catalog:{page.id}:{HANDLER_VERSION}",
            schedule_metadata={
                "phase": "catalog",
                "crawl_run_id": str(crawl.id),
                "catalog_page_id": page.id,
                "url": page.url,
                "raw_root": raw_root,
                "not_before": not_before.isoformat(),
                "request_delay_seconds": crawl.request_delay_seconds,
                "concurrency": 1,
            },
            max_attempts=4,
            timeout_seconds=180,
            now=not_before,
        )
    return None


def _update_dataset_progress(dataset: DataSet, crawl: FirmotekaCrawlRun, now: datetime) -> None:
    coverage = dict(dataset.coverage or {})
    coverage.update(
        {
            "crawl_run_id": str(crawl.id),
            "crawl_kind": crawl.run_kind,
            "crawl_status": crawl.status,
            "crawl_phase": crawl.phase,
            "catalog_discovered": crawl.catalog_discovered,
            "company_discovered": crawl.company_discovered,
            "fetched": crawl.fetched_count,
            "parsed": crawl.parsed_count,
            "valid": crawl.valid_count,
            "new_master": crawl.new_master_count,
            "updated_master": crawl.updated_master_count,
            "legal": crawl.legal_count,
            "ip": crawl.ip_count,
            "quarantine": crawl.quarantine_count,
            "captcha": crawl.captcha_count,
            "raw_bytes": crawl.raw_bytes,
            "request_delay_seconds": crawl.request_delay_seconds,
            "concurrency": crawl.concurrency,
            "completion_scope": "publicly_enumerated_ssr_sitemap_catalog_surface",
            "national_completeness_claimed": False,
            "checkpoints_reached": [value for value in (500, 2000, 10000) if crawl.valid_count >= value],
            "api_projection": "company_source_data.firmoteka",
            "card_projection": "company_card.firmoteka",
        }
    )
    dataset.coverage = coverage
    dataset.checked_at = now
    dataset.record_count = int(crawl.valid_count)
    dataset.operational_status = OperationalStatus.UPDATING


def publish_firmoteka_result(session: Session, claim: Any, result: HandlerResult) -> HandlerResult:
    validation = dict(result.staging_result.validation.metadata if result.staging_result else {})
    crawl = session.get(FirmotekaCrawlRun, UUID(str(validation.get("crawl_run_id"))), with_for_update=True)
    dataset = session.scalar(select(DataSet).where(DataSet.code == DATASET_CODE).with_for_update())
    if crawl is None or dataset is None:
        raise InvalidDataError("Firmoteka crawl or dataset is unavailable")
    source = _source_row(session)
    now = utc_now()
    previous_source_date = dataset.last_data_date
    phase = validation["phase"]
    crawl.last_request_at = datetime.fromisoformat(validation["last_request_at"])
    crawl.last_checkpoint_at = now
    crawl.request_count += len(result.raw_artifacts)
    crawl.success_count += len(result.raw_artifacts)
    counts = dict(crawl.http_status_counts or {})
    counts["200"] = int(counts.get("200") or 0) + len(result.raw_artifacts)
    crawl.http_status_counts = counts
    raw_root = str(claim.schedule_metadata["raw_root"])
    for artifact in result.raw_artifacts:
        manifest = dict(artifact.manifest)
        session.execute(
            pg_insert(FirmotekaRawArtifact)
            .values(
                sha256=artifact.checksum,
                artifact_kind=manifest["artifact_kind"],
                source_url=manifest["source_url"],
                stored_path=urlparse(artifact.artifact_reference).path,
                media_type=manifest.get("response_headers", {}).get("content-type"),
                size_bytes=int(manifest["response_size"]),
                retrieved_at=datetime.fromisoformat(manifest["retrieved_at"]),
                http_status=int(manifest["http_status"]),
                response_headers=manifest.get("response_headers") or {},
                parser_version=PARSER_VERSION,
                manifest=manifest,
            )
            .on_conflict_do_nothing(index_elements=["sha256"])
        )
        crawl.raw_bytes += int(manifest["response_size"])

    if phase == "discovery":
        position = int(session.scalar(select(func.coalesce(func.max(FirmotekaCatalogPage.position), 0)).where(FirmotekaCatalogPage.crawl_run_id == crawl.id)) or 0)
        for url in validation["documents"]:
            position += 1
            crawl.catalog_discovered += int(_upsert_document(session, crawl, url, position=position))

    elif phase == "catalog":
        page = session.get(FirmotekaCatalogPage, int(validation["catalog_page_id"]), with_for_update=True)
        if page is None or page.crawl_run_id != crawl.id:
            raise InvalidDataError("Firmoteka catalog checkpoint differs from job")
        page.status = "succeeded"
        page.attempt_count += 1
        page.checked_at = now
        page.discovered_companies = len(validation["companies"])
        page.last_error = None
        doc_position = int(session.scalar(select(func.coalesce(func.max(FirmotekaCatalogPage.position), 0)).where(FirmotekaCatalogPage.crawl_run_id == crawl.id)) or 0)
        for url in validation["documents"]:
            doc_position += 1
            crawl.catalog_discovered += int(_upsert_document(session, crawl, url, position=doc_position))
        item_position = int(session.scalar(select(func.coalesce(func.max(FirmotekaCrawlItem.position), 0)).where(FirmotekaCrawlItem.crawl_run_id == crawl.id)) or 0)
        for item in validation["companies"]:
            item_position += 1
            crawl.company_discovered += int(
                _upsert_company_item(
                    session,
                    crawl,
                    inn=item["inn"],
                    url=item["url"],
                    discovered_from=page.url,
                    position=item_position,
                )
            )

    else:
        new_master = changed_master = published = rejected = legal = ip = 0
        for row in validation["projections"]:
            item = session.get(FirmotekaCrawlItem, int(row["item_id"]), with_for_update=True)
            if item is None or item.crawl_run_id != crawl.id or item.inn != row["inn"]:
                raise InvalidDataError("Firmoteka company checkpoint differs from job")
            item.attempt_count += 1
            item.fetched_at = datetime.fromisoformat(row["retrieved_at"])
            item.raw_sha256 = row["raw_sha256"]
            item.content_hash = row["content_hash"]
            if not row["accepted"]:
                item.status = "quarantined"
                item.last_error = row["reject_reason"]
                rejected += 1
                session.execute(
                    pg_insert(FirmotekaQuarantineRecord)
                    .values(
                        crawl_run_id=crawl.id,
                        requested_inn=item.inn,
                        source_url=item.source_url,
                        reason=row["reject_reason"],
                        raw_sha256=row["raw_sha256"],
                        safe_details={"identity_match": False},
                        observed_at=now,
                    )
                    .on_conflict_do_nothing()
                )
                continue
            item.status = "succeeded"
            item.last_error = None
            session.execute(
                update(FirmotekaCompanySnapshot)
                .where(FirmotekaCompanySnapshot.inn == item.inn, FirmotekaCompanySnapshot.is_current.is_(True))
                .values(is_current=False)
            )
            existing_snapshot = session.scalar(
                select(FirmotekaCompanySnapshot).where(
                    FirmotekaCompanySnapshot.inn == item.inn,
                    FirmotekaCompanySnapshot.content_hash == row["content_hash"],
                )
            )
            created, changed, is_legal = _apply_master(
                session, dataset=dataset, source=source, claim=claim, row=row, now=now
            )
            company_id = session.scalar(select(Company.id).where(Company.inn == item.inn))
            if existing_snapshot is None:
                session.add(
                    FirmotekaCompanySnapshot(
                        dataset_id=dataset.id,
                        company_id=company_id,
                        inn=item.inn,
                        ogrn=row["projection"].get("ogrn"),
                        source_url=item.source_url,
                        retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
                        source_as_of=source_as_of(row["projection"]),
                        raw_sha256=row["raw_sha256"],
                        normalized_sha256=row["normalized_sha256"],
                        normalized_path=row["normalized_path"],
                        content_hash=row["content_hash"],
                        projection=row["projection"],
                        is_current=True,
                    )
                )
                published += 1
            else:
                existing_snapshot.is_current = True
                existing_snapshot.company_id = company_id
            new_master += int(created)
            changed_master += int(changed and not created)
            legal += int(is_legal)
            ip += int(not is_legal)
        crawl.fetched_count += len(validation["projections"])
        crawl.parsed_count += len(validation["projections"]) - rejected
        crawl.valid_count += len(validation["projections"]) - rejected
        crawl.new_master_count += new_master
        crawl.updated_master_count += changed_master
        crawl.legal_count += legal
        crawl.ip_count += ip
        crawl.quarantine_count += rejected
        result = replace(
            result,
            counters=ExecutionCounters(
                records_seen=len(validation["projections"]),
                records_written=len(validation["projections"]) - rejected,
                records_rejected=rejected,
                records_published=published,
            ),
        )

    _update_dataset_progress(dataset, crawl, now)
    next_job = _create_next_job(session, crawl=crawl, raw_root=raw_root, now=now)
    if next_job is None:
        crawl.phase = "complete"
        crawl.status = "completed"
        crawl.completed_at = now
        coverage = dict(dataset.coverage or {})
        successful = int(coverage.get("successful_scheduled_checks") or 0) + 1
        coverage.update(
            {
                "crawl_status": "completed",
                "crawl_phase": "complete",
                "successful_scheduled_checks": successful,
                "operational_accepted": successful >= 2,
            }
        )
        dataset.coverage = coverage
        dataset.last_success_at = dataset.checked_at = dataset.published_at = now
        dataset.last_data_date = now.date()
        dataset.source_as_of = now
        dataset.retrieved_at = now
        dataset.official_actual_until = (now + CHECK_INTERVAL).date()
        dataset.operational_status = OperationalStatus.CURRENT
        dataset.next_expected_update_at = now + CHECK_INTERVAL
        dataset.last_error = None
        dataset.last_error_at = None
    else:
        dataset.next_expected_update_at = now
    source_records = int(crawl.valid_count + crawl.quarantine_count)
    summary = SourceChangeSummary(
        matched_companies=int(crawl.updated_master_count),
        new_facts=int(crawl.new_master_count),
        changed_facts=int(crawl.updated_master_count),
        removed_or_expired_facts=0,
        unchanged_facts=max(0, int(crawl.valid_count - crawl.new_master_count - crawl.updated_master_count)),
        replayed_facts=0,
        quarantined_records=int(crawl.quarantine_count),
        source_records=source_records,
        source_data_date=now.date(),
        previous_source_data_date=previous_source_date,
        unavailable_reasons=(
            {"previous_source_data_date": "first successful live crawl"}
            if previous_source_date is None
            else {}
        ),
    )
    return replace(result, change_summary=summary)


def register_firmoteka_worker(session: Session, registry: HandlerRegistry) -> Any:
    return register_handler(
        session,
        registry,
        source_id=SOURCE_ID,
        version=HANDLER_VERSION,
        handler=firmoteka_worker_handler,
        publisher=publish_firmoteka_result,
        approved=True,
        live=False,
        fixture=False,
        metadata={
            "mode": "authorized_public_catalog_crawl",
            "minimum_request_delay_seconds": MIN_REQUEST_DELAY_SECONDS,
            "concurrency": 1,
            "content_addressed_raw": True,
        },
    )


def schedule_firmoteka_check(
    session: Session,
    *,
    raw_root: Path,
    now: datetime | None = None,
) -> JobCreation:
    now = now or utc_now()
    approval = session.get(WorkerHandlerRegistration, (SOURCE_ID, HANDLER_VERSION))
    if approval is None or not approval.approved or not approval.enabled or approval.live_mode:
        raise HandlerNotRegisteredError(f"durable handler approval is missing: {SOURCE_ID}@{HANDLER_VERSION}")
    crawl = session.scalar(
        select(FirmotekaCrawlRun)
        .where(FirmotekaCrawlRun.status.in_(("running", "paused")))
        .order_by(FirmotekaCrawlRun.created_at.desc())
        .with_for_update()
        .limit(1)
    )
    if crawl is not None and crawl.status == "paused":
        raise InvalidDataError("Firmoteka crawl is paused by operator")
    if crawl is None:
        prior = session.scalar(
            select(FirmotekaCrawlRun)
            .where(FirmotekaCrawlRun.status == "completed")
            .order_by(FirmotekaCrawlRun.completed_at.desc())
            .limit(1)
        )
        crawl = FirmotekaCrawlRun(
            run_kind="daily" if prior else "initial",
            status="running",
            phase="discovery",
            cursor={"discovery": "robots_and_sitemap"},
            request_delay_seconds=MIN_REQUEST_DELAY_SECONDS,
            concurrency=1,
            started_at=now,
        )
        session.add(crawl)
        session.flush()
    existing = session.scalar(
        select(WorkerJob)
        .where(
            WorkerJob.source_id == SOURCE_ID,
            WorkerJob.status.in_(("queued", "running", "retry_scheduled")),
            WorkerJob.schedule_metadata["crawl_run_id"].astext == str(crawl.id),
        )
        .order_by(WorkerJob.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        return JobCreation(job=existing, created=False)
    if crawl.phase != "discovery":
        continuation = _create_next_job(session, crawl=crawl, raw_root=str(Path(raw_root).resolve()), now=now)
        if continuation is None:
            raise InvalidDataError("Firmoteka crawl has no runnable checkpoint")
        return continuation
    not_before = (crawl.last_request_at + timedelta(seconds=crawl.request_delay_seconds)) if crawl.last_request_at else now
    return create_job(
        session,
        source_id=SOURCE_ID,
        job_type="firmoteka_catalog_discovery",
        handler_version=HANDLER_VERSION,
        idempotency_key=f"{SOURCE_ID}:{crawl.id}:discovery:{HANDLER_VERSION}",
        schedule_metadata={
            "phase": "discovery",
            "crawl_run_id": str(crawl.id),
            "raw_root": str(Path(raw_root).resolve()),
            "not_before": not_before.isoformat(),
            "request_delay_seconds": crawl.request_delay_seconds,
            "concurrency": 1,
        },
        max_attempts=4,
        timeout_seconds=180,
        now=not_before,
    )


def firmoteka_card_projection(session: Session, *, company_id: int) -> dict[str, Any] | None:
    snapshot = session.scalar(
        select(FirmotekaCompanySnapshot)
        .where(
            FirmotekaCompanySnapshot.company_id == company_id,
            FirmotekaCompanySnapshot.is_current.is_(True),
        )
        .order_by(FirmotekaCompanySnapshot.retrieved_at.desc())
        .limit(1)
    )
    if snapshot is None:
        return None
    return {
        "status": "found",
        "source": "Firmoteka (authorized bridge)",
        "source_class": "authorized_bridge",
        "retrieved_at": snapshot.retrieved_at.isoformat(),
        "source_as_of": snapshot.source_as_of.isoformat() if snapshot.source_as_of else None,
        "raw_sha256": snapshot.raw_sha256,
        "facts": snapshot.projection.get("facts") or [],
        "limitations": [
            "Bridge evidence is not an official-registry assertion.",
            "Negative bridge observations are never clean official negatives.",
        ],
    }
