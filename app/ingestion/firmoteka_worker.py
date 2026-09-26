"""Authorized, low-rate Firmoteka public-catalog crawl on Worker Foundation.

This is intentionally one source adapter, not a second supervisor.  PostgreSQL
owns crawl/checkpoint state, Worker Foundation owns leases/retry/run history,
and every public response is retained in content-addressed RAW storage.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from threading import Lock
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.ingestion.firmoteka_parser import PARSER_VERSION, parse_firmoteka_page, source_as_of
from app.ingestion.mintrans_ted_registry import is_valid_inn, is_valid_ogrn
from app.models.company import Company, CompanyManager
from app.models.company_enrichment import CompanyEnrichmentRun
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
HANDLER_VERSION = "firmoteka-authorized-public-catalog-v3"
BASE_URL = "https://firmoteka.ru/"
ROBOTS_URL = urljoin(BASE_URL, "robots.txt")
SITEMAP_URL = urljoin(BASE_URL, "sitemap.xml")
TOP_CATALOG_URL = urljoin(BASE_URL, "top-5000-by-revenue")
CHECK_INTERVAL = timedelta(days=1)
MIN_REQUEST_DELAY_SECONDS = 4
COMPANY_BATCH_SIZE = 10
DAILY_REFRESH_LIMIT = 0
DEFAULT_BACKPRESSURE_THRESHOLD = 2_000
DEFAULT_DAILY_REFRESH_HORIZON_DAYS = 7
MAX_LANE_CONCURRENCY = 64
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
_RAW_WRITE_LOCK = Lock()


@dataclass(frozen=True)
class FirmotekaScaleConfig:
    """Non-secret persisted scale controls for one durable crawl."""

    catalog_concurrency: int = 1
    company_concurrency: int = 1
    min_request_gap_seconds: int = MIN_REQUEST_DELAY_SECONDS
    backpressure_threshold: int = DEFAULT_BACKPRESSURE_THRESHOLD
    daily_refresh_horizon_days: int = DEFAULT_DAILY_REFRESH_HORIZON_DAYS
    daily_refresh_budget: int = DAILY_REFRESH_LIMIT

    def __post_init__(self) -> None:
        if not 1 <= self.catalog_concurrency <= MAX_LANE_CONCURRENCY:
            raise ValueError("Firmoteka catalog concurrency must be between 1 and 64")
        if not 1 <= self.company_concurrency <= MAX_LANE_CONCURRENCY:
            raise ValueError("Firmoteka company concurrency must be between 1 and 64")
        if self.min_request_gap_seconds < MIN_REQUEST_DELAY_SECONDS:
            raise ValueError("Firmoteka per-lane request gap must be at least 4 seconds")
        if self.backpressure_threshold <= 0:
            raise ValueError("Firmoteka backpressure threshold must be positive")
        if self.daily_refresh_horizon_days <= 0:
            raise ValueError("Firmoteka daily refresh horizon must be positive")
        if self.daily_refresh_budget < 0:
            raise ValueError("Firmoteka daily refresh budget cannot be negative")

    @classmethod
    def from_environment(cls) -> "FirmotekaScaleConfig":
        def value(name: str, default: int) -> int:
            raw = str(os.environ.get(name, default)).strip()
            try:
                return int(raw)
            except ValueError as error:
                raise InvalidDataError(
                    f"Firmoteka scale setting {name} must be an integer"
                ) from error

        return cls(
            catalog_concurrency=value("FIRMOTEKA_CATALOG_CONCURRENCY", 1),
            company_concurrency=value("FIRMOTEKA_COMPANY_CONCURRENCY", 1),
            min_request_gap_seconds=value(
                "FIRMOTEKA_MIN_REQUEST_GAP_SECONDS", MIN_REQUEST_DELAY_SECONDS
            ),
            backpressure_threshold=value(
                "FIRMOTEKA_BACKPRESSURE_THRESHOLD", DEFAULT_BACKPRESSURE_THRESHOLD
            ),
            daily_refresh_horizon_days=value(
                "FIRMOTEKA_DAILY_REFRESH_HORIZON_DAYS",
                DEFAULT_DAILY_REFRESH_HORIZON_DAYS,
            ),
            daily_refresh_budget=value(
                "FIRMOTEKA_DAILY_REFRESH_BUDGET", DAILY_REFRESH_LIMIT
            ),
        )


@dataclass(frozen=True)
class _EgressLane:
    key: str
    proxy_url: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _LaneFailure:
    task: dict[str, Any]
    error: Exception = field(repr=False)


def _egress_lanes(concurrency: int) -> tuple[_EgressLane, ...]:
    """Load optional proxy credentials only in handler memory.

    The JSON is never copied into a job, crawl row, RAW manifest, error message,
    or handler registration.  Persisted lane keys are anonymous ordinals.
    """

    pool_file = str(os.environ.get("FIRMOTEKA_EGRESS_POOL_FILE") or "").strip()
    raw = str(os.environ.get("FIRMOTEKA_EGRESS_POOL_JSON") or "").strip()
    if pool_file:
        if raw:
            raise InvalidDataError("Firmoteka egress pool has conflicting configuration")
        try:
            path = Path(pool_file).expanduser().resolve(strict=True)
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise InvalidDataError("Firmoteka egress pool file permissions must be 0600")
            raw = path.read_text(encoding="utf-8").strip()
        except InvalidDataError:
            raise
        except OSError:
            raise InvalidDataError("Firmoteka egress pool file is unavailable") from None
    if not raw:
        return tuple(_EgressLane(key=f"lane-{index}") for index in range(concurrency))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # JSON decoder exceptions embed the input document, which may contain
        # proxy credentials.  Do not retain that exception chain.
        raise InvalidDataError("Firmoteka egress pool JSON is invalid") from None
    if not isinstance(payload, list) or len(payload) < concurrency:
        raise InvalidDataError("Firmoteka egress pool has fewer lanes than configured")
    lanes: list[_EgressLane] = []
    for index, item in enumerate(payload[:concurrency]):
        proxy_url = item.get("proxy_url") if isinstance(item, dict) else None
        parsed = urlparse(str(proxy_url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise InvalidDataError("Firmoteka egress pool contains an invalid proxy")
        lanes.append(_EgressLane(key=f"lane-{index}", proxy_url=str(proxy_url)))
    return tuple(lanes)


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


def _decoded_source_body(content: bytes) -> bytes:
    """Decode a gzip HTTP body for parsing while retaining the wire bytes in RAW."""

    if not content.startswith(b"\x1f\x8b"):
        return content
    try:
        return gzip.decompress(content)
    except OSError as error:
        raise SchemaMismatchError("Firmoteka returned an invalid gzip body") from error


def _write_once(path: Path, content: bytes) -> None:
    # Worker Foundation holds the source lease across the handler, so the lock
    # only has to close the intra-process race introduced by parallel lanes.
    with _RAW_WRITE_LOCK:
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


def _fetch(
    url: str,
    *,
    not_before: datetime | None,
    egress_lane: _EgressLane | None = None,
) -> tuple[bytes, int, dict[str, str], datetime]:
    if not _allowed_public_url(url) and url != ROBOTS_URL:
        raise InvalidDataError("Firmoteka URL is outside the approved public surface")
    if not_before is not None:
        wait = (_utc(not_before) - utc_now()).total_seconds()
        if wait > 0:
            time.sleep(wait)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml,text/plain;q=0.9,*/*;q=0.1"})
    try:
        open_request = (
            build_opener(ProxyHandler({"http": egress_lane.proxy_url, "https": egress_lane.proxy_url})).open
            if egress_lane is not None and egress_lane.proxy_url
            else urlopen
        )
        with open_request(request, timeout=45) as response:
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
    except (URLError, TimeoutError, OSError, ValueError):
        # Proxy transports can embed credentials in their native exceptions.
        # The public error deliberately drops that exception chain.
        raise WorkerNetworkError("Firmoteka public surface request failed") from None
    head = _decoded_source_body(content)[:100_000].decode(
        "utf-8", errors="ignore"
    ).casefold()
    if any(marker in head for marker in CHALLENGE_MARKERS):
        raise LegalBlockError("Firmoteka challenge/protection page detected")
    return content, status, headers, utc_now()


def _parse_lane_not_before(value: Mapping[str, Any] | None) -> dict[str, datetime]:
    result: dict[str, datetime] = {}
    for key, raw in dict(value or {}).items():
        if not re.fullmatch(r"lane-\d+", str(key)):
            raise InvalidDataError("Firmoteka lane key is invalid")
        try:
            result[str(key)] = _utc(datetime.fromisoformat(str(raw)))
        except ValueError as error:
            raise InvalidDataError("Firmoteka lane checkpoint is invalid") from error
    return result


def _run_parallel_lanes(
    tasks: list[dict[str, Any]],
    *,
    concurrency: int,
    min_gap_seconds: int,
    lane_not_before: Mapping[str, Any] | None,
    fetch_task: Any,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
    now: Any = utc_now,
) -> tuple[list[Any], dict[str, str], datetime]:
    """Run deterministic lane partitions with an independent monotonic gap.

    One lane is always sequential.  Different lanes may make requests at the
    same time; no process-global sleep or rate clock is used.
    """

    if not tasks:
        raise InvalidDataError("Firmoteka parallel batch is empty")
    if min_gap_seconds < MIN_REQUEST_DELAY_SECONDS:
        raise InvalidDataError("Firmoteka per-lane request gap is below four seconds")
    lanes = _egress_lanes(min(concurrency, len(tasks)))
    durable_gate = _parse_lane_not_before(lane_not_before)
    buckets: list[list[tuple[int, dict[str, Any]]]] = [list() for _ in lanes]
    for index, task in enumerate(tasks):
        buckets[index % len(lanes)].append((index, task))

    def run_lane(lane: _EgressLane, bucket: list[tuple[int, dict[str, Any]]]):
        rows: list[tuple[int, Any]] = []
        last_started_monotonic: float | None = None
        last_started_at: datetime | None = None
        first_not_before = durable_gate.get(lane.key)
        for index, task in bucket:
            if first_not_before is not None:
                wait = (first_not_before - now()).total_seconds()
                if wait > 0:
                    sleep(wait)
                first_not_before = None
            if last_started_monotonic is not None:
                wait = min_gap_seconds - (monotonic() - last_started_monotonic)
                if wait > 0:
                    sleep(wait)
            last_started_monotonic = monotonic()
            last_started_at = now()
            try:
                outcome = fetch_task(task, lane)
            except Exception as error:  # isolate a failed checkpoint from its peers
                outcome = _LaneFailure(task=task, error=error)
            rows.append((index, outcome))
        return rows, lane.key, last_started_at

    indexed: list[tuple[int, Any]] = []
    lane_state: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(lanes), thread_name_prefix="firmoteka") as pool:
        futures = [
            pool.submit(run_lane, lane, bucket)
            for lane, bucket in zip(lanes, buckets)
            if bucket
        ]
        for future in futures:
            rows, lane_key, last_started_at = future.result()
            indexed.extend(rows)
            if last_started_at is not None:
                lane_state[lane_key] = last_started_at.isoformat()
    indexed.sort(key=lambda pair: pair[0])
    retrieved_values = [
        row[1]["retrieved_at"]
        for row in indexed
        if isinstance(row[1], dict) and row[1].get("retrieved_at")
    ]
    latest_retrieved_at = (
        max(
            datetime.fromisoformat(value) if isinstance(value, str) else value
            for value in retrieved_values
        )
        if retrieved_values
        else now()
    )
    return [row for _, row in indexed], lane_state, latest_retrieved_at


def _store_raw(
    *,
    raw_root: Path,
    content: bytes,
    kind: str,
    url: str,
    status: int,
    headers: dict[str, str],
    retrieved_at: datetime,
    latency_ms: int | None = None,
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
    if latency_ms is not None:
        manifest["latency_ms"] = max(0, int(latency_ms))
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str) + "\n"
    ).encode()
    observation_digest = sha256(manifest_bytes).hexdigest()
    _write_once(
        directory
        / f"manifest-{sha256(url.encode()).hexdigest()}-{observation_digest}.json",
        manifest_bytes,
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
    min_gap_seconds = int(
        context.schedule_metadata.get("request_delay_seconds")
        or MIN_REQUEST_DELAY_SECONDS
    )
    lane_not_before = dict(
        context.schedule_metadata.get("lane_not_before") or {}
    )
    lane_request_state: dict[str, str] = {}

    if phase == "discovery":
        urls = (ROBOTS_URL, SITEMAP_URL)
        discovered: list[str] = [TOP_CATALOG_URL]
        discovery_lane = _egress_lanes(1)[0]
        for index, url in enumerate(urls):
            durable = _parse_lane_not_before(lane_not_before).get("lane-0")
            if index == 0:
                gates = [
                    value for value in (not_before, durable) if value is not None
                ]
                gate = max(gates) if gates else None
            else:
                gate = last_request_at + timedelta(seconds=min_gap_seconds)
            if gate is not None:
                wait = (_utc(gate) - utc_now()).total_seconds()
                if wait > 0:
                    time.sleep(wait)
            request_started = time.monotonic()
            content, status, headers, retrieved_at = _fetch(
                url, not_before=None, egress_lane=discovery_lane
            )
            latency_ms = max(0, round((time.monotonic() - request_started) * 1000))
            last_request_at = retrieved_at
            lane_request_state["lane-0"] = retrieved_at.isoformat()
            artifact, manifest = _store_raw(
                raw_root=raw_root, content=content, kind="discovery", url=url,
                status=status, headers=headers, retrieved_at=retrieved_at,
                latency_ms=latency_ms,
            )
            artifacts.append(artifact)
            observations.append(
                {"url": url, "manifest": manifest, "latency_ms": latency_ms}
            )
            decoded = _decoded_source_body(content)
            if url == ROBOTS_URL:
                text = decoded.decode("utf-8", errors="replace")
                if "Sitemap: https://firmoteka.ru/sitemap.xml" not in text:
                    raise SchemaMismatchError("Firmoteka robots no longer advertises pinned sitemap")
            else:
                discovered.extend(_extract_locs(decoded))
        validation = {
            "phase": phase,
            "crawl_run_id": str(crawl_id),
            "documents": list(dict.fromkeys(discovered)),
            "observations": observations,
            "request_metrics": [
                {"http_status": row["manifest"]["http_status"], "latency_ms": row["latency_ms"]}
                for row in observations
            ],
            "last_request_at": last_request_at.isoformat(),
            "lane_request_state": lane_request_state,
        }
        pointer = artifacts[-1].artifact_reference
        counters = ExecutionCounters(records_seen=len(discovered), records_written=len(discovered))

    elif phase == "catalog":
        pages = list(context.schedule_metadata.get("catalog_pages") or ())
        if not pages and context.schedule_metadata.get("catalog_page_id"):
            pages = [{
                "id": context.schedule_metadata["catalog_page_id"],
                "url": context.schedule_metadata["url"],
            }]
        concurrency = int(context.schedule_metadata.get("catalog_concurrency") or 1)
        if not pages or len(pages) > concurrency:
            raise InvalidDataError("Firmoteka catalog batch is empty or unbounded")

        def fetch_catalog(task: dict[str, Any], lane: _EgressLane) -> dict[str, Any]:
            url = str(task["url"])
            request_started = time.monotonic()
            content, status, headers, retrieved_at = _fetch(
                url, not_before=None, egress_lane=lane
            )
            latency_ms = max(0, round((time.monotonic() - request_started) * 1000))
            artifact, manifest = _store_raw(
                raw_root=raw_root,
                content=content,
                kind="catalog",
                url=url,
                status=status,
                headers=headers,
                retrieved_at=retrieved_at,
                latency_ms=latency_ms,
            )
            decoded = _decoded_source_body(content)
            locs = _extract_locs(decoded) if urlparse(url).path.endswith(".xml") else ()
            companies = list(_extract_company_links(decoded, url))
            for loc in locs:
                inn = _company_inn_from_url(loc)
                if inn:
                    companies.append((inn, loc))
            companies = list(dict.fromkeys(companies))
            return {
                "catalog_page_id": int(task["id"]),
                "url": url,
                "documents": [loc for loc in locs if _company_inn_from_url(loc) is None],
                "companies": [
                    {"inn": inn, "url": item_url} for inn, item_url in companies
                ],
                "artifact": artifact,
                "manifest": manifest,
                "retrieved_at": retrieved_at,
                "latency_ms": latency_ms,
            }

        page_outcomes, lane_request_state, last_request_at = _run_parallel_lanes(
            pages,
            concurrency=concurrency,
            min_gap_seconds=min_gap_seconds,
            lane_not_before=lane_not_before,
            fetch_task=fetch_catalog,
        )
        page_failures = [row for row in page_outcomes if isinstance(row, _LaneFailure)]
        page_results = [row for row in page_outcomes if not isinstance(row, _LaneFailure)]
        if page_failures and not page_results:
            raise page_failures[0].error
        artifacts.extend(row.pop("artifact") for row in page_results)
        for row in page_results:
            row["retrieved_at"] = row["retrieved_at"].isoformat()
        validation = {
            "phase": phase,
            "crawl_run_id": str(crawl_id),
            "catalog_pages": page_results,
            "failed_catalog_pages": [
                {
                    "id": int(failure.task["id"]),
                    "error_kind": type(failure.error).__name__,
                }
                for failure in page_failures
            ],
            "request_metrics": [
                {"http_status": row["manifest"]["http_status"], "latency_ms": row["latency_ms"]}
                for row in page_results
            ],
            "last_request_at": last_request_at.isoformat(),
            "lane_request_state": lane_request_state,
        }
        pointer = artifacts[-1].artifact_reference
        catalog_records = sum(
            len(row["documents"]) + len(row["companies"]) for row in page_results
        )
        counters = ExecutionCounters(
            records_seen=catalog_records, records_written=catalog_records
        )

    else:
        items = list(context.schedule_metadata.get("items") or ())
        concurrency = int(context.schedule_metadata.get("company_concurrency") or 1)
        if not items or len(items) > COMPANY_BATCH_SIZE * concurrency:
            raise InvalidDataError("Firmoteka company batch is empty or unbounded")

        def fetch_company(item: dict[str, Any], lane: _EgressLane) -> dict[str, Any]:
            inn, url = str(item.get("inn") or ""), str(item.get("url") or "")
            if not is_valid_inn(inn) or _company_inn_from_url(url) != inn:
                raise InvalidDataError("Firmoteka company batch contains non-exact INN URL")
            request_started = time.monotonic()
            content, status, headers, retrieved_at = _fetch(
                url, not_before=None, egress_lane=lane
            )
            latency_ms = max(0, round((time.monotonic() - request_started) * 1000))
            artifact, manifest = _store_raw(
                raw_root=raw_root, content=content, kind="company", url=url,
                status=status, headers=headers, retrieved_at=retrieved_at,
                latency_ms=latency_ms,
            )
            projection = parse_firmoteka_page(
                _decoded_source_body(content),
                requested_inn=inn,
                url=url,
                fetched_at=retrieved_at,
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
            return {
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
                "latency_ms": latency_ms,
                "artifact": artifact,
            }

        company_outcomes, lane_request_state, last_request_at = _run_parallel_lanes(
            items,
            concurrency=concurrency,
            min_gap_seconds=min_gap_seconds,
            lane_not_before=lane_not_before,
            fetch_task=fetch_company,
        )
        company_failures = [
            row for row in company_outcomes if isinstance(row, _LaneFailure)
        ]
        projections = [
            row for row in company_outcomes if not isinstance(row, _LaneFailure)
        ]
        if company_failures and not projections:
            raise company_failures[0].error
        artifacts.extend(row.pop("artifact") for row in projections)
        rejected = sum(not row["accepted"] for row in projections)
        context.report_counters(
            ExecutionCounters(
                records_seen=len(items),
                records_written=len(items) - rejected,
                records_rejected=rejected,
            )
        )
        context.heartbeat()
        validation = {
            "phase": phase,
            "crawl_run_id": str(crawl_id),
            "projections": projections,
            "failed_company_items": [
                {
                    "id": int(failure.task["id"]),
                    "error_kind": type(failure.error).__name__,
                }
                for failure in company_failures
            ],
            "request_metrics": [
                {
                    "http_status": row["raw_manifest"]["http_status"],
                    "latency_ms": row["latency_ms"],
                }
                for row in projections
            ],
            "last_request_at": last_request_at.isoformat(),
            "lane_request_state": lane_request_state,
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
        checksum_metadata={
            "phase": phase,
            "crawl_run_id": str(crawl_id),
            "last_request_at": last_request_at.isoformat(),
            "lane_count": len(lane_request_state),
        },
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
    force_pending: bool = False,
) -> bool:
    if not is_valid_inn(inn) or _company_inn_from_url(url) != inn:
        return False
    already_known = session.scalar(
        select(FirmotekaCompanySnapshot.id).where(
            FirmotekaCompanySnapshot.inn == inn,
            FirmotekaCompanySnapshot.is_current.is_(True),
        ).limit(1)
    )
    initial_status = (
        "succeeded"
        if crawl.run_kind == "daily" and already_known and not force_pending
        else "pending"
    )
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


def _lane_not_before(crawl: FirmotekaCrawlRun, concurrency: int) -> dict[str, str]:
    state = dict(crawl.lane_request_state or {})
    result: dict[str, str] = {}
    for index in range(concurrency):
        key = f"lane-{index}"
        raw = state.get(key)
        if raw:
            try:
                last_started = _utc(datetime.fromisoformat(str(raw)))
            except ValueError as error:
                raise InvalidDataError("Firmoteka persisted lane clock is invalid") from error
            result[key] = (
                last_started + timedelta(seconds=crawl.request_delay_seconds)
            ).isoformat()
    return result


def _requeue_terminal_claims(session: Session, crawl: FirmotekaCrawlRun) -> None:
    terminal_jobs = select(WorkerJob.id).where(
        WorkerJob.status.in_(("failed", "cancelled"))
    )
    session.execute(
        update(FirmotekaCrawlItem)
        .where(
            FirmotekaCrawlItem.crawl_run_id == crawl.id,
            FirmotekaCrawlItem.status == "running",
            FirmotekaCrawlItem.claimed_by_job_id.in_(terminal_jobs),
        )
        .values(
            status="pending",
            attempt_count=FirmotekaCrawlItem.attempt_count + 1,
            claimed_by_job_id=None,
            claim_fencing_token=None,
            claimed_at=None,
        )
    )
    session.execute(
        update(FirmotekaCatalogPage)
        .where(
            FirmotekaCatalogPage.crawl_run_id == crawl.id,
            FirmotekaCatalogPage.status == "running",
            FirmotekaCatalogPage.claimed_by_job_id.in_(terminal_jobs),
        )
        .values(
            status="pending",
            attempt_count=FirmotekaCatalogPage.attempt_count + 1,
            claimed_by_job_id=None,
            claim_fencing_token=None,
            claimed_at=None,
        )
    )


def _claim_resume_suffix(checkpoints: tuple[Any, ...]) -> str:
    generations = tuple(
        (int(checkpoint.id), int(checkpoint.attempt_count))
        for checkpoint in checkpoints
    )
    if all(generation == 0 for _, generation in generations):
        return ""
    digest = sha256(
        ",".join(f"{checkpoint_id}.{generation}" for checkpoint_id, generation in generations).encode()
    ).hexdigest()[:16]
    return f":resume:{digest}"


def _seed_daily_refresh_items(
    session: Session,
    crawl: FirmotekaCrawlRun,
    *,
    now: datetime,
) -> int:
    cutoff = now - timedelta(days=crawl.daily_refresh_horizon_days)
    snapshots = tuple(
        session.scalars(
            select(FirmotekaCompanySnapshot)
            .where(
                FirmotekaCompanySnapshot.is_current.is_(True),
                FirmotekaCompanySnapshot.retrieved_at <= cutoff,
            )
            .order_by(
                FirmotekaCompanySnapshot.retrieved_at,
                FirmotekaCompanySnapshot.inn,
            )
            .limit(crawl.daily_refresh_budget)
        )
    )
    position = int(
        session.scalar(
            select(func.coalesce(func.max(FirmotekaCrawlItem.position), 0)).where(
                FirmotekaCrawlItem.crawl_run_id == crawl.id
            )
        )
        or 0
    )
    inserted = 0
    for snapshot in snapshots:
        position += 1
        inserted += int(
            _upsert_company_item(
                session,
                crawl,
                inn=snapshot.inn,
                url=snapshot.source_url,
                discovered_from="daily_refresh_horizon",
                position=position,
                force_pending=True,
            )
        )
    cursor = dict(crawl.cursor or {})
    cursor["daily_refresh_seeded"] = inserted
    cursor["daily_refresh_cutoff"] = cutoff.isoformat()
    crawl.cursor = cursor
    return inserted


def _enrichment_backlog(session: Session) -> int:
    active = select(CompanyEnrichmentRun.company_id.label("company_id")).where(
        CompanyEnrichmentRun.status.in_(
            ("pending", "waiting_sources", "retry_scheduled", "running")
        )
    )
    signalled = select(MasterReplaySignal.company_id.label("company_id")).where(
        MasterReplaySignal.status == "pending"
    )
    companies = active.union(signalled).subquery()
    return int(
        session.scalar(
            select(func.count()).select_from(companies)
        )
        or 0
    )


def _scaled_daily_budget(
    session: Session, *, horizon_days: int, configured_minimum: int
) -> int:
    known = int(
        session.scalar(
            select(func.count())
            .select_from(FirmotekaCompanySnapshot)
            .where(FirmotekaCompanySnapshot.is_current.is_(True))
        )
        or 0
    )
    rolling = (known + horizon_days - 1) // horizon_days
    return max(1, configured_minimum, rolling)


def _apply_scale_config(
    session: Session,
    crawl: FirmotekaCrawlRun,
    config: FirmotekaScaleConfig,
) -> None:
    """Apply operator scale settings at a durable job boundary only."""

    crawl.request_delay_seconds = config.min_request_gap_seconds
    crawl.catalog_concurrency = config.catalog_concurrency
    crawl.company_concurrency = config.company_concurrency
    crawl.concurrency = max(config.catalog_concurrency, config.company_concurrency)
    crawl.backpressure_threshold = config.backpressure_threshold
    crawl.daily_refresh_horizon_days = config.daily_refresh_horizon_days
    crawl.daily_refresh_budget = _scaled_daily_budget(
        session,
        horizon_days=config.daily_refresh_horizon_days,
        configured_minimum=config.daily_refresh_budget,
    )


def _create_next_job(
    session: Session,
    *,
    crawl: FirmotekaCrawlRun,
    raw_root: str,
    now: datetime,
) -> JobCreation | None:
    _requeue_terminal_claims(session, crawl)
    pending_items = int(
        session.scalar(
            select(func.count())
            .select_from(FirmotekaCrawlItem)
            .where(
                FirmotekaCrawlItem.crawl_run_id == crawl.id,
                FirmotekaCrawlItem.status == "pending",
            )
        )
        or 0
    )
    pending_pages = int(
        session.scalar(
            select(func.count())
            .select_from(FirmotekaCatalogPage)
            .where(
                FirmotekaCatalogPage.crawl_run_id == crawl.id,
                FirmotekaCatalogPage.status == "pending",
            )
        )
        or 0
    )
    enrichment_backlog = _enrichment_backlog(session)
    cursor = dict(crawl.cursor or {})
    cursor["pending_enrichment_backlog"] = enrichment_backlog
    cursor["company_intake_backpressured"] = (
        enrichment_backlog >= crawl.backpressure_threshold
    )
    crawl.cursor = cursor
    process_companies = (
        pending_items > 0
        and enrichment_backlog < crawl.backpressure_threshold
    )
    if process_companies:
        batch_limit = COMPANY_BATCH_SIZE * crawl.company_concurrency
        items = tuple(
            session.scalars(
                select(FirmotekaCrawlItem)
                .where(
                    FirmotekaCrawlItem.crawl_run_id == crawl.id,
                    FirmotekaCrawlItem.status == "pending",
                )
                .order_by(FirmotekaCrawlItem.position, FirmotekaCrawlItem.id)
                .limit(batch_limit)
                .with_for_update(skip_locked=True)
            )
        )
        if not items:
            return None
        crawl.phase = "companies"
        ids = "-".join(str(item.id) for item in items)
        resume_suffix = _claim_resume_suffix(items)
        creation = create_job(
            session,
            source_id=SOURCE_ID,
            job_type="firmoteka_company_batch",
            handler_version=HANDLER_VERSION,
            idempotency_key=(
                f"{SOURCE_ID}:{crawl.id}:companies:{ids}:{HANDLER_VERSION}"
                f"{resume_suffix}"
            ),
            schedule_metadata={
                "phase": "companies",
                "crawl_run_id": str(crawl.id),
                "raw_root": raw_root,
                "items": [{"id": item.id, "inn": item.inn, "url": item.source_url} for item in items],
                "request_delay_seconds": crawl.request_delay_seconds,
                "company_concurrency": crawl.company_concurrency,
                "lane_not_before": _lane_not_before(
                    crawl, crawl.company_concurrency
                ),
            },
            max_attempts=4,
            timeout_seconds=max(
                180,
                COMPANY_BATCH_SIZE * (crawl.request_delay_seconds + 50),
            ),
            now=now,
        )
        session.execute(
            update(FirmotekaCrawlItem)
            .where(FirmotekaCrawlItem.id.in_([item.id for item in items]))
            .values(
                status="running",
                claimed_by_job_id=creation.job.id,
                claim_fencing_token=None,
                claimed_at=now,
            )
        )
        return creation
    if pending_pages:
        pages = tuple(
            session.scalars(
                select(FirmotekaCatalogPage)
                .where(
                    FirmotekaCatalogPage.crawl_run_id == crawl.id,
                    FirmotekaCatalogPage.status == "pending",
                )
                .order_by(FirmotekaCatalogPage.position, FirmotekaCatalogPage.id)
                .with_for_update(skip_locked=True)
                .limit(crawl.catalog_concurrency)
            )
        )
        if not pages:
            return None
        crawl.phase = "catalog"
        page_ids = "-".join(str(page.id) for page in pages)
        resume_suffix = _claim_resume_suffix(pages)
        creation = create_job(
            session,
            source_id=SOURCE_ID,
            job_type="firmoteka_catalog_page",
            handler_version=HANDLER_VERSION,
            idempotency_key=(
                f"{SOURCE_ID}:{crawl.id}:catalog:{page_ids}:{HANDLER_VERSION}"
                f"{resume_suffix}"
            ),
            schedule_metadata={
                "phase": "catalog",
                "crawl_run_id": str(crawl.id),
                "catalog_pages": [
                    {"id": page.id, "url": page.url} for page in pages
                ],
                "raw_root": raw_root,
                "request_delay_seconds": crawl.request_delay_seconds,
                "catalog_concurrency": crawl.catalog_concurrency,
                "lane_not_before": _lane_not_before(
                    crawl, crawl.catalog_concurrency
                ),
            },
            max_attempts=4,
            timeout_seconds=180,
            now=now,
        )
        session.execute(
            update(FirmotekaCatalogPage)
            .where(FirmotekaCatalogPage.id.in_([page.id for page in pages]))
            .values(
                status="running",
                claimed_by_job_id=creation.job.id,
                claim_fencing_token=None,
                claimed_at=now,
            )
        )
        return creation
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
            "catalog_concurrency": crawl.catalog_concurrency,
            "company_concurrency": crawl.company_concurrency,
            "backpressure_threshold": crawl.backpressure_threshold,
            "daily_refresh_horizon_days": crawl.daily_refresh_horizon_days,
            "daily_refresh_budget": crawl.daily_refresh_budget,
            "daily_refresh_seeded": int(
                (crawl.cursor or {}).get("daily_refresh_seeded") or 0
            ),
            "http_status_counts": dict(crawl.http_status_counts or {}),
            "request_latency_ms_average": (
                round(crawl.latency_ms_total / crawl.request_count, 2)
                if crawl.request_count
                else None
            ),
            "request_latency_ms_max": crawl.latency_ms_max,
            "request_latency_histogram": dict(crawl.latency_histogram or {}),
            "active_lane_count": len(crawl.lane_request_state or {}),
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


def _record_request_metrics(
    crawl: FirmotekaCrawlRun, metrics: list[dict[str, Any]]
) -> None:
    counts = dict(crawl.http_status_counts or {})
    histogram = dict(crawl.latency_histogram or {})
    for metric in metrics:
        status_key = str(int(metric["http_status"]))
        counts[status_key] = int(counts.get(status_key) or 0) + 1
        latency_ms = max(0, int(metric["latency_ms"]))
        crawl.latency_ms_total += latency_ms
        crawl.latency_ms_max = max(crawl.latency_ms_max, latency_ms)
        bucket = next(
            (
                label
                for ceiling, label in (
                    (250, "le_250"),
                    (500, "le_500"),
                    (1000, "le_1000"),
                    (2500, "le_2500"),
                    (5000, "le_5000"),
                )
                if latency_ms <= ceiling
            ),
            "gt_5000",
        )
        histogram[bucket] = int(histogram.get(bucket) or 0) + 1
    crawl.http_status_counts = counts
    crawl.latency_histogram = histogram


def _run_benchmark_metrics(
    validation: Mapping[str, Any], result: HandlerResult
) -> dict[str, Any]:
    metrics = list(validation.get("request_metrics") or ())
    latencies = sorted(max(0, int(item["latency_ms"])) for item in metrics)
    statuses: dict[str, int] = {}
    for item in metrics:
        key = str(int(item["http_status"]))
        statuses[key] = statuses.get(key, 0) + 1

    def percentile(fraction: float) -> int | None:
        if not latencies:
            return None
        rank = int(fraction * len(latencies) + 0.999999)
        return latencies[max(0, min(len(latencies) - 1, rank - 1))]

    return {
        # ``lane_count`` is handler execution evidence.  It is intentionally
        # stored in checksum metadata (not staging validation), so preserve it
        # when the publisher adds the durable benchmark summary.
        "concurrency": int((result.checksum_metadata or {}).get("lane_count") or 1),
        "requests": len(metrics),
        "http_status_counts": statuses,
        "latency_ms_p50": percentile(0.50),
        "latency_ms_p95": percentile(0.95),
        "latency_ms_max": max(latencies) if latencies else None,
        "raw_bytes": sum(
            int(artifact.manifest.get("response_size") or 0)
            for artifact in result.raw_artifacts
        ),
        "parser_rejected": result.counters.records_rejected,
    }


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
    lane_state = dict(crawl.lane_request_state or {})
    lane_state.update(dict(validation.get("lane_request_state") or {}))
    crawl.lane_request_state = lane_state
    crawl.last_checkpoint_at = now
    crawl.request_count += len(result.raw_artifacts)
    crawl.success_count += len(result.raw_artifacts)
    _record_request_metrics(crawl, list(validation.get("request_metrics") or ()))
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
        doc_position = int(session.scalar(select(func.coalesce(func.max(FirmotekaCatalogPage.position), 0)).where(FirmotekaCatalogPage.crawl_run_id == crawl.id)) or 0)
        item_position = int(session.scalar(select(func.coalesce(func.max(FirmotekaCrawlItem.position), 0)).where(FirmotekaCrawlItem.crawl_run_id == crawl.id)) or 0)
        page_results = list(validation.get("catalog_pages") or ())
        if not page_results and validation.get("catalog_page_id"):
            page_results = [validation]
        for page_result in page_results:
            page = session.get(
                FirmotekaCatalogPage,
                int(page_result["catalog_page_id"]),
                with_for_update=True,
            )
            if (
                page is None
                or page.crawl_run_id != crawl.id
                or page.claimed_by_job_id not in (None, claim.job_id)
            ):
                raise InvalidDataError("Firmoteka catalog checkpoint differs from job")
            page.status = "succeeded"
            page.claimed_by_job_id = claim.job_id
            page.claim_fencing_token = claim.fencing_token
            page.attempt_count += 1
            page.checked_at = now
            page.discovered_companies = len(page_result["companies"])
            page.last_error = None
            for url in page_result["documents"]:
                doc_position += 1
                crawl.catalog_discovered += int(
                    _upsert_document(session, crawl, url, position=doc_position)
                )
            for item in page_result["companies"]:
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
        for failed in validation.get("failed_catalog_pages") or ():
            page = session.get(
                FirmotekaCatalogPage, int(failed["id"]), with_for_update=True
            )
            if (
                page is None
                or page.crawl_run_id != crawl.id
                or page.claimed_by_job_id != claim.job_id
            ):
                raise InvalidDataError("Firmoteka failed catalog checkpoint differs")
            page.status = "pending"
            page.claimed_by_job_id = None
            page.claim_fencing_token = None
            page.claimed_at = None
            page.attempt_count += 1
            page.last_error = str(failed.get("error_kind") or "request_failed")[:120]
            crawl.failure_count += 1
            crawl.retry_count += 1

    else:
        new_master = changed_master = published = rejected = legal = ip = 0
        for row in validation["projections"]:
            item = session.get(FirmotekaCrawlItem, int(row["item_id"]), with_for_update=True)
            if (
                item is None
                or item.crawl_run_id != crawl.id
                or item.inn != row["inn"]
                or item.claimed_by_job_id not in (None, claim.job_id)
            ):
                raise InvalidDataError("Firmoteka company checkpoint differs from job")
            item.claimed_by_job_id = claim.job_id
            item.claim_fencing_token = claim.fencing_token
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
        for failed in validation.get("failed_company_items") or ():
            item = session.get(
                FirmotekaCrawlItem, int(failed["id"]), with_for_update=True
            )
            if (
                item is None
                or item.crawl_run_id != crawl.id
                or item.claimed_by_job_id != claim.job_id
            ):
                raise InvalidDataError("Firmoteka failed company checkpoint differs")
            item.status = "pending"
            item.claimed_by_job_id = None
            item.claim_fencing_token = None
            item.claimed_at = None
            item.attempt_count += 1
            item.last_error = str(failed.get("error_kind") or "request_failed")[:120]
            crawl.failure_count += 1
            crawl.retry_count += 1
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
    # A continuation is created atomically by the publisher, so the scheduler
    # never observes an idle gap in an active crawl.  Re-read scale settings at
    # this completed-job boundary to let the controlled 1→2→4 ladder advance
    # without cancelling a queued checkpoint or restarting the crawl.
    _apply_scale_config(session, crawl, FirmotekaScaleConfig.from_environment())
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
    checksum_metadata = dict(result.checksum_metadata or {})
    checksum_metadata["benchmark"] = _run_benchmark_metrics(validation, result)
    return replace(
        result,
        checksum_metadata=checksum_metadata,
        change_summary=summary,
    )


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
            "configurable_parallel_lanes": True,
            "egress_pool_secrets_persisted": False,
            "content_addressed_raw": True,
        },
    )


def schedule_firmoteka_check(
    session: Session,
    *,
    raw_root: Path,
    now: datetime | None = None,
    scale_config: FirmotekaScaleConfig | None = None,
) -> JobCreation:
    now = now or utc_now()
    config = scale_config or FirmotekaScaleConfig.from_environment()
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
        daily_budget = _scaled_daily_budget(
            session,
            horizon_days=config.daily_refresh_horizon_days,
            configured_minimum=config.daily_refresh_budget,
        )
        crawl = FirmotekaCrawlRun(
            run_kind="daily" if prior else "initial",
            status="running",
            phase="discovery",
            cursor={"discovery": "robots_and_sitemap"},
            request_delay_seconds=config.min_request_gap_seconds,
            concurrency=max(
                config.catalog_concurrency, config.company_concurrency
            ),
            catalog_concurrency=config.catalog_concurrency,
            company_concurrency=config.company_concurrency,
            backpressure_threshold=config.backpressure_threshold,
            daily_refresh_horizon_days=config.daily_refresh_horizon_days,
            daily_refresh_budget=daily_budget,
            lane_request_state={},
            started_at=now,
        )
        session.add(crawl)
        session.flush()
        if prior is not None:
            _seed_daily_refresh_items(session, crawl, now=now)
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
    # Apply changes only between jobs.  Persisted checkpoint claims remain
    # valid while the controlled 1→2→4 ladder can continue on this crawl.
    _apply_scale_config(session, crawl, config)
    if crawl.phase != "discovery":
        continuation = _create_next_job(session, crawl=crawl, raw_root=str(Path(raw_root).resolve()), now=now)
        if continuation is None:
            raise InvalidDataError("Firmoteka crawl has no runnable checkpoint")
        return continuation
    lane_not_before = _lane_not_before(crawl, 1)
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
            "request_delay_seconds": crawl.request_delay_seconds,
            "catalog_concurrency": 1,
            "lane_not_before": lane_not_before,
        },
        max_attempts=4,
        timeout_seconds=180,
        now=now,
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
