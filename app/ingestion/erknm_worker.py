"""Worker Foundation adapter for official monthly ERKNM inspection data.

ERKNM publishes one metadata passport per calendar month.  Each passport may
receive several immutable revisions.  The worker follows the current month's
latest 248-FZ revision, preserves older events, and upserts the current
revision by ERPID in one publication transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urlparse
from zipfile import BadZipFile, ZipFile

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.ingestion.erknm import iter_erknm_records, select_xml_member
from app.models.company import Company
from app.models.erknm import ErknmInspection
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
from app.worker.errors import (
    HandlerNotRegisteredError,
    InvalidDataError,
    SchemaMismatchError,
    WorkerNetworkError,
)
from app.worker.execution import JobCreation, create_job, register_handler
from app.worker.registry import HandlerRegistry
from scripts.sync_erknm_open_data import build_metadata_url, parse_erknm_metadata


SOURCE_ID = "erknm_inspections"
DATASET_CODE = SOURCE_ID
HANDLER_VERSION = "erknm-monthly-official-v1"
OFFICIAL_HOST = "proverki.gov.ru"
OFFICIAL_PATH_PREFIX = "/blob/erknm-opendata/"
CHECK_INTERVAL = timedelta(days=1)
HTTP_TIMEOUT_SECONDS = 120.0
PUBLISH_BATCH_SIZE = 1000


ERKNM_FACT_FIELDS = (
    "data_date",
    "period_year",
    "period_month",
    "erpid",
    "classification",
    "creation_source",
    "status",
    "status_key",
    "control_level",
    "supervision_name",
    "start_date",
    "end_date",
    "prosecutor_office",
    "kind_control",
    "kind_knm",
    "kno_organization",
    "subject_inn",
    "subject_ogrn",
    "subject_name",
    "subject_type",
    "subject_guid",
    "msp_code",
    "place",
    "object_address",
    "object_type",
    "object_kind",
    "object_sub_kind",
    "risk_category",
    "reason_text",
    "warning_caption",
    "result_text",
    "inspection_attributes",
    "subject_attributes",
    "okveds",
    "objects",
    "inspectors",
    "reasons",
)
DATE_FIELDS = frozenset({"data_date", "start_date", "end_date"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must contain a timezone")
    return value.astimezone(timezone.utc)


def _official_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != OFFICIAL_HOST
        or not parsed.path.startswith(OFFICIAL_PATH_PREFIX)
    ):
        raise InvalidDataError(f"unapproved ERKNM URL: {value}")
    return value


def _hash_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


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
            raise InvalidDataError(f"immutable ERKNM RAW member differs: {path}")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _persist_temp(temp_path: Path, target: Path, *, checksum: str) -> None:
    if target.exists():
        actual, _size = _hash_file(target)
        temp_path.unlink(missing_ok=True)
        if actual != checksum:
            raise InvalidDataError(f"immutable ERKNM artifact differs: {target}")
        return
    try:
        os.link(temp_path, target)
        target.chmod(0o444)
    except FileExistsError:
        actual, _size = _hash_file(target)
        if actual != checksum:
            raise InvalidDataError(f"immutable ERKNM artifact differs: {target}")
    finally:
        temp_path.unlink(missing_ok=True)


def _file_path(reference: str) -> Path:
    parsed = urlparse(reference)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise InvalidDataError("accepted ERKNM snapshot must use a local file URI")
    path = Path(unquote(parsed.path))
    if not path.is_absolute() or not path.is_file():
        raise InvalidDataError("accepted ERKNM snapshot is unavailable")
    return path


def _fetch_bytes(url: str) -> tuple[bytes, Mapping[str, str], int]:
    _official_url(url)
    try:
        with httpx.Client(
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "next.company-erknm-worker/1.0"},
        ) as client:
            response = client.get(url)
    except (httpx.TimeoutException, httpx.RequestError) as error:
        raise WorkerNetworkError(f"ERKNM request failed: {type(error).__name__}") from error
    if response.status_code >= 500 or response.status_code in {408, 429}:
        raise WorkerNetworkError(f"ERKNM request returned HTTP {response.status_code}")
    if response.status_code != 200:
        raise InvalidDataError(f"ERKNM request returned HTTP {response.status_code}")
    content = bytes(response.content)
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as error:
            raise SchemaMismatchError("ERKNM HTTP content-length is invalid") from error
        if declared_size != len(content):
            raise SchemaMismatchError("ERKNM response length differs from HTTP metadata")
    return content, dict(response.headers), response.status_code


def _fetch_to_temp(url: str, root: Path) -> tuple[Path, str, int, Mapping[str, str], int]:
    """Stream a potentially large official archive to worker-local storage."""

    _official_url(url)
    root.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix="erknm-download-", dir=root)
    os.close(handle)
    path = Path(name)
    digest = sha256()
    size = 0
    try:
        with httpx.Client(
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "next.company-erknm-worker/1.0"},
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 500 or response.status_code in {408, 429}:
                    raise WorkerNetworkError(
                        f"ERKNM request returned HTTP {response.status_code}"
                    )
                if response.status_code != 200:
                    raise InvalidDataError(
                        f"ERKNM request returned HTTP {response.status_code}"
                    )
                headers = dict(response.headers)
                with path.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
        declared = headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError as error:
                raise SchemaMismatchError(
                    "ERKNM HTTP content-length is invalid"
                ) from error
            if declared_size != size:
                raise SchemaMismatchError(
                    "ERKNM response length differs from HTTP metadata"
                )
        return path, digest.hexdigest(), size, headers, 200
    except (WorkerNetworkError, InvalidDataError, SchemaMismatchError):
        path.unlink(missing_ok=True)
        raise
    except (httpx.TimeoutException, httpx.RequestError, OSError) as error:
        path.unlink(missing_ok=True)
        raise WorkerNetworkError(
            f"ERKNM request failed: {type(error).__name__}"
        ) from error


@dataclass(frozen=True)
class ErknmRelease:
    metadata_url: str
    metadata_sha256: str
    source_url: str
    xsd_url: str
    source_created: date
    period_year: int
    period_month: int
    provenance: str
    discovered_at: datetime

    @property
    def actual_until(self) -> date:
        # The passport has no dc:valid field.  A successful live passport
        # observation proves freshness only through that UTC calendar date.
        return self.discovered_at.date()

    @property
    def identity(self) -> str:
        return sha256(
            "\n".join(
                (
                    self.metadata_url,
                    self.metadata_sha256,
                    self.source_url,
                    self.xsd_url,
                    self.source_created.isoformat(),
                    f"{self.period_year:04d}-{self.period_month:02d}",
                )
            ).encode("utf-8")
        ).hexdigest()

    def as_metadata(self) -> dict[str, Any]:
        return {
            "metadata_url": self.metadata_url,
            "metadata_sha256": self.metadata_sha256,
            "source_url": self.source_url,
            "xsd_url": self.xsd_url,
            "source_created": self.source_created.isoformat(),
            "source_data_date": self.source_created.isoformat(),
            "period_year": self.period_year,
            "period_month": self.period_month,
            "provenance": self.provenance,
            "discovered_at": self.discovered_at.isoformat(),
            "actual_until": self.actual_until.isoformat(),
            "release_identity": self.identity,
        }


def _release_from_metadata(metadata: Mapping[str, Any]) -> ErknmRelease:
    required = (
        "metadata_url",
        "metadata_sha256",
        "source_url",
        "xsd_url",
        "source_created",
        "period_year",
        "period_month",
        "provenance",
        "discovered_at",
        "release_identity",
    )
    missing = [name for name in required if not str(metadata.get(name) or "").strip()]
    if missing:
        raise InvalidDataError("ERKNM release metadata missing: " + ", ".join(missing))
    release = ErknmRelease(
        metadata_url=_official_url(str(metadata["metadata_url"])),
        metadata_sha256=str(metadata["metadata_sha256"]),
        source_url=_official_url(str(metadata["source_url"])),
        xsd_url=_official_url(str(metadata["xsd_url"])),
        source_created=date.fromisoformat(str(metadata["source_created"])),
        period_year=int(metadata["period_year"]),
        period_month=int(metadata["period_month"]),
        provenance=str(metadata["provenance"]),
        discovered_at=_utc(datetime.fromisoformat(str(metadata["discovered_at"]))),
    )
    if release.identity != metadata["release_identity"]:
        raise InvalidDataError("ERKNM release identity differs from job metadata")
    return release


def discover_erknm_release(
    *,
    now: datetime | None = None,
    fetch: Callable[[str], tuple[bytes, Mapping[str, str], int]] = _fetch_bytes,
) -> ErknmRelease:
    now = _utc(now or utc_now())
    metadata_url = build_metadata_url(now.year, now.month)
    raw, _headers, _status = fetch(metadata_url)
    try:
        parsed = parse_erknm_metadata(raw)
        created_text = str(parsed["latest"]["created"])
        source_created = datetime.strptime(created_text, "%Y%m%d").date()
    except (KeyError, TypeError, ValueError) as error:
        raise SchemaMismatchError(f"ERKNM metadata rejected: {error}") from error
    source_url = _official_url(str(parsed["latest"]["source_url"]))
    xsd_url = _official_url(str(parsed["structure_url"]))
    return ErknmRelease(
        metadata_url=metadata_url,
        metadata_sha256=_hash_bytes(raw),
        source_url=source_url,
        xsd_url=xsd_url,
        source_created=source_created,
        period_year=now.year,
        period_month=now.month,
        provenance=str(parsed["latest"].get("provenance") or ""),
        discovered_at=now,
    )


def _validate_xml(zip_path: Path, xsd_path: Path) -> str:
    from lxml import etree

    try:
        schema = etree.XMLSchema(etree.parse(str(xsd_path)))
        with ZipFile(zip_path) as archive:
            broken = archive.testzip()
            if broken:
                raise SchemaMismatchError(f"ERKNM ZIP CRC failed: {broken}")
            member = select_xml_member(archive)
            with archive.open(member) as stream:
                for _event, element in etree.iterparse(
                    stream,
                    events=("end",),
                    schema=schema,
                    resolve_entities=False,
                    no_network=True,
                ):
                    element.clear()
            return member
    except SchemaMismatchError:
        raise
    except (BadZipFile, etree.XMLSyntaxError, etree.XMLSchemaParseError, etree.DocumentInvalid, OSError) as error:
        raise SchemaMismatchError(f"official ERKNM XML/XSD validation failed: {error}") from error


def _normalize_release(
    zip_path: Path,
    *,
    xsd_path: Path,
    release: ErknmRelease,
) -> tuple[Path, str, dict[str, int | str]]:
    selected_member = _validate_xml(zip_path, xsd_path)
    handle, database_name = tempfile.mkstemp(
        prefix="erknm-normalize-", suffix=".sqlite", dir=zip_path.parent
    )
    os.close(handle)
    out_handle, output_name = tempfile.mkstemp(
        prefix="normalized-", suffix=".jsonl.tmp", dir=zip_path.parent
    )
    os.close(out_handle)
    database = Path(database_name)
    output = Path(output_name)
    connection = sqlite3.connect(database)
    seen = duplicates = with_inn = with_ogrn = without_identifiers = 0
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("CREATE TABLE records (erpid TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        for record in iter_erknm_records(
            zip_path,
            data_date=release.source_created,
            period_year=release.period_year,
            period_month=release.period_month,
            member=selected_member,
        ):
            seen += 1
            with_inn += int(bool(record.get("subject_inn")))
            with_ogrn += int(bool(record.get("subject_ogrn")))
            without_identifiers += int(
                not record.get("subject_inn") and not record.get("subject_ogrn")
            )
            payload = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                default=lambda value: value.isoformat()
                if isinstance(value, (date, datetime))
                else str(value),
            )
            cursor = connection.execute(
                "INSERT OR IGNORE INTO records (erpid, payload) VALUES (?, ?)",
                (str(record["erpid"]), payload),
            )
            if cursor.rowcount == 0:
                duplicates += 1
                connection.execute(
                    "UPDATE records SET payload = ? WHERE erpid = ?",
                    (payload, str(record["erpid"])),
                )
        with output.open("w", encoding="utf-8") as target:
            for (payload,) in connection.execute(
                "SELECT payload FROM records ORDER BY erpid"
            ):
                target.write(payload + "\n")
        unique = int(connection.execute("SELECT count(*) FROM records").fetchone()[0])
        checksum, _size = _hash_file(output)
        target = zip_path.parent / f"normalized-{checksum}.jsonl"
        _persist_temp(output, target, checksum=checksum)
        return target, checksum, {
            "records_seen": seen,
            "records_unique": unique,
            "records_duplicated": duplicates,
            "with_inn": with_inn,
            "with_ogrn": with_ogrn,
            "without_identifiers": without_identifiers,
            "selected_member": selected_member,
        }
    finally:
        connection.close()
        database.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def _download_release(
    release: ErknmRelease,
    *,
    raw_root: Path,
    fetch: Callable[[str], tuple[bytes, Mapping[str, str], int]] = _fetch_bytes,
) -> tuple[Path, Path, dict[str, Any]]:
    raw_root = raw_root.resolve()
    raw_root.mkdir(parents=True, exist_ok=True)
    metadata_content, metadata_headers, metadata_status = fetch(release.metadata_url)
    if _hash_bytes(metadata_content) != release.metadata_sha256:
        raise SchemaMismatchError("ERKNM metadata changed after scheduling")
    xsd_content, xsd_headers, xsd_status = fetch(release.xsd_url)
    xsd_checksum = _hash_bytes(xsd_content)
    zip_temp = None
    if fetch is _fetch_bytes:
        zip_temp, zip_checksum, zip_size, zip_headers, zip_status = _fetch_to_temp(
            release.source_url, raw_root
        )
        zip_content = None
    else:
        zip_content, zip_headers, zip_status = fetch(release.source_url)
        zip_checksum = _hash_bytes(zip_content)
        zip_size = len(zip_content)
    artifact_dir = raw_root / SOURCE_ID / f"{release.identity}-{zip_checksum}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    zip_path = artifact_dir / Path(urlparse(release.source_url).path).name
    xsd_path = artifact_dir / Path(urlparse(release.xsd_url).path).name
    metadata_path = artifact_dir / Path(urlparse(release.metadata_url).path).name
    if zip_temp is not None:
        _persist_temp(zip_temp, zip_path, checksum=zip_checksum)
    else:
        _write_once(zip_path, zip_content)
    _write_once(xsd_path, xsd_content)
    _write_once(metadata_path, metadata_content)
    stable_manifest = {
        "manifest_version": 1,
        "source_id": SOURCE_ID,
        "dataset_code": DATASET_CODE,
        **release.as_metadata(),
        "artifact_reference": zip_path.as_uri(),
        "artifact_sha256": zip_checksum,
        "artifact_size": zip_size,
        "xsd_reference": xsd_path.as_uri(),
        "xsd_sha256": xsd_checksum,
        "xsd_size": len(xsd_content),
        "metadata_reference": metadata_path.as_uri(),
        "metadata_size": len(metadata_content),
        "immutable": True,
    }
    _write_once(
        artifact_dir / "manifest.json",
        (json.dumps(stable_manifest, ensure_ascii=False, sort_keys=True) + "\n").encode(),
    )
    observation = {
        **stable_manifest,
        "http": {
            "metadata": {"status": metadata_status, "headers": dict(metadata_headers)},
            "artifact": {"status": zip_status, "headers": dict(zip_headers)},
            "xsd": {"status": xsd_status, "headers": dict(xsd_headers)},
        },
    }
    return zip_path, xsd_path, observation


def run_erknm_handler(
    context: HandlerContext,
    *,
    fetch: Callable[[str], tuple[bytes, Mapping[str, str], int]] = _fetch_bytes,
) -> HandlerResult:
    release = _release_from_metadata(context.schedule_metadata)
    if context.schedule_metadata.get("check_only"):
        return HandlerResult(
            checksum_metadata={
                "release_identity": release.identity,
                "check_only": True,
            },
            counters=ExecutionCounters(),
        )
    raw_value = str(context.schedule_metadata.get("raw_root") or "").strip()
    if not raw_value:
        raise InvalidDataError("raw_root is required")
    context.ensure_active(now=utc_now())
    zip_path, xsd_path, manifest = _download_release(
        release, raw_root=Path(raw_value), fetch=fetch
    )
    context.heartbeat()
    normalized_path, normalized_checksum, coverage = _normalize_release(
        zip_path, xsd_path=xsd_path, release=release
    )
    counters = ExecutionCounters(
        records_seen=int(coverage["records_seen"]),
        records_written=int(coverage["records_unique"]),
        records_duplicated=int(coverage["records_duplicated"]),
    )
    context.report_counters(counters)
    context.heartbeat()
    validation = {
        "release_identity": release.identity,
        "source_data_date": release.source_created.isoformat(),
        "official_actual_until": release.actual_until.isoformat(),
        "period_year": release.period_year,
        "period_month": release.period_month,
        **coverage,
    }
    return HandlerResult(
        raw_artifacts=(
            RawArtifactReference(
                artifact_reference=zip_path.as_uri(),
                checksum=str(manifest["artifact_sha256"]),
                manifest=manifest,
            ),
        ),
        staging_result=StagingResult(
            staging_pointer=normalized_path.as_uri(),
            checksum=normalized_checksum,
            validation=ValidationResult(accepted=True, metadata=validation),
            metadata={
                "source_id": SOURCE_ID,
                "dataset_code": DATASET_CODE,
                "api_projection": "erknm_check",
                "card_projection": "company_card.erknm",
                "publication_mode": "historical_event_upsert",
            },
        ),
        checksum_metadata={
            "release_identity": release.identity,
            "artifact_sha256": manifest["artifact_sha256"],
            "xsd_sha256": manifest["xsd_sha256"],
            "normalized_sha256": normalized_checksum,
        },
        counters=counters,
    )


def erknm_worker_handler(context: HandlerContext) -> HandlerResult:
    return run_erknm_handler(context)


def _iter_jsonl(path: Path) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                for field in DATE_FIELDS:
                    if row.get(field):
                        row[field] = date.fromisoformat(row[field])
                batch.append(row)
            if len(batch) >= PUBLISH_BATCH_SIZE:
                yield batch
                batch = []
    if batch:
        yield batch


def _same_fact(row: ErknmInspection, candidate: Mapping[str, Any]) -> bool:
    return all(getattr(row, field) == candidate.get(field) for field in ERKNM_FACT_FIELDS)


def _upsert_event_batch(
    session: Session, *, dataset_id: int, rows: list[dict[str, Any]]
) -> tuple[int, int, int]:
    existing = {
        row.erpid: row
        for row in session.scalars(
            select(ErknmInspection).where(
                ErknmInspection.dataset_id == dataset_id,
                ErknmInspection.erpid.in_([str(item["erpid"]) for item in rows]),
            )
        )
    }
    new = changed = unchanged = 0
    for item in rows:
        previous = existing.get(str(item["erpid"]))
        if previous is None:
            new += 1
        elif _same_fact(previous, item):
            unchanged += 1
        else:
            changed += 1
    statement = pg_insert(ErknmInspection).values(
        [{"dataset_id": dataset_id, **item} for item in rows]
    )
    session.execute(
        statement.on_conflict_do_update(
            constraint="uq_erknm_inspections_dataset_erpid",
            set_={
                **{
                    field: getattr(statement.excluded, field)
                    for field in ERKNM_FACT_FIELDS
                    if field != "erpid"
                },
                "updated_at": func.now(),
            },
        )
    )
    return new, changed, unchanged


def _matched_companies(session: Session, *, dataset_id: int) -> int:
    condition = or_(
        Company.inn == ErknmInspection.subject_inn,
        and_(
            ErknmInspection.subject_inn.is_(None),
            Company.ogrn == ErknmInspection.subject_ogrn,
        ),
    )
    return int(
        session.scalar(
            select(func.count(func.distinct(Company.id)))
            .select_from(ErknmInspection)
            .join(Company, condition)
            .where(ErknmInspection.dataset_id == dataset_id)
        )
        or 0
    )


def _apply_success(
    dataset: DataSet,
    *,
    now: datetime,
    actual_until: date,
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


def publish_erknm_worker_result(
    session: Session, claim: Any, result: HandlerResult
) -> HandlerResult:
    release = _release_from_metadata(claim.schedule_metadata)
    dataset = session.scalar(
        select(DataSet).where(DataSet.code == DATASET_CODE).with_for_update()
    )
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {DATASET_CODE}")
    now = utc_now()
    previous_date = dataset.last_data_date
    check_only = bool(claim.schedule_metadata.get("check_only"))
    new = changed = unchanged = 0
    source_records = 0
    if check_only:
        state = session.get(WorkerPublicationState, SOURCE_ID)
        if state is None or not state.active_pointer:
            raise InvalidDataError("ERKNM check-only has no accepted publication")
        validation = (state.validation_metadata or {}).get("validation") or {}
        if validation.get("release_identity") != release.identity:
            raise InvalidDataError("ERKNM accepted release identity differs")
        source_records = int(validation.get("records_unique") or 0)
        unchanged = source_records
    else:
        if result.staging_result is None:
            raise InvalidDataError("ERKNM publisher requires normalized staging")
        validation = dict(result.staging_result.validation.metadata)
        if validation.get("release_identity") != release.identity:
            raise InvalidDataError("ERKNM staged release identity differs")
        staging_path = _file_path(result.staging_result.staging_pointer)
        actual_checksum, _size = _hash_file(staging_path)
        if actual_checksum != result.staging_result.checksum:
            raise InvalidDataError("ERKNM normalized checksum differs")
        for batch in _iter_jsonl(staging_path):
            batch_new, batch_changed, batch_unchanged = _upsert_event_batch(
                session, dataset_id=dataset.id, rows=batch
            )
            new += batch_new
            changed += batch_changed
            unchanged += batch_unchanged
        source_records = int(validation["records_unique"])
        if new + changed + unchanged != source_records:
            raise InvalidDataError("ERKNM publication count differs from staging")

    total_facts = int(
        session.scalar(
            select(func.count())
            .select_from(ErknmInspection)
            .where(ErknmInspection.dataset_id == dataset.id)
        )
        or 0
    )
    matched = _matched_companies(session, dataset_id=dataset.id)
    summary = SourceChangeSummary(
        matched_companies=matched,
        new_facts=new,
        changed_facts=changed,
        removed_or_expired_facts=0,
        unchanged_facts=unchanged,
        replayed_facts=0,
        quarantined_records=0,
        source_records=source_records,
        source_data_date=release.source_created,
        previous_source_data_date=previous_date,
        unavailable_reasons=(
            {"previous_source_data_date": "first successful publication has no previous source date"}
            if previous_date is None
            else {}
        ),
    )
    if not check_only:
        dataset.enabled = True
        dataset.auto_update_status = AutoUpdateStatus.CONFIGURED
        dataset.last_success_at = now
        dataset.last_data_date = release.source_created
        dataset.source_as_of = datetime.combine(
            release.source_created, datetime.min.time(), tzinfo=timezone.utc
        )
        dataset.retrieved_at = now
        dataset.published_at = now
    dataset.record_count = total_facts
    dataset.coverage = {
        "periods": [
            f"{year:04d}-{month:02d}"
            for year, month in session.execute(
                select(
                    ErknmInspection.period_year,
                    ErknmInspection.period_month,
                )
                .where(ErknmInspection.dataset_id == dataset.id)
                .distinct()
                .order_by(
                    ErknmInspection.period_year,
                    ErknmInspection.period_month,
                )
            )
        ],
        "source_records": source_records,
        "published_facts": total_facts,
        "matched_companies": matched,
        "matching_method": "inn_exact_ogrn_fallback_only_when_source_inn_missing",
        "release_identity": release.identity,
        "api_projection": "erknm_check",
        "card_projection": "company_card.erknm",
        "change_summary": summary.as_dict(),
    }
    _apply_success(dataset, now=now, actual_until=release.actual_until)
    counters = ExecutionCounters(
        records_seen=source_records,
        records_written=new + changed,
        records_duplicated=(result.counters.records_duplicated if result.counters else 0),
        records_published=new + changed,
    )
    if check_only:
        return replace(
            result,
            staging_result=None,
            checksum_metadata={
                **result.checksum_metadata,
                "freshness": dataset.operational_status.value,
                "official_actual_until": release.actual_until.isoformat(),
            },
            counters=counters,
            change_summary=summary,
        )
    updated_validation = replace(
        result.staging_result.validation,
        metadata={
            **result.staging_result.validation.metadata,
            "published_facts": total_facts,
            "matched_companies": matched,
            "freshness": dataset.operational_status.value,
        },
    )
    return replace(
        result,
        staging_result=replace(
            result.staging_result, validation=updated_validation
        ),
        counters=counters,
        change_summary=summary,
    )


def register_erknm_worker(session: Session, registry: HandlerRegistry) -> Any:
    return register_handler(
        session,
        registry,
        source_id=SOURCE_ID,
        version=HANDLER_VERSION,
        handler=erknm_worker_handler,
        publisher=publish_erknm_worker_result,
        approved=True,
        live=False,
        fixture=False,
        metadata={
            "mode": "official_monthly_events",
            "metadata_url_template": (
                "https://proverki.gov.ru/blob/erknm-opendata/"
                "7710146102-inspection-{year}-{month}.xml"
            ),
            "matching_method": "inn_exact_ogrn_fallback_only_when_source_inn_missing",
        },
    )


def schedule_erknm_check(
    session: Session,
    *,
    raw_root: Path,
    now: datetime | None = None,
    fetch: Callable[[str], tuple[bytes, Mapping[str, str], int]] = _fetch_bytes,
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
    release = discover_erknm_release(now=now, fetch=fetch)
    state = session.get(WorkerPublicationState, SOURCE_ID)
    validation = dict(state.validation_metadata or {}) if state else {}
    current_identity = (validation.get("validation") or {}).get("release_identity")
    check_only = current_identity == release.identity
    action = f"check:{now.date().isoformat()}" if check_only else "release"
    return create_job(
        session,
        source_id=SOURCE_ID,
        job_type=f"{SOURCE_ID}_{'check' if check_only else 'release'}",
        handler_version=HANDLER_VERSION,
        idempotency_key=f"{SOURCE_ID}:{action}:{release.identity}:{HANDLER_VERSION}",
        schedule_metadata={
            **release.as_metadata(),
            "raw_root": str(Path(raw_root).resolve()),
            "check_only": check_only,
            "check_frequency": "daily",
            "publication_frequency": "monthly_revisioned_events",
        },
        max_attempts=3,
        timeout_seconds=7200,
        now=now,
    )
