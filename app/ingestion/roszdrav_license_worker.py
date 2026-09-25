"""Worker Foundation adapters for three Roszdravnadzor licence snapshots.

Each official licence category keeps its own source id, lease, run history,
publication pointer, freshness boundary, change summary and fact semantics.
The existing W1-004 on-demand provider is not used or modified here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable
from urllib.parse import unquote, urlparse
from zipfile import BadZipFile, ZipFile

from lxml import etree
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.ingestion.roszdrav import parse_license_xml, validate_complete_snapshot
from app.models.company import Company
from app.models.roszdrav import RoszdravLicenseEntry
from app.models.source import DataSet
from app.models.worker import WorkerHandlerRegistration, WorkerPublicationState
from app.providers.roszdrav_open_data_provider import (
    RoszdravOpenDataError,
    RoszdravOpenDataProvider,
    RoszdravOpenDataRelease,
)
from app.services.roszdrav_registry_service import LICENSE_DATASETS
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


CHECK_INTERVAL = timedelta(days=1)
HANDLER_VERSION = "roszdrav-license-official-v1"
NORMALIZED_BATCH_SIZE = 1000


@dataclass(frozen=True)
class RoszdravLicenseSourceSpec:
    category: str
    source_id: str
    dataset_code: str
    source_page_url: str
    api_projection: str = "roszdrav_bulk_license_check"
    card_projection: str = "company_card.roszdrav_bulk_licenses"

    def __post_init__(self) -> None:
        if self.category not in LICENSE_DATASETS:
            raise ValueError(f"unsupported Roszdrav licence category: {self.category}")
        if self.dataset_code != LICENSE_DATASETS[self.category]:
            raise ValueError("Roszdrav category and dataset code differ")
        if self.source_id != self.dataset_code:
            raise ValueError("Roszdrav source id must preserve dataset identity")


SPECS = {
    "pharma": RoszdravLicenseSourceSpec(
        category="pharma",
        source_id=LICENSE_DATASETS["pharma"],
        dataset_code=LICENSE_DATASETS["pharma"],
        source_page_url=(
            "https://roszdravnadzor.gov.ru/opendata/7710537160-ls_licenses"
        ),
    ),
    "narcotics": RoszdravLicenseSourceSpec(
        category="narcotics",
        source_id=LICENSE_DATASETS["narcotics"],
        dataset_code=LICENSE_DATASETS["narcotics"],
        source_page_url=(
            "https://roszdravnadzor.gov.ru/opendata/7710537160-nark_licenses"
        ),
    ),
    "medical_device_maintenance": RoszdravLicenseSourceSpec(
        category="medical_device_maintenance",
        source_id=LICENSE_DATASETS["medical_device_maintenance"],
        dataset_code=LICENSE_DATASETS["medical_device_maintenance"],
        source_page_url=(
            "https://roszdravnadzor.gov.ru/opendata/7710537160-md_licenses"
        ),
    ),
}
SPEC_BY_SOURCE_ID = {spec.source_id: spec for spec in SPECS.values()}


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
            raise InvalidDataError(f"immutable Roszdrav RAW member differs: {path}")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _persist_temp(temp_path: Path, target: Path, *, checksum: str) -> None:
    if target.exists():
        actual, _size = _hash_file(target)
        temp_path.unlink(missing_ok=True)
        if actual != checksum:
            raise InvalidDataError(
                f"immutable Roszdrav normalized snapshot differs: {target}"
            )
        return
    try:
        os.link(temp_path, target)
        target.chmod(0o444)
    except FileExistsError:
        actual, _size = _hash_file(target)
        if actual != checksum:
            raise InvalidDataError(
                f"immutable Roszdrav normalized snapshot differs: {target}"
            )
    finally:
        temp_path.unlink(missing_ok=True)


def _file_path(reference: str) -> Path:
    parsed = urlparse(reference)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise InvalidDataError("accepted Roszdrav snapshot must use a local file URI")
    path = Path(unquote(parsed.path))
    if not path.is_absolute() or not path.is_file():
        raise InvalidDataError("accepted Roszdrav snapshot is unavailable")
    return path


def _release_from_metadata(
    metadata: dict[str, Any],
    *,
    spec: RoszdravLicenseSourceSpec,
) -> RoszdravOpenDataRelease:
    required = {
        "source_page_url",
        "artifact_url",
        "xsd_url",
        "source_data_date",
        "actual_until",
        "discovered_at",
        "provenance",
        "release_identity",
    }
    missing = sorted(key for key in required if metadata.get(key) in {None, ""})
    if missing:
        raise InvalidDataError(
            "Roszdrav release metadata missing: " + ", ".join(missing)
        )
    release = RoszdravOpenDataRelease(
        source_page_url=str(metadata["source_page_url"]),
        artifact_url=str(metadata["artifact_url"]),
        xsd_url=str(metadata["xsd_url"]),
        source_data_date=date.fromisoformat(str(metadata["source_data_date"])),
        actual_until=date.fromisoformat(str(metadata["actual_until"])),
        discovered_at=_utc(datetime.fromisoformat(str(metadata["discovered_at"]))),
        provenance=str(metadata["provenance"]),
    )
    if release.source_page_url != spec.source_page_url:
        raise InvalidDataError("Roszdrav source page differs from handler pin")
    if release.identity != str(metadata["release_identity"]):
        raise InvalidDataError("Roszdrav release identity changed")
    if release.actual_until < release.source_data_date:
        raise InvalidDataError("Roszdrav official validity predates the release")
    return release


def _provider_error(error: RoszdravOpenDataError) -> Exception:
    if error.kind in {"timeout", "network_error", "service_unavailable"}:
        return WorkerNetworkError(f"Roszdrav request failed: {error.kind}")
    return SchemaMismatchError(f"Roszdrav response rejected: {error.kind}")


def _single_xml(content: bytes) -> tuple[bytes, str]:
    descriptor, name = tempfile.mkstemp(suffix=".zip")
    os.close(descriptor)
    path = Path(name)
    try:
        path.write_bytes(content)
        with ZipFile(path) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise SchemaMismatchError(f"Roszdrav ZIP CRC error: {bad}")
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1 or not members[0].filename.lower().endswith(".xml"):
                raise SchemaMismatchError("Roszdrav ZIP must contain exactly one XML")
            xml_content = archive.read(members[0])
            if len(xml_content) != members[0].file_size:
                raise SchemaMismatchError("Roszdrav XML member is truncated")
            return xml_content, members[0].filename
    except BadZipFile as error:
        raise SchemaMismatchError("Roszdrav archive is not a valid ZIP") from error
    finally:
        path.unlink(missing_ok=True)


def _validate_xsd(xml_content: bytes, xsd_content: bytes) -> None:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        schema = etree.XMLSchema(etree.fromstring(xsd_content, parser=parser))
        document = etree.fromstring(xml_content, parser=parser)
        schema.assertValid(document)
    except (etree.XMLSyntaxError, etree.XMLSchemaParseError) as error:
        raise SchemaMismatchError("Roszdrav XSD is invalid") from error
    except etree.DocumentInvalid as error:
        message = str(error.error_log.last_error or error)
        raise SchemaMismatchError(
            f"Roszdrav official XML fails official XSD: {message}"
        ) from error


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
            for record in sorted(
                records,
                key=lambda item: (
                    str(item["inn"]),
                    str(item["license_number"]),
                    str(item["record_key"]),
                ),
            ):
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


def run_roszdrav_license_handler(
    context: HandlerContext,
    *,
    spec: RoszdravLicenseSourceSpec,
    provider: RoszdravOpenDataProvider | None = None,
) -> HandlerResult:
    release = _release_from_metadata(context.schedule_metadata, spec=spec)
    if context.schedule_metadata.get("check_only"):
        replay_pointer = str(
            context.schedule_metadata.get("replay_pointer") or ""
        )
        replay_checksum = str(
            context.schedule_metadata.get("replay_checksum") or ""
        )
        if not replay_pointer or not replay_checksum:
            raise InvalidDataError("accepted Roszdrav replay metadata is missing")
        replay_path = _file_path(replay_pointer)
        actual_checksum, _size = _hash_file(replay_path)
        if actual_checksum != replay_checksum:
            raise InvalidDataError("accepted Roszdrav normalized replay checksum changed")
        return HandlerResult(
            checksum_metadata={
                "check_only": True,
                "replay_snapshot": True,
                "release_identity": release.identity,
                "official_actual_until": release.actual_until.isoformat(),
            }
        )
    raw_value = str(context.schedule_metadata.get("raw_root") or "").strip()
    if not raw_value:
        raise InvalidDataError("raw_root is required")
    raw_root_value = Path(raw_value)
    if not raw_root_value.is_absolute():
        raise InvalidDataError("raw_root must be absolute")
    raw_root = raw_root_value.resolve()
    context.ensure_active(now=utc_now())
    client = provider or RoszdravOpenDataProvider()
    try:
        archive_response = client.download(release.artifact_url)
        xsd_response = client.download(release.xsd_url)
    except RoszdravOpenDataError as error:
        raise _provider_error(error) from error

    archive_content = bytes(archive_response["content"])
    xsd_content = bytes(xsd_response["content"])
    retrieved_at = utc_now()
    artifact_checksum = sha256(archive_content).hexdigest()
    xsd_checksum = sha256(xsd_content).hexdigest()

    artifact_dir = raw_root / spec.source_id / artifact_checksum
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / Path(urlparse(release.artifact_url).path).name
    xsd_path = artifact_dir / Path(urlparse(release.xsd_url).path).name
    _write_once(artifact_path, archive_content)
    _write_once(xsd_path, xsd_content)
    retrieval_manifest = {
        "manifest_version": 1,
        "source_id": spec.source_id,
        "dataset_code": spec.dataset_code,
        "category": spec.category,
        "source_page_url": release.source_page_url,
        "artifact_url": release.artifact_url,
        "artifact_reference": artifact_path.as_uri(),
        "artifact_sha256": artifact_checksum,
        "artifact_size": len(archive_content),
        "xsd_url": release.xsd_url,
        "xsd_reference": xsd_path.as_uri(),
        "xsd_sha256": xsd_checksum,
        "xsd_size": len(xsd_content),
        "source_data_date": release.source_data_date.isoformat(),
        "release_identity": release.identity,
        "immutable": True,
    }
    _write_once(
        artifact_dir / "retrieval-manifest.json",
        (
            json.dumps(retrieval_manifest, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8"),
    )

    # The durable source bytes and their provenance exist before parsing starts.
    xml_content, xml_name = _single_xml(archive_content)
    _validate_xsd(xml_content, xsd_content)
    try:
        parsed = parse_license_xml(
            xml_content,
            category=spec.category,
            data_date=release.source_data_date,
        )
        validate_complete_snapshot(parsed)
    except ValueError as error:
        raise SchemaMismatchError(str(error)) from error

    normalized_path, normalized_checksum = _normalize_to_jsonl(
        parsed["records"], artifact_dir=artifact_dir
    )
    stable_manifest = {
        "manifest_version": 1,
        "source_id": spec.source_id,
        "dataset_code": spec.dataset_code,
        "category": spec.category,
        "source_page_url": release.source_page_url,
        "artifact_url": release.artifact_url,
        "artifact_reference": artifact_path.as_uri(),
        "artifact_sha256": artifact_checksum,
        "artifact_size": len(archive_content),
        "xsd_url": release.xsd_url,
        "xsd_reference": xsd_path.as_uri(),
        "xsd_sha256": xsd_checksum,
        "normalized_reference": normalized_path.as_uri(),
        "normalized_sha256": normalized_checksum,
        "source_data_date": release.source_data_date.isoformat(),
        "release_identity": release.identity,
        "xml_member": xml_name,
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
    observation_manifest = {
        **stable_manifest,
        "retrieved_at": retrieved_at.isoformat(),
        "official_actual_until": release.actual_until.isoformat(),
        "provenance": release.provenance,
        "artifact_http_headers": dict(archive_response.get("headers") or {}),
        "xsd_http_headers": dict(xsd_response.get("headers") or {}),
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
                    "release_identity": release.identity,
                    "source_data_date": release.source_data_date.isoformat(),
                    "official_actual_until": release.actual_until.isoformat(),
                    "retrieved_at": retrieved_at.isoformat(),
                    "source_records": int(parsed["source_records"]),
                    "imported_records": int(parsed["imported_records"]),
                    "duplicate_records": int(parsed["duplicate_records"]),
                    "rejected_records": int(parsed["rejected_records"]),
                },
            ),
            metadata={
                "source_id": spec.source_id,
                "dataset_code": spec.dataset_code,
                "license_category": spec.category,
                "source_page_url": release.source_page_url,
                "api_projection": spec.api_projection,
                "card_projection": spec.card_projection,
            },
        ),
        checksum_metadata={
            "artifact_sha256": artifact_checksum,
            "xsd_sha256": xsd_checksum,
            "normalized_sha256": normalized_checksum,
            "release_identity": release.identity,
        },
        counters=counters,
    )


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


def _fact_identity(row: Any) -> tuple[str, str, str]:
    if isinstance(row, dict):
        return (
            str(row["category"]),
            str(row["inn"]),
            str(row["license_number"]),
        )
    return (str(row.category), str(row.inn), str(row.license_number))


def _change_counts(
    old_rows: list[RoszdravLicenseEntry],
    new_rows: list[dict[str, Any]],
) -> dict[str, int]:
    old: dict[tuple[str, str, str], Counter[str]] = {}
    new: dict[tuple[str, str, str], Counter[str]] = {}
    for row in old_rows:
        old.setdefault(_fact_identity(row), Counter())[str(row.record_key)] += 1
    for row in new_rows:
        new.setdefault(_fact_identity(row), Counter())[str(row["record_key"])] += 1

    counts = {
        "new_facts": 0,
        "changed_facts": 0,
        "removed_or_expired_facts": 0,
        "unchanged_facts": 0,
    }
    for identity in set(old) | set(new):
        old_keys = old.get(identity, Counter())
        new_keys = new.get(identity, Counter())
        unchanged = sum((old_keys & new_keys).values())
        old_remaining = sum(old_keys.values()) - unchanged
        new_remaining = sum(new_keys.values()) - unchanged
        changed = min(old_remaining, new_remaining)
        counts["unchanged_facts"] += unchanged
        counts["changed_facts"] += changed
        counts["removed_or_expired_facts"] += old_remaining - changed
        counts["new_facts"] += new_remaining - changed
    return counts


def _row_values(dataset_id: int, row: dict[str, Any]) -> dict[str, Any]:
    def parsed_date(value):
        return date.fromisoformat(value) if value else None

    return {
        "dataset_id": dataset_id,
        "data_date": parsed_date(row["data_date"]),
        "record_key": row["record_key"],
        "category": row["category"],
        "inn": row["inn"],
        "ogrn": row.get("ogrn"),
        "license_number": row["license_number"],
        "licensee_name": row.get("licensee_name"),
        "authority_name": row.get("authority_name"),
        "activity_type": row.get("activity_type"),
        "legal_form": row.get("legal_form"),
        "address": row.get("address"),
        "work_places": row.get("work_places") or [],
        "decision_date": parsed_date(row.get("decision_date")),
        "start_date": parsed_date(row.get("start_date")),
        "end_date": parsed_date(row.get("end_date")),
        "termination_info": row.get("termination_info"),
        "termination_date": parsed_date(row.get("termination_date")),
        "suspension_info": row.get("suspension_info"),
        "cancellation_info": row.get("cancellation_info"),
        "raw_payload": row.get("raw_payload") or {},
    }


def publish_roszdrav_license_result(
    session: Session,
    claim: Any,
    result: HandlerResult,
    *,
    spec: RoszdravLicenseSourceSpec,
) -> HandlerResult:
    release = _release_from_metadata(claim.schedule_metadata, spec=spec)
    dataset = session.scalar(
        select(DataSet)
        .where(DataSet.code == spec.dataset_code)
        .with_for_update()
    )
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {spec.dataset_code}")
    state = session.scalar(
        select(WorkerPublicationState)
        .where(WorkerPublicationState.source_id == spec.source_id)
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
            .select_from(RoszdravLicenseEntry)
            .where(RoszdravLicenseEntry.dataset_id == dataset.id)
        )
        or 0
    )
    now = utc_now()

    if result.staging_result is None or previous_identity == release.identity:
        if previous_identity != release.identity or existing_count <= 0:
            raise InvalidDataError("accepted Roszdrav snapshot rows are unavailable")
        _apply_successful_check(dataset, actual_until=release.actual_until, now=now)
        coverage = dict(dataset.coverage or {})
        matched_companies = int(coverage.get("matched_companies") or 0)
        source_records = int(coverage.get("source_records") or existing_count)
        summary = SourceChangeSummary(
            matched_companies=matched_companies,
            new_facts=0,
            changed_facts=0,
            removed_or_expired_facts=0,
            unchanged_facts=existing_count,
            replayed_facts=0,
            quarantined_records=0,
            source_records=source_records,
            source_data_date=dataset.last_data_date,
            previous_source_data_date=dataset.last_data_date,
        )
        coverage["last_check"] = {
            "checked_at": now.isoformat(),
            "release_identity": release.identity,
            "changed": False,
        }
        coverage["change_summary"] = summary.as_dict()
        dataset.coverage = coverage
        return replace(
            result,
            checksum_metadata={
                **result.checksum_metadata,
                "check_only": True,
                "freshness": dataset.operational_status.value,
                "official_actual_until": release.actual_until.isoformat(),
            },
            counters=ExecutionCounters(records_seen=source_records),
            change_summary=summary,
        )

    validation = dict(result.staging_result.validation.metadata)
    if validation.get("release_identity") != release.identity:
        raise InvalidDataError("Roszdrav normalized release identity differs")
    source_records = int(validation["source_records"])
    imported_records = int(validation["imported_records"])
    staging_path = _file_path(result.staging_result.staging_pointer)
    old_rows = list(
        session.scalars(
            select(RoszdravLicenseEntry).where(
                RoszdravLicenseEntry.dataset_id == dataset.id
            )
        )
    )
    normalized_rows = [row for batch in _iter_jsonl(staging_path) for row in batch]
    if len(normalized_rows) != imported_records:
        raise InvalidDataError("Roszdrav normalized snapshot count differs from source")
    if any(row.get("category") != spec.category for row in normalized_rows):
        raise InvalidDataError("Roszdrav normalized snapshot mixes licence categories")
    changes = _change_counts(old_rows, normalized_rows)
    previous_source_date = dataset.last_data_date

    session.execute(
        delete(RoszdravLicenseEntry).where(
            RoszdravLicenseEntry.dataset_id == dataset.id
        )
    )
    published = 0
    for start in range(0, len(normalized_rows), NORMALIZED_BATCH_SIZE):
        values = [
            _row_values(dataset.id, row)
            for row in normalized_rows[start : start + NORMALIZED_BATCH_SIZE]
        ]
        if values:
            session.execute(pg_insert(RoszdravLicenseEntry).values(values))
            published += len(values)
    if published != imported_records:
        raise InvalidDataError("Roszdrav publication count differs from source")

    matched_facts = int(
        session.scalar(
            select(func.count())
            .select_from(RoszdravLicenseEntry)
            .join(Company, Company.inn == RoszdravLicenseEntry.inn)
            .where(RoszdravLicenseEntry.dataset_id == dataset.id)
        )
        or 0
    )
    matched_companies = int(
        session.scalar(
            select(func.count(func.distinct(Company.id)))
            .select_from(RoszdravLicenseEntry)
            .join(Company, Company.inn == RoszdravLicenseEntry.inn)
            .where(RoszdravLicenseEntry.dataset_id == dataset.id)
        )
        or 0
    )
    unavailable_reasons = (
        {"previous_source_data_date": "no_previous_successful_publication"}
        if previous_source_date is None
        else {}
    )
    summary = SourceChangeSummary(
        matched_companies=matched_companies,
        new_facts=changes["new_facts"],
        changed_facts=changes["changed_facts"],
        removed_or_expired_facts=changes["removed_or_expired_facts"],
        unchanged_facts=changes["unchanged_facts"],
        replayed_facts=0,
        quarantined_records=0,
        source_records=source_records,
        source_data_date=release.source_data_date,
        previous_source_data_date=previous_source_date,
        unavailable_reasons=unavailable_reasons,
    )
    dataset.enabled = True
    dataset.auto_update_status = AutoUpdateStatus.CONFIGURED
    dataset.last_success_at = now
    dataset.last_data_date = release.source_data_date
    dataset.source_as_of = datetime.combine(
        release.source_data_date, datetime.min.time(), tzinfo=timezone.utc
    )
    dataset.retrieved_at = _utc(datetime.fromisoformat(str(validation["retrieved_at"])))
    dataset.published_at = now
    dataset.record_count = published
    dataset.coverage = {
        "license_category": spec.category,
        "source_records": source_records,
        "matched": matched_facts,
        "unmatched": published - matched_facts,
        "matched_companies": matched_companies,
        "published_facts": published,
        "matching_method": "inn_exact",
        "api_projection": spec.api_projection,
        "card_projection": spec.card_projection,
        "release_identity": release.identity,
        "change_summary": summary.as_dict(),
    }
    _apply_successful_check(dataset, actual_until=release.actual_until, now=now)
    updated_validation = replace(
        result.staging_result.validation,
        metadata={
            **validation,
            "matched": matched_facts,
            "unmatched": published - matched_facts,
            "matched_companies": matched_companies,
            "published_facts": published,
            "matching_method": "inn_exact",
            "freshness": dataset.operational_status.value,
            "change_summary": summary.as_dict(),
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
            records_rejected=int(validation["rejected_records"]),
            records_duplicated=int(validation["duplicate_records"]),
            records_published=published,
        ),
        change_summary=summary,
    )


def roszdrav_pharma_worker_handler(context: HandlerContext) -> HandlerResult:
    return run_roszdrav_license_handler(context, spec=SPECS["pharma"])


def roszdrav_narcotics_worker_handler(context: HandlerContext) -> HandlerResult:
    return run_roszdrav_license_handler(context, spec=SPECS["narcotics"])


def roszdrav_medical_device_maintenance_worker_handler(
    context: HandlerContext,
) -> HandlerResult:
    return run_roszdrav_license_handler(
        context, spec=SPECS["medical_device_maintenance"]
    )


def publish_roszdrav_pharma_result(session, claim, result):
    return publish_roszdrav_license_result(
        session, claim, result, spec=SPECS["pharma"]
    )


def publish_roszdrav_narcotics_result(session, claim, result):
    return publish_roszdrav_license_result(
        session, claim, result, spec=SPECS["narcotics"]
    )


def publish_roszdrav_medical_device_maintenance_result(session, claim, result):
    return publish_roszdrav_license_result(
        session, claim, result, spec=SPECS["medical_device_maintenance"]
    )


HANDLERS = {
    "pharma": roszdrav_pharma_worker_handler,
    "narcotics": roszdrav_narcotics_worker_handler,
    "medical_device_maintenance": (
        roszdrav_medical_device_maintenance_worker_handler
    ),
}
PUBLISHERS = {
    "pharma": publish_roszdrav_pharma_result,
    "narcotics": publish_roszdrav_narcotics_result,
    "medical_device_maintenance": (
        publish_roszdrav_medical_device_maintenance_result
    ),
}


def register_roszdrav_license_workers(
    session: Session,
    registry: HandlerRegistry,
) -> tuple[Any, ...]:
    registrations = []
    for category, spec in SPECS.items():
        registrations.append(
            register_handler(
                session,
                registry,
                source_id=spec.source_id,
                version=HANDLER_VERSION,
                handler=HANDLERS[category],
                publisher=PUBLISHERS[category],
                approved=True,
                live=False,
                fixture=False,
                metadata={
                    "mode": "official_bulk_snapshot",
                    "source_page_url": spec.source_page_url,
                    "license_category": spec.category,
                    "matching_method": "inn_exact",
                },
            )
        )
    return tuple(registrations)


def schedule_roszdrav_license_check(
    session: Session,
    *,
    category: str,
    raw_root: Path,
    now: datetime | None = None,
    provider: RoszdravOpenDataProvider | None = None,
) -> JobCreation:
    try:
        spec = SPECS[category]
    except KeyError as error:
        raise ValueError(f"unsupported Roszdrav licence category: {category}") from error
    now = _utc(now or utc_now())
    raw_root = Path(raw_root)
    if not raw_root.is_absolute():
        raise InvalidDataError("raw_root must be absolute")
    approval = session.get(
        WorkerHandlerRegistration, (spec.source_id, HANDLER_VERSION)
    )
    if (
        approval is None
        or not approval.approved
        or not approval.enabled
        or approval.live_mode
    ):
        raise HandlerNotRegisteredError(
            f"durable handler approval is missing: {spec.source_id}@{HANDLER_VERSION}"
        )
    try:
        release = (provider or RoszdravOpenDataProvider()).discover(
            spec.source_page_url, now=now
        )
    except RoszdravOpenDataError as error:
        raise _provider_error(error) from error
    state = session.get(WorkerPublicationState, spec.source_id)
    accepted_identity = (
        ((state.validation_metadata or {}).get("validation") or {}).get(
            "release_identity"
        )
        if state is not None
        else None
    )
    check_only = bool(
        state is not None
        and state.active_pointer
        and accepted_identity == release.identity
    )
    replay_checksum = (
        str((state.validation_metadata or {}).get("checksum") or "")
        if check_only
        else ""
    )
    if check_only and not replay_checksum:
        raise InvalidDataError("accepted Roszdrav replay checksum is missing")
    check_date = now.date().isoformat()
    return create_job(
        session,
        source_id=spec.source_id,
        job_type=f"{spec.source_id}_check",
        handler_version=HANDLER_VERSION,
        idempotency_key=(
            f"{spec.source_id}:check:{check_date}:{release.identity}:"
            f"{HANDLER_VERSION}"
        ),
        schedule_metadata={
            **release.as_metadata(),
            "license_category": spec.category,
            "raw_root": str(raw_root.resolve()),
            "check_only": check_only,
            "replay_pointer": state.active_pointer if check_only else None,
            "replay_checksum": replay_checksum or None,
            "check_frequency": "daily",
            "publication_frequency": "weekly_official_snapshot",
        },
        max_attempts=3,
        timeout_seconds=1800,
        now=now,
    )
