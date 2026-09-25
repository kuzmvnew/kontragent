"""Official Mintrans transport-forwarder registry Worker Foundation adapter."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from html import unescape
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from zipfile import is_zipfile

from openpyxl import load_workbook
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.ingestion.mintrans_ted_registry import build_registry_listing_fact, is_valid_inn, is_valid_ogrn, resolve_exact_identity
from app.models.company import Company
from app.models.mintrans_ted import MINTRANS_TED_FACT_CODE, MintransTedEntry, MintransTedQuarantineRow, MintransTedRawArtifact, TransportForwardingRegistryListing
from app.models.source import DataSet
from app.models.worker import WorkerHandlerRegistration, WorkerPublicationState
from app.worker.contracts import ExecutionCounters, HandlerContext, HandlerResult, RawArtifactReference, SourceChangeSummary, StagingResult, ValidationResult
from app.worker.errors import HandlerNotRegisteredError, InvalidDataError, SchemaMismatchError, WorkerNetworkError
from app.worker.execution import JobCreation, create_job, register_handler
from app.worker.registry import HandlerRegistry


SOURCE_ID = DATASET_CODE = "mintrans_ted_registry"
HANDLER_VERSION = "mintrans-ted-official-v1"
SEARCH_URL = "https://www.mintrans.gov.ru/search"
CHECK_INTERVAL = timedelta(days=1)
BATCH_SIZE = 1000
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RUSSIAN_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


@dataclass(frozen=True)
class MintransRelease:
    artifact_url: str
    artifact_id: str
    filename: str
    source_data_date: date

    @property
    def identity(self) -> str:
        return f"{self.artifact_id}:{self.source_data_date.isoformat()}:{self.filename}"

    def as_metadata(self) -> dict[str, Any]:
        return {
            "artifact_url": self.artifact_url, "artifact_id": self.artifact_id,
            "filename": self.filename, "source_data_date": self.source_data_date.isoformat(),
            "release_identity": self.identity,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MintransOfficialProvider:
    def _fetch(self, url: str) -> tuple[bytes, dict[str, str]]:
        request = Request(url, headers={"User-Agent": "next.company-source-worker/1.0"})
        try:
            with urlopen(request, timeout=90) as response:
                return response.read(), {key.lower(): value for key, value in response.headers.items()}
        except HTTPError as error:
            raise WorkerNetworkError(f"Mintrans official endpoint HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            raise WorkerNetworkError("Mintrans official endpoint request failed") from error

    def discover(self, *, max_pages: int = 20) -> MintransRelease:
        candidates: list[MintransRelease] = []
        for page in range(1, max_pages + 1):
            query = urlencode({
                "page_search2": page,
                "search_type": 2,
                "check_name": 1,
                "value": "транспортно-экспедиционной деятельности",
            })
            content, _headers = self._fetch(f"{SEARCH_URL}?{query}")
            html = content.decode("utf-8", errors="replace")
            page_candidates = 0
            for block in re.split(
                r'class=["\']document-list-item["\']', html, flags=re.I
            )[1:]:
                match = re.search(
                    r'href=["\']([^"\']*/file/(\d+)[^"\']*)["\']',
                    block,
                    re.I,
                )
                if match is None:
                    continue
                context = unescape(
                    re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block))
                ).strip()
                lowered = context.lower()
                if "транспортно-экспедицион" not in lowered and "реестр тэд" not in lowered:
                    continue
                date_text = re.search(
                    r'class=["\']date-span["\'][^>]*>\s*([^<]+)',
                    block,
                    re.I,
                )
                source_date = _official_result_date(
                    date_text.group(1) if date_text is not None else context
                )
                if source_date is None:
                    continue
                url = urljoin(SEARCH_URL, match.group(1))
                candidates.append(MintransRelease(
                    artifact_url=url, artifact_id=match.group(2),
                    filename=f"Реестр ТЭД {source_date.strftime('%d.%m.%Y')}.xlsx",
                    source_data_date=source_date,
                ))
                page_candidates += 1
            if page > 1 and page_candidates == 0:
                break
        if not candidates:
            raise SchemaMismatchError("official Mintrans search returned no TED XLSX release")
        return max(candidates, key=lambda item: (item.source_data_date, int(item.artifact_id)))

    def download(self, release: MintransRelease, target: Path) -> dict[str, str]:
        content, headers = self._fetch(release.artifact_url)
        content_type = headers.get("content-type", "").lower()
        if "spreadsheet" not in content_type and "octet-stream" not in content_type:
            raise SchemaMismatchError("Mintrans artifact content type is not XLSX")
        target.write_bytes(content)
        return headers


def _official_result_date(value: str) -> date | None:
    numeric = re.search(
        r"(?<!\d)(\d{1,2})[.](\d{1,2})[.](20\d{2})(?!\d)", value
    )
    if numeric is not None:
        day, month, year = numeric.groups()
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            return None
    words = re.search(
        r"(?<!\d)(\d{1,2})\s+([A-Za-zА-Яа-яЁё]+)\s+(20\d{2})(?!\d)",
        value,
    )
    if words is None:
        return None
    day, month_name, year = words.groups()
    month = RUSSIAN_MONTHS.get(month_name.casefold())
    if month is None:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    result = str(value).strip().replace("\u00a0", "")
    if result.startswith("'"):
        result = result[1:]
    return result or None


def _included_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(value or "").strip(), pattern).date()
        except ValueError:
            pass
    return None


def _header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("ё", "е"))


def _resolve_columns(header: tuple[Any, ...]) -> dict[str, int]:
    aliases = {
        "registry_number": {"реестровый номер", "регистрационный номер", "номер реестровой записи"},
        "included_at": {"дата включения в реестр уведомлений тэд", "дата включения в реестр", "дата включения"},
        "legal_inn": {"инн юл"}, "legal_ogrn": {"огрн юл"},
        "ip_inn": {"инн ип"}, "ip_ogrn": {"огрн ип"},
        "generic_inn": {"инн"}, "generic_ogrn": {"огрн", "огрн/огрнип", "огрн / огрнип"},
    }
    resolved = {}
    for index, value in enumerate(header):
        key = _header(value)
        for canonical, choices in aliases.items():
            if key in choices:
                resolved[canonical] = index
    if "registry_number" not in resolved or "included_at" not in resolved:
        raise SchemaMismatchError("Mintrans XLSX misses registry number or inclusion date")
    if not ({"legal_inn", "ip_inn"} & resolved.keys()) and "generic_inn" not in resolved:
        raise SchemaMismatchError("Mintrans XLSX misses INN identity columns")
    return resolved


def _cell(values: tuple[Any, ...], columns: dict[str, int], name: str) -> Any:
    index = columns.get(name)
    return values[index] if index is not None and index < len(values) else None


def _normalize_row(values: tuple[Any, ...], columns: dict[str, int], row_number: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    legal_inn, ip_inn = _text(_cell(values, columns, "legal_inn")), _text(_cell(values, columns, "ip_inn"))
    legal_ogrn, ip_ogrn = _text(_cell(values, columns, "legal_ogrn")), _text(_cell(values, columns, "ip_ogrn"))
    inn = legal_inn or ip_inn or _text(_cell(values, columns, "generic_inn"))
    ogrn = legal_ogrn or ip_ogrn or _text(_cell(values, columns, "generic_ogrn"))
    registry_number = _text(_cell(values, columns, "registry_number"))
    included_at = _included_date(_cell(values, columns, "included_at"))
    reasons = []
    if inn is not None and not is_valid_inn(inn): reasons.append("invalid_inn")
    if ogrn is not None and not is_valid_ogrn(ogrn): reasons.append("invalid_ogrn")
    if inn is None and ogrn is None: reasons.append("missing_identity")
    if not registry_number: reasons.append("missing_registry_number")
    elif len(registry_number) > 200: reasons.append("invalid_registry_number")
    if included_at is None: reasons.append("invalid_included_at")
    safe = {"registry_number": registry_number, "included_at": str(_cell(values, columns, "included_at") or ""), "inn": inn, "ogrn": ogrn}
    if reasons:
        return None, {
            "source_row_number": row_number, "raw_hash": sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest(),
            "reason_codes": reasons, "raw_payload": safe,
        }
    canonical = {"inn": inn, "ogrn": ogrn, "registry_number": registry_number, "included_at": included_at.isoformat()}
    return {
        "source_row_number": row_number, "inn": inn, "ogrn": ogrn,
        "registry_number": registry_number, "included_at": included_at.isoformat(),
        "row_hash": sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest(),
        "validation_state": "valid",
    }, None


def normalize_mintrans_xlsx(path: Path, output: Path) -> dict[str, int | str]:
    if not is_zipfile(path):
        raise SchemaMismatchError("Mintrans artifact is not an XLSX container")
    workbook = load_workbook(path, read_only=True, data_only=True)
    source_records = normalized = rejected = duplicated = 0
    seen: set[str] = set()
    digest = sha256()
    try:
        rows = workbook.active.iter_rows(values_only=True)
        try:
            columns = _resolve_columns(tuple(next(rows)))
        except StopIteration as error:
            raise SchemaMismatchError("Mintrans XLSX is empty") from error
        with output.open("xb") as stream:
            for row_number, values in enumerate(rows, 2):
                if not any(value is not None and str(value).strip() for value in values):
                    continue
                source_records += 1
                record, invalid = _normalize_row(tuple(values), columns, row_number)
                item: dict[str, Any]
                if invalid is not None:
                    rejected += 1
                    item = {"kind": "quarantine", **invalid}
                elif record["row_hash"] in seen:
                    duplicated += 1
                    continue
                else:
                    seen.add(record["row_hash"])
                    normalized += 1
                    item = {"kind": "entry", **record}
                line = (json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n").encode()
                stream.write(line)
                digest.update(line)
    finally:
        workbook.close()
    return {"source_records": source_records, "normalized_records": normalized, "quarantined_records": rejected, "duplicate_records": duplicated, "normalized_sha256": digest.hexdigest()}


def _hash_file(path: Path) -> tuple[str, int]:
    digest, size = sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk); size += len(chunk)
    return digest.hexdigest(), size


def _find_existing(root: Path, release: MintransRelease) -> tuple[Path, dict[str, Any]] | None:
    source_root = root / SOURCE_ID
    if not source_root.is_dir():
        return None
    for manifest_path in source_root.glob("*/*manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        manifest_id = str(manifest.get("artifact_id") or manifest.get("file_id") or "")
        manifest_url = str(manifest.get("source_url") or manifest.get("artifact_url") or "")
        if manifest_id != release.artifact_id and not manifest_url.rstrip("/").endswith(f"/file/{release.artifact_id}"):
            continue
        artifact = manifest_path.parent / "artifact.xlsx"
        if artifact.is_file() and _hash_file(artifact)[0] == manifest.get("sha256"):
            return artifact, manifest
    return None


def mintrans_ted_worker_handler(context: HandlerContext, *, provider: MintransOfficialProvider | None = None) -> HandlerResult:
    metadata = context.schedule_metadata
    if metadata.get("check_only"):
        return HandlerResult(checksum_metadata={"check_only": True, "release_identity": metadata["release_identity"]}, counters=ExecutionCounters())
    release = MintransRelease(
        artifact_url=metadata["artifact_url"], artifact_id=str(metadata["artifact_id"]),
        filename=metadata["filename"], source_data_date=date.fromisoformat(metadata["source_data_date"]),
    )
    root = Path(str(metadata["raw_root"])).resolve()
    root.mkdir(parents=True, exist_ok=True)
    existing = _find_existing(root, release)
    if existing is None:
        descriptor, name = tempfile.mkstemp(prefix="mintrans-ted-", suffix=".xlsx", dir=root)
        os.close(descriptor)
        temp = Path(name)
        try:
            headers = (provider or MintransOfficialProvider()).download(release, temp)
            checksum, size = _hash_file(temp)
            artifact_dir = root / SOURCE_ID / checksum
            artifact_dir.mkdir(parents=True, exist_ok=True)
            artifact = artifact_dir / "artifact.xlsx"
            if not artifact.exists():
                os.link(temp, artifact); artifact.chmod(0o444)
            manifest = {
                "source_id": SOURCE_ID, "source_owner": "Минтранс России", "artifact_id": release.artifact_id,
                "source_url": release.artifact_url, "filename": release.filename,
                "source_data_date": release.source_data_date.isoformat(), "sha256": checksum, "size": size,
                "media_type": MEDIA_TYPE, "immutable": True,
                "content_type": headers.get("content-type"),
            }
            manifest_path = artifact_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n")
            manifest_path.chmod(0o444)
        finally:
            temp.unlink(missing_ok=True)
    else:
        artifact, manifest = existing
        checksum, size = _hash_file(artifact)
        manifest = {
            "source_id": SOURCE_ID,
            "source_owner": "Минтранс России",
            "artifact_id": release.artifact_id,
            "source_url": release.artifact_url,
            "filename": release.filename,
            "source_data_date": release.source_data_date.isoformat(),
            "sha256": checksum,
            "size": size,
            "media_type": MEDIA_TYPE,
            "immutable": True,
            **manifest,
        }
    if not is_zipfile(artifact):
        raise SchemaMismatchError("Mintrans official artifact is not XLSX")
    normalized_path = artifact.parent / "normalized.jsonl"
    descriptor, temp_name = tempfile.mkstemp(prefix="normalized-", suffix=".jsonl", dir=artifact.parent)
    os.close(descriptor)
    temp_normalized = Path(temp_name)
    temp_normalized.unlink()
    try:
        stats = normalize_mintrans_xlsx(artifact, temp_normalized)
        if normalized_path.exists():
            if _hash_file(normalized_path)[0] != stats["normalized_sha256"]:
                raise InvalidDataError("immutable Mintrans normalized snapshot differs")
        else:
            os.link(temp_normalized, normalized_path)
            normalized_path.chmod(0o444)
    finally:
        temp_normalized.unlink(missing_ok=True)
    counters = ExecutionCounters(
        records_seen=int(stats["source_records"]), records_written=int(stats["normalized_records"]),
        records_rejected=int(stats["quarantined_records"]), records_duplicated=int(stats["duplicate_records"]),
    )
    context.report_counters(counters)
    context.heartbeat()
    observation = {**manifest, "retrieved_at": utc_now().isoformat()}
    return HandlerResult(
        raw_artifacts=(RawArtifactReference(artifact.as_uri(), checksum, observation),),
        staging_result=StagingResult(
            normalized_path.as_uri(), ValidationResult(accepted=True, metadata={
                **stats, "release_identity": release.identity, "source_data_date": release.source_data_date.isoformat(),
                "artifact_sha256": checksum, "artifact_size": size,
                "artifact_reference": artifact.as_uri(), "manifest": manifest,
            }), checksum=str(stats["normalized_sha256"]),
        ), checksum_metadata={"artifact_sha256": checksum, "normalized_sha256": stats["normalized_sha256"]}, counters=counters,
    )


def _iter_jsonl(uri: str) -> Iterator[dict[str, Any]]:
    path = Path(urlparse(uri).path)
    if not path.is_file(): raise InvalidDataError("Mintrans normalized snapshot is unavailable")
    with path.open() as stream:
        for line in stream:
            if line.strip(): yield json.loads(line)


def _artifact_row(session: Session, dataset: DataSet, validation: dict[str, Any]) -> MintransTedRawArtifact:
    checksum = validation["artifact_sha256"]
    row = session.scalar(select(MintransTedRawArtifact).where(MintransTedRawArtifact.dataset_id == dataset.id, MintransTedRawArtifact.sha256 == checksum))
    manifest = validation["manifest"]
    if row is None:
        row = MintransTedRawArtifact(
            dataset_id=dataset.id, sha256=checksum, original_file_name=manifest["filename"],
            stored_path=str(Path(urlparse(validation["artifact_reference"]).path)),
            media_type=MEDIA_TYPE, size_bytes=validation["artifact_size"], manifest=manifest,
            source_metadata={"source_owner": "Минтранс России", "live_ingestion": True, "source_url": manifest["source_url"]},
        )
        # stored_path is overwritten with the actual RAW reference by the caller metadata below.
        session.add(row); session.flush()
    return row


def _maps(session: Session, rows: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    inns = {row["inn"] for row in rows if row.get("inn")}
    ogrns = {row["ogrn"] for row in rows if row.get("ogrn")}
    by_inn = dict(session.execute(select(Company.inn, Company.id).where(Company.inn.in_(inns))).all()) if inns else {}
    by_ogrn = dict(session.execute(select(Company.ogrn, Company.id).where(Company.ogrn.in_(ogrns))).all()) if ogrns else {}
    return by_inn, by_ogrn


def _insert_facts(session: Session, entries: list[tuple[MintransTedEntry, dict[str, Any], Any]], *, dataset: DataSet, checksum: str, observed_at: datetime) -> int:
    count = 0
    for entry, row, match in entries:
        if match.state != "matched": continue
        value, evidence = build_registry_listing_fact({**row, "included_at": date.fromisoformat(row["included_at"])}, match_method=match.method, artifact_sha256=checksum)
        session.add(TransportForwardingRegistryListing(
            company_id=match.company_id, dataset_id=dataset.id, source_entry_id=entry.id,
            fact_code=MINTRANS_TED_FACT_CODE, value=value, evidence=evidence,
            effective_from=date.fromisoformat(row["included_at"]), observed_at=observed_at,
        )); count += 1
    return count


def replay_mintrans_matches(session: Session, *, dataset: DataSet, checksum: str, observed_at: datetime) -> tuple[int, int]:
    candidates = session.execute(
        select(MintransTedEntry, Company).join(
            Company, or_(Company.inn == MintransTedEntry.inn, Company.ogrn == MintransTedEntry.ogrn)
        ).where(MintransTedEntry.dataset_id == dataset.id, MintransTedEntry.company_id.is_(None))
    ).all()
    grouped: dict[int, tuple[MintransTedEntry, list[Company]]] = {}
    for entry, company in candidates:
        grouped.setdefault(entry.id, (entry, []))[1].append(company)
    replayed = conflicts = 0
    for entry, companies in grouped.values():
        inn_matches = {item.id: item for item in companies if entry.inn and item.inn == entry.inn}
        ogrn_matches = {item.id: item for item in companies if entry.ogrn and item.ogrn == entry.ogrn}
        ids = set(inn_matches) | set(ogrn_matches)
        if len(ids) != 1:
            entry.match_state = "conflict_identity"; entry.match_method = None; conflicts += 1; continue
        company = (inn_matches or ogrn_matches)[next(iter(ids))]
        method = "inn_exact" if company.id in inn_matches else "ogrn_exact"
        entry.company_id = company.id; entry.match_state = "matched"; entry.match_method = method
        row = {"inn": entry.inn, "ogrn": entry.ogrn, "registry_number": entry.registry_number, "included_at": entry.included_at, "row_hash": entry.row_hash}
        value, evidence = build_registry_listing_fact(row, match_method=method, artifact_sha256=checksum)
        session.add(TransportForwardingRegistryListing(
            company_id=company.id, dataset_id=dataset.id, source_entry_id=entry.id,
            fact_code=MINTRANS_TED_FACT_CODE, value=value, evidence=evidence,
            effective_from=entry.included_at, observed_at=observed_at,
        )); replayed += 1
    return replayed, conflicts


def publish_mintrans_ted_result(session: Session, claim: Any, result: HandlerResult) -> HandlerResult:
    dataset = session.scalar(select(DataSet).where(DataSet.code == DATASET_CODE).with_for_update())
    if dataset is None: raise InvalidDataError("Mintrans dataset is not registered")
    state = session.scalar(select(WorkerPublicationState).where(WorkerPublicationState.source_id == SOURCE_ID).with_for_update())
    previous_identity = (((state.validation_metadata or {}).get("validation") or {}).get("release_identity") if state else None)
    release_identity = claim.schedule_metadata["release_identity"]
    now = utc_now()
    if result.staging_result is None or previous_identity == release_identity:
        checksum = str(
            (((state.validation_metadata or {}).get("validation") or {}).get("artifact_sha256") or "")
            if state else ""
        )
        if not checksum:
            checksum = str((dataset.coverage or {}).get("artifact_sha256") or "")
        replayed, conflicts = replay_mintrans_matches(session, dataset=dataset, checksum=checksum, observed_at=now)
        matched = int(session.scalar(select(func.count()).select_from(MintransTedEntry).where(MintransTedEntry.dataset_id == dataset.id, MintransTedEntry.match_state == "matched")) or 0)
        total_conflicts = int(session.scalar(select(func.count()).select_from(MintransTedEntry).where(MintransTedEntry.dataset_id == dataset.id, MintransTedEntry.match_state == "conflict_identity")) or 0)
        normalized_records = int(dataset.record_count or 0)
        source_records = int((dataset.coverage or {}).get("source_records") or normalized_records)
        summary = SourceChangeSummary(
            matched_companies=int(session.scalar(select(func.count(func.distinct(MintransTedEntry.company_id))).where(MintransTedEntry.dataset_id == dataset.id, MintransTedEntry.company_id.is_not(None))) or 0),
            new_facts=0, changed_facts=0, removed_or_expired_facts=0,
            unchanged_facts=max(0, matched - replayed), replayed_facts=replayed,
            quarantined_records=int((dataset.coverage or {}).get("quarantined_records") or 0),
            source_records=source_records, source_data_date=dataset.last_data_date, previous_source_data_date=dataset.last_data_date,
        )
        coverage = dict(dataset.coverage or {})
        coverage.update({
            "matched": matched,
            "unmatched": max(0, normalized_records - matched - total_conflicts),
            "identity_conflicts": total_conflicts,
            "last_replay_conflicts": conflicts,
            "replayed_facts": replayed,
            "change_summary": summary.as_dict(),
        })
        dataset.coverage = coverage
        dataset.checked_at = now; dataset.official_actual_until = now.date(); dataset.operational_status = OperationalStatus.CURRENT
        dataset.next_expected_update_at = now + CHECK_INTERVAL; dataset.last_error = None; dataset.last_error_at = None
        return replace(result, change_summary=summary, counters=ExecutionCounters(records_seen=source_records, records_published=replayed), checksum_metadata={**result.checksum_metadata, "check_only": True, "replayed_facts": replayed})

    validation = dict(result.staging_result.validation.metadata)
    artifact = _artifact_row(session, dataset, validation)
    old_fact_hashes = set(session.scalars(
        select(MintransTedEntry.row_hash)
        .join(TransportForwardingRegistryListing, TransportForwardingRegistryListing.source_entry_id == MintransTedEntry.id)
        .where(TransportForwardingRegistryListing.dataset_id == dataset.id)
    ))
    session.execute(delete(TransportForwardingRegistryListing).where(TransportForwardingRegistryListing.dataset_id == dataset.id))
    session.execute(delete(MintransTedEntry).where(MintransTedEntry.dataset_id == dataset.id))
    session.execute(delete(MintransTedQuarantineRow).where(MintransTedQuarantineRow.dataset_id == dataset.id))
    batch: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    matched = unmatched = conflicts = published = 0
    matched_fact_hashes: set[str] = set()
    for item in _iter_jsonl(result.staging_result.staging_pointer):
        if item.pop("kind") == "quarantine":
            quarantine.append({"dataset_id": dataset.id, "artifact_id": artifact.id, **item})
            if len(quarantine) >= BATCH_SIZE:
                session.execute(pg_insert(MintransTedQuarantineRow).values(quarantine)); quarantine = []
            continue
        batch.append(item)
        if len(batch) < BATCH_SIZE: continue
        by_inn, by_ogrn = _maps(session, batch); pending = []
        for row in batch:
            match = resolve_exact_identity(inn=row.get("inn"), ogrn=row.get("ogrn"), companies_by_inn=by_inn, companies_by_ogrn=by_ogrn)
            entry = MintransTedEntry(dataset_id=dataset.id, artifact_id=artifact.id, company_id=match.company_id, match_state=match.state, match_method=match.method, source_row_number=row["source_row_number"], inn=row.get("inn"), ogrn=row.get("ogrn"), registry_number=row["registry_number"], included_at=date.fromisoformat(row["included_at"]), row_hash=row["row_hash"], validation_state=row["validation_state"])
            session.add(entry); pending.append((entry, row, match))
            if match.state == "matched": matched += 1; matched_fact_hashes.add(row["row_hash"])
            elif match.state == "unmatched": unmatched += 1
            else: conflicts += 1
        session.flush(); published += _insert_facts(session, pending, dataset=dataset, checksum=validation["artifact_sha256"], observed_at=now); batch = []
    if batch:
        by_inn, by_ogrn = _maps(session, batch); pending = []
        for row in batch:
            match = resolve_exact_identity(inn=row.get("inn"), ogrn=row.get("ogrn"), companies_by_inn=by_inn, companies_by_ogrn=by_ogrn)
            entry = MintransTedEntry(dataset_id=dataset.id, artifact_id=artifact.id, company_id=match.company_id, match_state=match.state, match_method=match.method, source_row_number=row["source_row_number"], inn=row.get("inn"), ogrn=row.get("ogrn"), registry_number=row["registry_number"], included_at=date.fromisoformat(row["included_at"]), row_hash=row["row_hash"], validation_state=row["validation_state"])
            session.add(entry); pending.append((entry, row, match))
            if match.state == "matched": matched += 1; matched_fact_hashes.add(row["row_hash"])
            elif match.state == "unmatched": unmatched += 1
            else: conflicts += 1
        session.flush(); published += _insert_facts(session, pending, dataset=dataset, checksum=validation["artifact_sha256"], observed_at=now)
    if quarantine: session.execute(pg_insert(MintransTedQuarantineRow).values(quarantine))
    source_date = date.fromisoformat(validation["source_data_date"])
    previous_date = dataset.last_data_date
    matched_companies = int(session.scalar(select(func.count(func.distinct(MintransTedEntry.company_id))).where(MintransTedEntry.dataset_id == dataset.id, MintransTedEntry.company_id.is_not(None))) or 0)
    summary = SourceChangeSummary(
        matched_companies=matched_companies,
        new_facts=len(matched_fact_hashes - old_fact_hashes), changed_facts=0,
        removed_or_expired_facts=len(old_fact_hashes - matched_fact_hashes),
        unchanged_facts=len(old_fact_hashes & matched_fact_hashes), replayed_facts=0,
        quarantined_records=int(validation["quarantined_records"]), source_records=int(validation["source_records"]),
        source_data_date=source_date, previous_source_data_date=previous_date,
        unavailable_reasons={"previous_source_data_date": "first successful publication"} if previous_date is None else {},
    )
    dataset.enabled = True; dataset.dataset_kind = "bulk_snapshot"; dataset.freshness_policy = "irregular"
    dataset.auto_update_status = AutoUpdateStatus.CONFIGURED; dataset.operational_status = OperationalStatus.CURRENT
    dataset.last_success_at = dataset.checked_at = dataset.published_at = now
    dataset.last_data_date = source_date; dataset.source_as_of = datetime.combine(source_date, datetime.min.time(), tzinfo=timezone.utc)
    dataset.retrieved_at = now; dataset.official_actual_until = now.date(); dataset.record_count = int(validation["normalized_records"])
    dataset.next_expected_update_at = now + CHECK_INTERVAL
    dataset.coverage = {
        "source_records": int(validation["source_records"]), "normalized_records": int(validation["normalized_records"]),
        "quarantined_records": int(validation["quarantined_records"]), "duplicate_records": int(validation["duplicate_records"]),
        "matched": matched, "unmatched": unmatched, "identity_conflicts": conflicts, "matched_companies": matched_companies,
        "published_facts": published, "matching_method": "inn_exact_then_ogrn_exact",
        "artifact_sha256": validation["artifact_sha256"], "release_identity": release_identity,
        "api_projection": "transport_forwarding_registry", "card_projection": "company_card.transport_forwarding_registry",
        "change_summary": summary.as_dict(),
    }
    return replace(result, change_summary=summary, counters=ExecutionCounters(
        records_seen=int(validation["source_records"]), records_written=int(validation["normalized_records"]),
        records_rejected=int(validation["quarantined_records"]), records_duplicated=int(validation["duplicate_records"]), records_published=published,
    ))


def register_mintrans_ted_worker(session: Session, registry: HandlerRegistry) -> Any:
    return register_handler(
        session, registry, source_id=SOURCE_ID, version=HANDLER_VERSION, handler=mintrans_ted_worker_handler,
        publisher=publish_mintrans_ted_result, approved=True, live=False, fixture=False,
        metadata={"mode": "official_bulk_snapshot", "source_owner": "Минтранс России", "matching_method": "inn_exact_then_ogrn_exact"},
    )


def schedule_mintrans_ted_check(session: Session, *, raw_root: Path, now: datetime | None = None, provider: MintransOfficialProvider | None = None) -> JobCreation:
    now = now or utc_now()
    approval = session.get(WorkerHandlerRegistration, (SOURCE_ID, HANDLER_VERSION))
    if approval is None or not approval.approved or not approval.enabled or approval.live_mode:
        raise HandlerNotRegisteredError(f"durable handler approval is missing: {SOURCE_ID}@{HANDLER_VERSION}")
    release = (provider or MintransOfficialProvider()).discover()
    state = session.get(WorkerPublicationState, SOURCE_ID)
    accepted_identity = (((state.validation_metadata or {}).get("validation") or {}).get("release_identity") if state else None)
    check_only = bool(state and state.active_pointer and accepted_identity == release.identity)
    return create_job(
        session, source_id=SOURCE_ID, job_type="mintrans_ted_registry_check", handler_version=HANDLER_VERSION,
        idempotency_key=f"{SOURCE_ID}:check:{now.date().isoformat()}:{release.identity}:{HANDLER_VERSION}",
        schedule_metadata={**release.as_metadata(), "raw_root": str(Path(raw_root).resolve()), "check_only": check_only, "check_frequency": "daily", "publication_frequency": "official_irregular_snapshot"},
        max_attempts=3, timeout_seconds=3600, now=now,
    )


def mintrans_card_projection(session: Session, *, company_id: int) -> list[dict[str, Any]]:
    rows = session.scalars(select(TransportForwardingRegistryListing).where(TransportForwardingRegistryListing.company_id == company_id).order_by(TransportForwardingRegistryListing.effective_from.desc())).all()
    return [{**row.value, "fact_code": row.fact_code, "source": "Минтранс России", "evidence": row.evidence} for row in rows]
