"""Shared Worker Foundation adapter for official FNS bulk XML releases.

This module does not introduce a second job system.  It adapts the existing
``app.worker`` lease/job/run/publisher contracts to the two legacy FNS
importers.  Tax offences and revenue/expenses keep separate source ids,
registrations, jobs, publication generations and readiness state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from html import unescape
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.models.company import Company
from app.models.revenue_expense import CompanyRevenueExpenseSnapshot
from app.models.source import DataSet
from app.models.tax_offence import CompanyTaxOffence
from app.models.worker import WorkerHandlerRegistration, WorkerPublicationState
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


OFFICIAL_HOSTS = frozenset({"www.nalog.gov.ru", "data.nalog.ru", "file.nalog.ru"})
CHECK_INTERVAL = timedelta(days=1)
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class FnsBulkSourceSpec:
    source_id: str
    dataset_code: str
    source_page_url: str
    source_path: str
    handler_version: str
    kind: str
    api_projection: str
    card_projection: str

    def __post_init__(self) -> None:
        if self.kind not in {"tax_offence", "revenue_expense"}:
            raise ValueError(f"unsupported FNS bulk source kind: {self.kind}")
        if not all(
            value.strip()
            for value in (
                self.source_id,
                self.dataset_code,
                self.source_page_url,
                self.source_path,
                self.handler_version,
            )
        ):
            raise ValueError("FNS bulk source specification contains an empty field")


@dataclass(frozen=True)
class FnsRelease:
    source_page_url: str
    artifact_url: str
    xsd_url: str
    source_data_date: date
    actual_until: date | None
    discovered_at: datetime
    provenance: str

    @property
    def identity(self) -> str:
        return sha256(
            "\n".join(
                (
                    self.artifact_url,
                    self.xsd_url,
                    self.source_data_date.isoformat(),
                )
            ).encode("utf-8")
        ).hexdigest()

    def as_metadata(self) -> dict[str, Any]:
        return {
            "source_page_url": self.source_page_url,
            "artifact_url": self.artifact_url,
            "xsd_url": self.xsd_url,
            "source_data_date": self.source_data_date.isoformat(),
            "actual_until": self.actual_until.isoformat() if self.actual_until else None,
            "discovered_at": self.discovered_at.isoformat(),
            "provenance": self.provenance,
            "release_identity": self.identity,
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must contain a timezone")
    return value.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def release_operational_status(
    actual_until: date | None,
    *,
    now: datetime,
) -> OperationalStatus:
    """Return fail-closed official-release freshness.

    The official ``actual_until`` date is inclusive.  A missing boundary
    cannot prove a clean negative; an expired boundary is stale regardless of
    how recently our scheduler checked the passport.
    """

    now = _utc(now)
    if actual_until is None:
        return OperationalStatus.UNAVAILABLE
    if now.date() > actual_until:
        return OperationalStatus.STALE
    return OperationalStatus.CURRENT


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    for pattern in (r"(\d{2}\.\d{2}\.\d{4})", r"(\d{4}-\d{2}-\d{2})"):
        match = re.search(pattern, text)
        if match:
            candidate = match.group(1)
            return datetime.strptime(
                candidate, "%d.%m.%Y" if "." in candidate else "%Y-%m-%d"
            ).date()
    year = re.search(r"\b(20\d{2})\b", text)
    return date(int(year.group(1)), 12, 31) if year else None


def _read_url(url: str, *, timeout_seconds: int = 60) -> tuple[bytes, Mapping[str, str]]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
        raise InvalidDataError(f"unapproved FNS URL: {url}")
    try:
        with urlopen(
            Request(url, headers={"User-Agent": "next.company-source-worker/1.0"}),
            timeout=timeout_seconds,
        ) as response:
            return response.read(), dict(response.headers.items())
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise WorkerNetworkError(f"FNS request failed for {url}: {error}") from error


def discover_fns_release(
    spec: FnsBulkSourceSpec,
    *,
    now: datetime | None = None,
    fetch: Callable[[str], tuple[bytes, Mapping[str, str]]] = _read_url,
) -> FnsRelease:
    """Read the official passport and return its current ZIP/XSD identity."""

    now = _utc(now or datetime.now(timezone.utc))
    raw, _headers = fetch(spec.source_page_url)
    html = unescape(raw.decode("utf-8", errors="replace"))
    urls = re.findall(r'href=["\'](https://[^"\']+)["\']', html, flags=re.I)
    prefix = f"/opendata/{spec.source_path}/"
    artifact_url = next(
        (url for url in urls if urlparse(url).path.startswith(prefix) and url.lower().endswith(".zip") and "structure-" in url),
        None,
    )
    xsd_url = next(
        (url for url in urls if urlparse(url).path.startswith(prefix) and url.lower().endswith(".xsd")),
        None,
    )
    if not artifact_url or not xsd_url:
        raise SchemaMismatchError("official FNS passport has no current ZIP/XSD links")

    provenance_match = re.search(
        r'property=["\']dc:provenance["\'][^>]*>(.*?)</td>', html, flags=re.I | re.S
    )
    provenance = re.sub(r"<[^>]+>", " ", provenance_match.group(1)).strip() if provenance_match else ""
    source_data_date = _parse_date(provenance)
    if source_data_date is None:
        raise SchemaMismatchError("official FNS passport has no source data date")

    valid_match = re.search(
        r'property=["\']dc:valid["\'][^>]*content=["\']([^"\']+)', html, flags=re.I
    )
    actual_until = _parse_date(valid_match.group(1)) if valid_match else None
    for candidate in (artifact_url, xsd_url):
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
            raise InvalidDataError(f"official passport points to unapproved URL: {candidate}")
    return FnsRelease(
        source_page_url=spec.source_page_url,
        artifact_url=artifact_url,
        xsd_url=xsd_url,
        source_data_date=source_data_date,
        actual_until=actual_until,
        discovered_at=now,
        provenance=provenance,
    )


def _hash_file(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(DOWNLOAD_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _download_temp(url: str, root: Path) -> tuple[Path, dict[str, str]]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
        raise InvalidDataError(f"unapproved FNS download URL: {url}")
    handle, name = tempfile.mkstemp(prefix="fns-download-", dir=root)
    os.close(handle)
    path = Path(name)
    try:
        with urlopen(
            Request(url, headers={"User-Agent": "next.company-source-worker/1.0"}),
            timeout=120,
        ) as response, path.open("wb") as output:
            headers = dict(response.headers.items())
            while chunk := response.read(DOWNLOAD_CHUNK_SIZE):
                output.write(chunk)
        return path, headers
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        path.unlink(missing_ok=True)
        raise WorkerNetworkError(f"FNS download failed for {url}: {error}") from error


def _write_once(path: Path, content: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        if path.read_bytes() != content:
            raise InvalidDataError(f"immutable RAW member differs: {path}")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _persist_download(temp: Path, target: Path, *, checksum: str) -> None:
    if target.exists():
        existing_checksum, _ = _hash_file(target)
        if existing_checksum != checksum:
            raise InvalidDataError(f"immutable RAW artifact differs: {target}")
        temp.unlink(missing_ok=True)
        return
    try:
        os.link(temp, target)
        target.chmod(0o444)
    except FileExistsError:
        existing_checksum, _ = _hash_file(target)
        if existing_checksum != checksum:
            raise InvalidDataError(f"immutable RAW artifact differs: {target}")
    finally:
        temp.unlink(missing_ok=True)


def stage_release(
    spec: FnsBulkSourceSpec,
    release: FnsRelease,
    *,
    raw_root: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    """Download ZIP/XSD and persist checksum-addressed, write-once RAW."""

    raw_root = raw_root.resolve()
    raw_root.mkdir(parents=True, exist_ok=True)
    prefix = f"/opendata/{spec.source_path}/"
    if any(
        not urlparse(candidate).path.startswith(prefix)
        for candidate in (release.artifact_url, release.xsd_url)
    ):
        raise InvalidDataError("FNS release path differs from source pin")
    zip_temp, zip_headers = _download_temp(release.artifact_url, raw_root)
    try:
        xsd_temp, xsd_headers = _download_temp(release.xsd_url, raw_root)
    except Exception:
        zip_temp.unlink(missing_ok=True)
        raise
    zip_checksum, zip_size = _hash_file(zip_temp)
    xsd_checksum, xsd_size = _hash_file(xsd_temp)
    advertised = next(
        (value for key, value in zip_headers.items() if key.lower() == "x-amz-meta-sha256"),
        None,
    )
    if advertised and advertised.lower() != zip_checksum:
        zip_temp.unlink(missing_ok=True)
        xsd_temp.unlink(missing_ok=True)
        raise InvalidDataError("download checksum differs from official response metadata")
    xsd_advertised = next(
        (value for key, value in xsd_headers.items() if key.lower() == "x-amz-meta-sha256"),
        None,
    )
    if xsd_advertised and xsd_advertised.lower() != xsd_checksum:
        zip_temp.unlink(missing_ok=True)
        xsd_temp.unlink(missing_ok=True)
        raise InvalidDataError("downloaded XSD checksum differs from official response metadata")
    for headers, size, label in (
        (zip_headers, zip_size, "ZIP"),
        (xsd_headers, xsd_size, "XSD"),
    ):
        declared = next(
            (value for key, value in headers.items() if key.lower() == "content-length"),
            None,
        )
        if declared is not None and int(declared) != size:
            zip_temp.unlink(missing_ok=True)
            xsd_temp.unlink(missing_ok=True)
            raise InvalidDataError(f"downloaded {label} length differs from HTTP metadata")

    artifact_dir = raw_root / spec.source_id / zip_checksum
    artifact_dir.mkdir(parents=True, exist_ok=True)
    zip_path = artifact_dir / Path(urlparse(release.artifact_url).path).name
    xsd_path = artifact_dir / Path(urlparse(release.xsd_url).path).name
    _persist_download(zip_temp, zip_path, checksum=zip_checksum)
    _persist_download(xsd_temp, xsd_path, checksum=xsd_checksum)
    manifest = {
        "manifest_version": 1,
        "source_id": spec.source_id,
        "dataset_code": spec.dataset_code,
        **release.as_metadata(),
        # Pinned discovery time keeps the manifest byte-identical when Worker
        # Foundation retries the same durable job.
        "retrieved_at": release.discovered_at.isoformat(),
        "artifact_reference": zip_path.as_uri(),
        "artifact_sha256": zip_checksum,
        "artifact_size": zip_size,
        "artifact_headers": {key.lower(): value for key, value in zip_headers.items() if key.lower() in {"etag", "last-modified", "content-length", "x-amz-meta-sha256"}},
        "xsd_sha256": xsd_checksum,
        "xsd_size": xsd_size,
        "xsd_reference": xsd_path.as_uri(),
        "xsd_headers": {key.lower(): value for key, value in xsd_headers.items() if key.lower() in {"etag", "last-modified", "content-length", "x-amz-meta-sha256"}},
        "immutable": True,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    _write_once(artifact_dir / "manifest.json", manifest_bytes)
    return zip_path, xsd_path, manifest


def _json_record(record: Mapping[str, Any]) -> str:
    return json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        default=lambda value: value.isoformat() if isinstance(value, (date, datetime)) else str(value),
    )


def normalize_release(
    zip_path: Path,
    *,
    xsd_path: Path,
    iterator: Callable[[Any], Iterable[Mapping[str, Any] | None]],
) -> tuple[Path, str, dict[str, Any]]:
    """Stream every XML member through the existing source parser into JSONL."""

    from lxml import etree

    try:
        schema = etree.XMLSchema(etree.parse(str(xsd_path)))
    except (etree.XMLSyntaxError, etree.XMLSchemaParseError, OSError) as error:
        raise SchemaMismatchError(f"official FNS XSD is invalid: {error}") from error

    descriptor, staging_name = tempfile.mkstemp(
        prefix="normalized-",
        suffix=".jsonl.tmp",
        dir=zip_path.parent,
    )
    os.close(descriptor)
    staging_temp = Path(staging_name)
    counters = {"records_seen": 0, "records_valid": 0, "records_rejected": 0, "xml_files": 0}
    data_dates: set[str] = set()
    try:
        with ZipFile(zip_path, "r") as archive, staging_temp.open("w", encoding="utf-8") as output:
            broken = archive.testzip()
            if broken:
                raise SchemaMismatchError(f"ZIP CRC failed: {broken}")
            members = [item for item in archive.infolist() if not item.is_dir() and item.filename.lower().endswith(".xml")]
            if not members:
                raise SchemaMismatchError("official ZIP contains no XML members")
            for member in members:
                counters["xml_files"] += 1
                # Validate the complete member before the legacy streaming parser
                # is allowed to emit normalized rows.  Re-opening a ZIP member is
                # deliberate: publication must never contain a partially
                # schema-validated file.
                try:
                    with archive.open(member, "r") as validation_stream:
                        for _event, element in etree.iterparse(
                            validation_stream,
                            events=("end",),
                            schema=schema,
                            resolve_entities=False,
                            no_network=True,
                        ):
                            element.clear()
                except (etree.XMLSyntaxError, etree.DocumentInvalid) as error:
                    raise SchemaMismatchError(
                        f"official XML member fails XSD validation: {member.filename}: {error}"
                    ) from error
                with archive.open(member, "r") as xml_stream:
                    for record in iterator(xml_stream):
                        counters["records_seen"] += 1
                        if record is None:
                            counters["records_rejected"] += 1
                            continue
                        counters["records_valid"] += 1
                        data_dates.add(str(record["data_date"]))
                        output.write(_json_record(record) + "\n")
    except SchemaMismatchError:
        staging_temp.unlink(missing_ok=True)
        raise
    except (BadZipFile, OSError) as error:
        staging_temp.unlink(missing_ok=True)
        raise SchemaMismatchError(f"cannot parse official FNS ZIP: {error}") from error
    except Exception:
        staging_temp.unlink(missing_ok=True)
        raise
    if counters["records_rejected"]:
        staging_temp.unlink(missing_ok=True)
        raise SchemaMismatchError(
            f"official FNS release contains {counters['records_rejected']} invalid documents"
        )
    if counters["records_valid"] == 0 or len(data_dates) != 1:
        staging_temp.unlink(missing_ok=True)
        raise SchemaMismatchError("official FNS release has empty or mixed-date normalized data")
    counters["source_data_date"] = next(iter(data_dates))
    normalized_checksum, _normalized_size = _hash_file(staging_temp)
    staging_path = zip_path.parent / f"normalized-{normalized_checksum}.jsonl"
    _persist_download(staging_temp, staging_path, checksum=normalized_checksum)
    return staging_path, normalized_checksum, counters


def release_from_metadata(metadata: Mapping[str, Any]) -> FnsRelease:
    required = ("source_page_url", "artifact_url", "xsd_url", "source_data_date", "discovered_at", "provenance")
    missing = [name for name in required if not str(metadata.get(name) or "").strip()]
    if missing:
        raise InvalidDataError("FNS release metadata missing: " + ", ".join(missing))
    return FnsRelease(
        source_page_url=str(metadata["source_page_url"]),
        artifact_url=str(metadata["artifact_url"]),
        xsd_url=str(metadata["xsd_url"]),
        source_data_date=date.fromisoformat(str(metadata["source_data_date"])),
        actual_until=date.fromisoformat(str(metadata["actual_until"])) if metadata.get("actual_until") else None,
        discovered_at=_utc(datetime.fromisoformat(str(metadata["discovered_at"]))),
        provenance=str(metadata["provenance"]),
    )


def run_bulk_handler(
    context: HandlerContext,
    *,
    spec: FnsBulkSourceSpec,
    iterator: Callable[[Any], Iterable[Mapping[str, Any] | None]],
) -> HandlerResult:
    metadata = context.schedule_metadata
    release = release_from_metadata(metadata)
    if release.source_page_url != spec.source_page_url:
        raise InvalidDataError("FNS source passport URL differs from handler pin")
    if metadata.get("check_only"):
        return HandlerResult(
            checksum_metadata={
                "release_identity": release.identity,
                "check_only": True,
                "replay_snapshot": bool(metadata.get("replay_snapshot")),
            },
            counters=ExecutionCounters(),
        )
    context.ensure_active(now=utc_now())
    raw_root = Path(str(metadata.get("raw_root") or "")).resolve()
    if not str(metadata.get("raw_root") or "").strip():
        raise InvalidDataError("raw_root is required")
    zip_path, xsd_path, manifest = stage_release(spec, release, raw_root=raw_root)
    context.heartbeat()
    staging_path, normalized_checksum, counters = normalize_release(
        zip_path,
        xsd_path=xsd_path,
        iterator=iterator,
    )
    parsed_data_date = date.fromisoformat(str(counters["source_data_date"]))
    if parsed_data_date != release.source_data_date:
        raise SchemaMismatchError("parsed source data date differs from official passport")
    context.ensure_active(now=utc_now())
    execution = ExecutionCounters(
        records_seen=int(counters["records_seen"]),
        records_written=int(counters["records_valid"]),
        records_rejected=int(counters["records_rejected"]),
    )
    context.report_counters(execution)
    return HandlerResult(
        raw_artifacts=(RawArtifactReference(
            artifact_reference=zip_path.as_uri(), checksum=manifest["artifact_sha256"], manifest=manifest
        ),),
        staging_result=StagingResult(
            staging_pointer=staging_path.as_uri(),
            checksum=normalized_checksum,
            validation=ValidationResult(accepted=True, metadata={
                "release_identity": release.identity,
                "source_data_date": release.source_data_date.isoformat(),
                "coverage": counters,
            }),
            metadata={
                "source_id": spec.source_id,
                "dataset_code": spec.dataset_code,
                "artifact_url": release.artifact_url,
                "xsd_url": release.xsd_url,
                "api_projection": spec.api_projection,
                "card_projection": spec.card_projection,
            },
        ),
        checksum_metadata={
            "artifact_sha256": manifest["artifact_sha256"],
            "xsd_sha256": manifest["xsd_sha256"],
            "normalized_sha256": normalized_checksum,
            "release_identity": release.identity,
        },
        counters=execution,
    )


def _iter_jsonl(path: Path, *, batch_size: int = 5000) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                batch.append(json.loads(line))
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch


def _file_path(reference: str) -> Path:
    parsed = urlparse(reference)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise InvalidDataError("accepted normalized snapshot must use a local file URI")
    path = Path(unquote(parsed.path))
    if not path.is_absolute() or not path.is_file():
        raise InvalidDataError("accepted normalized snapshot is unavailable")
    return path


def _claim_actual_until(claim: Any) -> date | None:
    raw = claim.schedule_metadata.get("actual_until")
    if raw in {None, ""}:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError as error:
        raise InvalidDataError("official actual_until is invalid") from error


def _apply_successful_check(
    dataset: DataSet,
    *,
    actual_until: date | None,
    now: datetime,
) -> OperationalStatus:
    status = release_operational_status(actual_until, now=now)
    dataset.checked_at = now
    dataset.official_actual_until = actual_until
    dataset.operational_status = status
    dataset.next_expected_update_at = now + CHECK_INTERVAL
    dataset.last_error = None
    dataset.last_error_at = None
    dataset.retry_count = 0
    dataset.next_retry_at = None
    return status


def _project_normalized_snapshot(
    session: Session,
    *,
    dataset: DataSet,
    spec: FnsBulkSourceSpec,
    staging_path: Path,
    replace_existing: bool,
) -> tuple[int, int, set[int], int]:
    """Exact-INN project a normalized snapshot using existing fact tables.

    A new release replaces its complete source snapshot atomically.  A
    same-release replay uses ``ON CONFLICT DO NOTHING`` so it only fills facts
    for Master companies added after the original publication.
    """

    matched = unmatched = changed = 0
    matched_companies: set[int] = set()
    model = CompanyTaxOffence if spec.kind == "tax_offence" else CompanyRevenueExpenseSnapshot
    if replace_existing:
        session.execute(delete(model).where(model.dataset_id == dataset.id))
    for batch in _iter_jsonl(staging_path):
        inns = {str(row["inn"]) for row in batch}
        company_ids = dict(
            session.execute(
                select(Company.inn, Company.id).where(Company.inn.in_(inns))
            ).all()
        )
        values: list[dict[str, Any]] = []
        for row in batch:
            company_id = company_ids.get(str(row["inn"]))
            if company_id is None:
                unmatched += 1
                continue
            matched += 1
            matched_companies.add(int(company_id))
            if spec.kind == "tax_offence":
                values.append({
                    "company_id": company_id,
                    "dataset_id": dataset.id,
                    "data_date": date.fromisoformat(row["data_date"]),
                    "document_date": date.fromisoformat(row["document_date"]) if row.get("document_date") else None,
                    "source_document_id": row["document_id"],
                    "fine_amount": Decimal(row["fine_amount"]),
                })
            else:
                values.append({
                    "company_id": company_id,
                    "dataset_id": dataset.id,
                    "data_date": date.fromisoformat(row["data_date"]),
                    "data_year": int(row["data_year"]),
                    "document_date": date.fromisoformat(row["document_date"]) if row.get("document_date") else None,
                    "source_document_id": row["document_id"],
                    "source_company_name": row.get("company_name"),
                    "revenue": Decimal(row["revenue"]),
                    "expenses": Decimal(row["expenses"]),
                    "profit_loss": Decimal(row["profit_loss"]),
                })
        if not values:
            continue
        if spec.kind == "tax_offence":
            values = list({str(value["source_document_id"]): value for value in values}.values())
            statement = pg_insert(CompanyTaxOffence).values(values)
            if replace_existing:
                statement = statement.on_conflict_do_update(
                    constraint="uq_company_tax_offence_dataset_document",
                    set_={
                        "company_id": statement.excluded.company_id,
                        "data_date": statement.excluded.data_date,
                        "document_date": statement.excluded.document_date,
                        "fine_amount": statement.excluded.fine_amount,
                        "updated_at": func.now(),
                    },
                )
            else:
                statement = statement.on_conflict_do_nothing(
                    constraint="uq_company_tax_offence_dataset_document"
                )
        else:
            values = list({
                (int(value["company_id"]), value["data_date"]): value for value in values
            }.values())
            statement = pg_insert(CompanyRevenueExpenseSnapshot).values(values)
            if replace_existing:
                statement = statement.on_conflict_do_update(
                    constraint="uq_company_revexp_company_dataset_date",
                    set_={
                        "data_year": statement.excluded.data_year,
                        "document_date": statement.excluded.document_date,
                        "source_document_id": statement.excluded.source_document_id,
                        "source_company_name": statement.excluded.source_company_name,
                        "revenue": statement.excluded.revenue,
                        "expenses": statement.excluded.expenses,
                        "profit_loss": statement.excluded.profit_loss,
                        "updated_at": func.now(),
                    },
                )
            else:
                statement = statement.on_conflict_do_nothing(
                    constraint="uq_company_revexp_company_dataset_date"
                )
        changed += len(session.scalars(statement.returning(model.id)).all())
    return matched, unmatched, matched_companies, changed


def _accepted_replay_path(
    session: Session,
    *,
    claim: Any,
    spec: FnsBulkSourceSpec,
) -> Path:
    pointer = str(claim.schedule_metadata.get("replay_pointer") or "")
    expected_checksum = str(claim.schedule_metadata.get("replay_checksum") or "")
    state = session.scalar(
        select(WorkerPublicationState)
        .where(WorkerPublicationState.source_id == spec.source_id)
        .with_for_update()
    )
    if state is None or state.active_pointer != pointer:
        raise InvalidDataError("accepted normalized replay pointer changed")
    validation = dict(state.validation_metadata or {})
    release_identity = (validation.get("validation") or {}).get("release_identity")
    if release_identity != claim.schedule_metadata.get("release_identity"):
        raise InvalidDataError("accepted normalized replay release changed")
    if not expected_checksum or validation.get("checksum") != expected_checksum:
        raise InvalidDataError("accepted normalized replay checksum changed")
    path = _file_path(pointer)
    actual_checksum, _size = _hash_file(path)
    if actual_checksum != expected_checksum:
        raise InvalidDataError("accepted normalized replay checksum mismatch")
    return path


def publish_bulk_result(
    session: Session,
    claim: Any,
    result: HandlerResult,
    *,
    spec: FnsBulkSourceSpec,
) -> HandlerResult:
    """Exact-INN match and atomic domain publication in the worker transaction."""

    dataset = session.scalar(select(DataSet).where(DataSet.code == spec.dataset_code).with_for_update())
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {spec.dataset_code}")
    now = utc_now()
    actual_until = _claim_actual_until(claim)
    if claim.schedule_metadata.get("check_only"):
        matched = unmatched = inserted = 0
        matched_companies: set[int] = set()
        if claim.schedule_metadata.get("replay_snapshot"):
            replay_path = _accepted_replay_path(session, claim=claim, spec=spec)
            matched, unmatched, matched_companies, inserted = _project_normalized_snapshot(
                session,
                dataset=dataset,
                spec=spec,
                staging_path=replay_path,
                replace_existing=False,
            )
            model = CompanyTaxOffence if spec.kind == "tax_offence" else CompanyRevenueExpenseSnapshot
            dataset.record_count = int(
                session.scalar(
                    select(func.count()).select_from(model).where(model.dataset_id == dataset.id)
                )
                or 0
            )
            coverage = dict(dataset.coverage or {})
            coverage["last_replay"] = {
                "checked_at": now.isoformat(),
                "matched": matched,
                "unmatched": unmatched,
                "new_facts": inserted,
                "candidate_companies": len(matched_companies),
            }
            coverage["published_facts"] = dataset.record_count
            dataset.coverage = coverage
        status = _apply_successful_check(
            dataset,
            actual_until=actual_until,
            now=now,
        )
        return replace(
            result,
            staging_result=None,
            checksum_metadata={
                **result.checksum_metadata,
                "freshness": status.value,
                "official_actual_until": actual_until.isoformat() if actual_until else None,
            },
            counters=ExecutionCounters(
                records_seen=matched + unmatched,
                records_written=inserted,
                records_published=inserted,
            ),
        )
    if result.staging_result is None:
        raise InvalidDataError("FNS publisher requires normalized staging")
    staging_path = _file_path(result.staging_result.staging_pointer)
    model = CompanyTaxOffence if spec.kind == "tax_offence" else CompanyRevenueExpenseSnapshot
    # A release is a complete official snapshot.  Delete + rebuild occurs in
    # this same publisher transaction; on any failure SQLAlchemy rollback keeps
    # the previous successful facts and WorkerPublicationState pointer intact.
    matched, unmatched, matched_companies, _changed = _project_normalized_snapshot(
        session,
        dataset=dataset,
        spec=spec,
        staging_path=staging_path,
        replace_existing=True,
    )

    published = int(
        session.scalar(
            select(func.count()).select_from(model).where(model.dataset_id == dataset.id)
        )
        or 0
    )

    source_date = date.fromisoformat(str(claim.schedule_metadata["source_data_date"]))
    source_as_of = datetime.combine(source_date, datetime.min.time(), tzinfo=timezone.utc)
    dataset.enabled = True
    dataset.auto_update_status = AutoUpdateStatus.CONFIGURED
    dataset.last_success_at = now
    dataset.last_data_date = source_date
    dataset.source_as_of = source_as_of
    dataset.retrieved_at = now
    dataset.published_at = now
    dataset.record_count = published
    dataset.coverage = {
        "source_records": result.counters.records_seen if result.counters else 0,
        "matched": matched,
        "unmatched": unmatched,
        "published_facts": published,
        "risk_summary_candidate_companies": len(matched_companies),
        "api_projection": spec.api_projection,
        "card_projection": spec.card_projection,
        "release_identity": claim.schedule_metadata.get("release_identity"),
    }
    status = _apply_successful_check(
        dataset,
        actual_until=actual_until,
        now=now,
    )
    validation = replace(
        result.staging_result.validation,
        metadata={
            **result.staging_result.validation.metadata,
            "matched": matched,
            "unmatched": unmatched,
            "published_facts": published,
            "risk_summary_candidate_companies": len(matched_companies),
            "api_projection": spec.api_projection,
            "card_projection": spec.card_projection,
            "freshness": status.value,
            "official_actual_until": actual_until.isoformat() if actual_until else None,
        },
    )
    return replace(
        result,
        staging_result=replace(result.staging_result, validation=validation),
        counters=ExecutionCounters(
            records_seen=result.counters.records_seen if result.counters else matched + unmatched,
            records_written=result.counters.records_written if result.counters else matched + unmatched,
            records_rejected=result.counters.records_rejected if result.counters else 0,
            records_duplicated=result.counters.records_duplicated if result.counters else 0,
            records_published=published,
        ),
    )


def register_bulk_handler(
    session: Session,
    registry: HandlerRegistry,
    *,
    spec: FnsBulkSourceSpec,
    handler: Callable[[HandlerContext], HandlerResult],
    publisher: Callable[[Session, Any, HandlerResult], HandlerResult],
) -> Any:
    return register_handler(
        session,
        registry,
        source_id=spec.source_id,
        version=spec.handler_version,
        handler=handler,
        publisher=publisher,
        approved=True,
        live=False,
        fixture=False,
        metadata={
            "mode": "official_bulk_release",
            "source_page_url": spec.source_page_url,
            "separate_operational_source": True,
        },
    )


def enqueue_bulk_release(
    session: Session,
    *,
    spec: FnsBulkSourceSpec,
    release: FnsRelease,
    raw_root: Path,
    check_only: bool = False,
    replay_pointer: str | None = None,
    replay_checksum: str | None = None,
    scheduled_for: date | None = None,
    max_attempts: int = 3,
    timeout_seconds: int = 7200,
) -> JobCreation:
    approval = session.get(WorkerHandlerRegistration, (spec.source_id, spec.handler_version))
    if approval is None or not approval.approved or not approval.enabled or approval.live_mode:
        raise HandlerNotRegisteredError(
            f"durable handler approval is missing: {spec.source_id}@{spec.handler_version}"
        )
    check_key = (scheduled_for or release.discovered_at.date()).isoformat()
    action = f"check:{check_key}" if check_only else "release"
    return create_job(
        session,
        source_id=spec.source_id,
        job_type=f"{spec.source_id}_{'check' if check_only else 'release'}",
        handler_version=spec.handler_version,
        idempotency_key=f"{spec.source_id}:{action}:{release.identity}:{spec.handler_version}",
        schedule_metadata={
            **release.as_metadata(),
            "raw_root": str(raw_root.resolve()),
            "check_only": check_only,
            "replay_snapshot": bool(replay_pointer),
            "replay_pointer": replay_pointer,
            "replay_checksum": replay_checksum,
            "check_frequency": "daily",
            "publication_frequency": "official_release",
        },
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
    )


def schedule_source_check(
    session: Session,
    *,
    spec: FnsBulkSourceSpec,
    raw_root: Path,
    now: datetime | None = None,
) -> JobCreation:
    now = _utc(now or utc_now())
    release = discover_fns_release(spec, now=now)
    state = session.get(WorkerPublicationState, spec.source_id)
    validation = dict(state.validation_metadata or {}) if state else {}
    current_identity = (validation.get("validation") or {}).get("release_identity")
    same_release = current_identity == release.identity
    return enqueue_bulk_release(
        session,
        spec=spec,
        release=release,
        raw_root=raw_root,
        check_only=same_release,
        replay_pointer=state.active_pointer if same_release and state else None,
        replay_checksum=str(validation.get("checksum") or "") if same_release else None,
        scheduled_for=now.date(),
    )
