"""S02 FNS Tax Debt worker pipeline: RAW -> normalized rows -> canonical facts."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile, is_zipfile

from sqlalchemy import delete, func, select

from app.models.company import Company
from app.models.source import DataSet
from app.models.tax_debt import (
    CompanyTaxDebtItem,
    CompanyTaxDebtSnapshot,
    FnsTaxDebtNormalizedRecord,
    FnsTaxDebtQuarantineRecord,
    FnsTaxDebtRawArtifact,
    TAX_DEBT_FACT_CODE,
)
from app.sources.fns_tax_debt import (
    CONTROLLED_LIVE_PILOT_ENABLED,
    DATASET_CODE,
    FACT_CODE,
    FNS_TAX_DEBT_SOURCE_CONTRACT,
    HANDLER_VERSION,
    NORMALIZATION_VERSION,
    OFFICIAL_SOURCE_PAGE,
    PARSER_VERSION,
    SOURCE_ID,
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
    InvalidDataError,
    LegalBlockError,
    SchemaMismatchError,
    TemporaryInfrastructureError,
)
from app.worker.execution import ClaimedExecution, JobCreation, create_job, register_handler
from app.worker.registry import HandlerRegistry


MEDIA_TYPE = "application/zip"
MANIFEST_SCHEMA_VERSION = 1
STAGING_SCHEMA_VERSION = 1
EXPECTED_XML_SCHEMA_VERSION = "4.01"
EXPECTED_INFORMATION_TYPE = "ОТКРДАННЫЕ6"
MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0.00")
LIMITATION_STATES = (
    "dated_snapshot",
    "not_real_time_balance",
    "post_publication_repayments_not_reflected",
    "legal_entities_only",
    "does_not_prove_bailiff_referral",
)


class TaxDebtParseError(InvalidDataError):
    pass


class TaxDebtSchemaError(SchemaMismatchError):
    pass


class RawArtifactImmutabilityError(InvalidDataError):
    pass


@dataclass(frozen=True)
class StagedTaxDebtArtifact:
    path: Path
    manifest_path: Path
    checksum: str
    size_bytes: int
    manifest: dict[str, Any]


@dataclass(frozen=True)
class ParsedTaxDebtArtifact:
    entries: tuple[dict[str, Any], ...]
    quarantine: tuple[dict[str, Any], ...]
    coverage: dict[str, Any]


@dataclass(frozen=True)
class InnMatch:
    company_id: int | None
    state: str
    method: str | None


def _canonical_json(value: object) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return str(value)


def _aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must contain a timezone")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise InvalidDataError(f"invalid {field}: {value!r}") from error
    return _aware_utc(parsed, field)


def calculate_sha256(path: str | Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise TemporaryInfrastructureError(f"cannot read source artifact: {error}") from error
    return digest.hexdigest(), size


def _write_once(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise RawArtifactImmutabilityError(
                f"immutable artifact member differs: {path}"
            )


def stage_tax_debt_artifact(
    source_path: str | Path,
    *,
    artifact_store: str | Path,
    source_as_of: datetime,
    retrieved_at: datetime,
    mode: str = "fixture",
    expected_sha256: str | None = None,
) -> StagedTaxDebtArtifact:
    """Retain one caller-supplied official-format ZIP under its checksum."""

    if mode not in {"fixture", "controlled_live"}:
        raise LegalBlockError(f"unsupported S02 ingestion mode: {mode}")
    if mode == "controlled_live" and not CONTROLLED_LIVE_PILOT_ENABLED:
        raise LegalBlockError("S02 controlled live pilot is gated until QA approval")

    source = Path(source_path).resolve()
    if source.suffix.lower() != ".zip" or not is_zipfile(source):
        raise TaxDebtSchemaError("S02 expects a valid ZIP artifact")
    checksum, size_bytes = calculate_sha256(source)
    if size_bytes <= 0:
        raise TaxDebtParseError("S02 artifact is empty")
    if expected_sha256 is not None and checksum != expected_sha256.lower():
        raise TaxDebtParseError(
            f"S02 checksum mismatch: expected {expected_sha256}, got {checksum}"
        )

    source_as_of = _aware_utc(source_as_of, "source_as_of")
    retrieved_at = _aware_utc(retrieved_at, "retrieved_at")
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source_id": SOURCE_ID,
        "dataset_code": DATASET_CODE,
        "artifact_kind": "raw_source_snapshot",
        "ingestion_mode": mode,
        "data_format": "zip+xml",
        "media_type": MEDIA_TYPE,
        "sha256": checksum,
        "size_bytes": size_bytes,
        "original_file_name": source.name,
        "official_source": OFFICIAL_SOURCE_PAGE,
        "source_as_of": source_as_of.isoformat(),
        "retrieved_at": retrieved_at.isoformat(),
        "parser_version": PARSER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
    }

    artifact_dir = Path(artifact_store).resolve() / SOURCE_ID / checksum
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        target = artifact_dir / "artifact.zip"
        if target.exists():
            existing_checksum, existing_size = calculate_sha256(target)
            if existing_checksum != checksum or existing_size != size_bytes:
                raise RawArtifactImmutabilityError(
                    "content-addressed S02 artifact bytes changed"
                )
        else:
            try:
                with source.open("rb") as input_stream, target.open("xb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            except Exception:
                target.unlink(missing_ok=True)
                raise
            copied_checksum, copied_size = calculate_sha256(target)
            if copied_checksum != checksum or copied_size != size_bytes:
                target.unlink(missing_ok=True)
                raise RawArtifactImmutabilityError("S02 RAW copy verification failed")

        manifest_path = artifact_dir / "manifest.json"
        _write_once(
            manifest_path,
            (_canonical_json(manifest) + "\n").encode("utf-8"),
        )
    except OSError as error:
        raise TemporaryInfrastructureError(
            f"cannot persist S02 RAW artifact: {error}"
        ) from error

    return StagedTaxDebtArtifact(
        path=target,
        manifest_path=manifest_path,
        checksum=checksum,
        size_bytes=size_bytes,
        manifest=manifest,
    )


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


def _parse_money(value: object) -> Decimal:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return ZERO
    try:
        number = Decimal(text).quantize(MONEY_QUANTUM)
    except InvalidOperation as error:
        raise ValueError("invalid_money") from error
    if not number.is_finite():
        raise ValueError("invalid_money")
    if number < ZERO:
        raise ValueError("negative_money")
    return number


def is_valid_legal_entity_inn(value: str) -> bool:
    if len(value) != 10 or not value.isdigit():
        return False
    digits = [int(char) for char in value]
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    return sum(a * b for a, b in zip(digits, weights)) % 11 % 10 == digits[9]


def _hash(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _document_payload(document: ET.Element) -> dict[str, Any]:
    taxpayer = None
    debt_items = []
    for element in document:
        name = local_name(element.tag)
        if name == "СведНП":
            taxpayer = dict(element.attrib)
        elif name == "СведНедоим":
            debt_items.append(dict(element.attrib))
    return {
        "document": dict(document.attrib),
        "taxpayer": taxpayer,
        "debt_items": debt_items,
    }


def _quarantine(
    *,
    member: str,
    ordinal: int,
    reason_codes: list[str],
    raw_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source_member": member,
        "source_ordinal": ordinal,
        "raw_hash": _hash(raw_payload),
        "reason_codes": tuple(sorted(set(reason_codes))),
        "raw_payload": raw_payload,
    }


def _normalize_document(
    document: ET.Element,
    *,
    member: str,
    ordinal: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    raw = _document_payload(document)
    taxpayer = raw["taxpayer"] or {}
    inn = str(taxpayer.get("ИННЮЛ") or taxpayer.get("ИНН") or "").strip()
    data_date = _parse_date(document.attrib.get("ДатаСост"))
    document_date = _parse_date(document.attrib.get("ДатаДок"))
    reasons: list[str] = []
    if not is_valid_legal_entity_inn(inn):
        reasons.append("invalid_inn")
    if data_date is None:
        reasons.append("invalid_source_date")
    if not raw["debt_items"]:
        reasons.append("missing_debt_items")

    items_by_name: dict[str, dict[str, Any]] = {}
    for raw_item in raw["debt_items"]:
        tax_name = str(raw_item.get("НаимНалог") or "Не указано").strip()
        if not tax_name:
            tax_name = "Не указано"
        try:
            arrears = _parse_money(raw_item.get("СумНедНалог"))
            penalties = _parse_money(raw_item.get("СумПени"))
            fines = _parse_money(raw_item.get("СумШтраф"))
            total = _parse_money(raw_item.get("ОбщСумНедоим"))
        except ValueError as error:
            reasons.append(str(error))
            continue
        if total != arrears + penalties + fines:
            reasons.append("inconsistent_item_total")
            continue
        item = items_by_name.setdefault(
            tax_name,
            {
                "tax_name": tax_name,
                "arrears": ZERO,
                "penalties": ZERO,
                "fines": ZERO,
                "total": ZERO,
            },
        )
        item["arrears"] += arrears
        item["penalties"] += penalties
        item["fines"] += fines
        item["total"] += total

    if reasons:
        return None, _quarantine(
            member=member,
            ordinal=ordinal,
            reason_codes=reasons,
            raw_payload=raw,
        )

    items = tuple(items_by_name[name] for name in sorted(items_by_name))
    normalized = {
        "source_member": member,
        "source_ordinal": ordinal,
        "inn": inn,
        "company_name": taxpayer.get("НаимОрг"),
        "source_document_id": document.attrib.get("ИдДок"),
        "document_date": document_date,
        "data_date": data_date,
        "total_arrears": sum((item["arrears"] for item in items), ZERO),
        "total_penalties": sum((item["penalties"] for item in items), ZERO),
        "total_fines": sum((item["fines"] for item in items), ZERO),
        "total_debt": sum((item["total"] for item in items), ZERO),
        "items": items,
        "normalization_version": NORMALIZATION_VERSION,
        "limitation_states": LIMITATION_STATES,
    }
    normalized["record_hash"] = _hash(
        {
            key: value
            for key, value in normalized.items()
            if key not in {"source_member", "source_ordinal"}
        }
    )
    return normalized, None


def parse_tax_debt_zip(path: str | Path) -> ParsedTaxDebtArtifact:
    """Deterministically parse the supported FNS ZIP/XML snapshot structure."""

    artifact = Path(path)
    if artifact.suffix.lower() != ".zip" or not is_zipfile(artifact):
        raise TaxDebtSchemaError("S02 parser expected a ZIP container")
    candidates: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    source_records = 0
    schema_versions: set[str] = set()
    xml_members = 0
    try:
        with ZipFile(artifact) as archive:
            member_names = sorted(
                name
                for name in archive.namelist()
                if name.lower().endswith(".xml") and not name.endswith("/")
            )
            if not member_names:
                raise TaxDebtSchemaError("S02 ZIP contains no XML members")
            for member in member_names:
                xml_members += 1
                try:
                    root = ET.fromstring(archive.read(member))
                except (ET.ParseError, OSError, RuntimeError) as error:
                    raise TaxDebtParseError(
                        f"cannot parse XML member {member}: {error}"
                    ) from error
                if local_name(root.tag) != "Файл":
                    raise TaxDebtSchemaError(
                        f"unexpected S02 XML root in {member}: {local_name(root.tag)}"
                    )
                version = str(root.attrib.get("ВерсФорм") or "").strip()
                if version != EXPECTED_XML_SCHEMA_VERSION:
                    raise TaxDebtSchemaError(
                        f"unsupported S02 ВерсФорм in {member}: {version or 'missing'}"
                    )
                information_type = str(root.attrib.get("ТипИнф") or "").strip()
                if information_type != EXPECTED_INFORMATION_TYPE:
                    raise TaxDebtSchemaError(
                        f"unsupported S02 ТипИнф in {member}: "
                        f"{information_type or 'missing'}"
                    )
                schema_versions.add(version)
                documents = [node for node in root if local_name(node.tag) == "Документ"]
                if not documents:
                    raise TaxDebtSchemaError(
                        f"S02 XML member {member} has no Документ records"
                    )
                try:
                    declared_count = int(root.attrib["КолДок"])
                except (KeyError, TypeError, ValueError) as error:
                    raise TaxDebtSchemaError(
                        f"S02 XML member {member} has invalid КолДок"
                    ) from error
                if declared_count != len(documents):
                    raise TaxDebtSchemaError(
                        f"S02 XML member {member} КолДок={declared_count} "
                        f"but contains {len(documents)} records"
                    )
                for ordinal, document in enumerate(documents, start=1):
                    source_records += 1
                    normalized, invalid = _normalize_document(
                        document,
                        member=member,
                        ordinal=ordinal,
                    )
                    if invalid is not None:
                        quarantine.append(invalid)
                    elif normalized is not None:
                        candidates.append(normalized)
    except BadZipFile as error:
        raise TaxDebtParseError(f"cannot open S02 ZIP: {error}") from error

    identical_duplicates = 0
    seen_hashes: set[str] = set()
    distinct: list[dict[str, Any]] = []
    for record in candidates:
        if record["record_hash"] in seen_hashes:
            identical_duplicates += 1
            continue
        seen_hashes.add(record["record_hash"])
        distinct.append(record)

    by_identity: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for record in distinct:
        by_identity[(record["inn"], record["data_date"])].append(record)

    entries = []
    identity_conflicts = 0
    for records in by_identity.values():
        if len(records) == 1:
            entries.append(records[0])
            continue
        identity_conflicts += len(records)
        for record in records:
            quarantine.append(
                _quarantine(
                    member=record["source_member"],
                    ordinal=record["source_ordinal"],
                    reason_codes=["duplicate_identity_conflict"],
                    raw_payload={
                        key: value
                        for key, value in record.items()
                        if key != "record_hash"
                    },
                )
            )

    entries.sort(key=lambda row: (row["inn"], row["data_date"], row["record_hash"]))
    quarantine.sort(
        key=lambda row: (row["source_member"], row["source_ordinal"], row["raw_hash"])
    )
    coverage = {
        "xml_members": xml_members,
        "schema_versions": sorted(schema_versions),
        "source_records": source_records,
        "normalized_records": len(entries),
        "quarantined_records": len(quarantine),
        "identical_duplicates": identical_duplicates,
        "identity_conflicts": identity_conflicts,
    }
    return ParsedTaxDebtArtifact(
        entries=tuple(entries),
        quarantine=tuple(quarantine),
        coverage=coverage,
    )


def write_staged_normalization(
    staged: StagedTaxDebtArtifact,
    parsed: ParsedTaxDebtArtifact,
) -> Path:
    payload = {
        "schema_version": STAGING_SCHEMA_VERSION,
        "source_id": SOURCE_ID,
        "dataset_code": DATASET_CODE,
        "artifact_sha256": staged.checksum,
        "parser_version": PARSER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "coverage": parsed.coverage,
        "entries": parsed.entries,
        "quarantine": parsed.quarantine,
    }
    path = staged.path.parent / "normalized.json"
    _write_once(path, (_canonical_json(payload) + "\n").encode("utf-8"))
    return path


def _file_uri_to_path(value: str) -> Path:
    parsed = urlparse(value)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise InvalidDataError("S02 staging pointer must be a local file URI")
    return Path(unquote(parsed.path))


def load_staged_normalization(pointer: str, *, expected_sha256: str) -> dict[str, Any]:
    path = _file_uri_to_path(pointer)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InvalidDataError(f"cannot replay S02 staged normalization: {error}") from error
    if payload.get("schema_version") != STAGING_SCHEMA_VERSION:
        raise TaxDebtSchemaError("unsupported S02 staging schema")
    if payload.get("artifact_sha256") != expected_sha256:
        raise InvalidDataError("S02 staged normalization checksum reference changed")
    return payload


def resolve_inn_match(
    inn: str,
    candidates: Mapping[str, tuple[tuple[int, str | None], ...]],
) -> InnMatch:
    matches = candidates.get(inn, ())
    if not matches:
        return InnMatch(None, "unmatched", None)
    if len(matches) != 1:
        return InnMatch(None, "conflict", None)
    company_id, entity_type = matches[0]
    if entity_type not in {None, "legal"}:
        return InnMatch(None, "conflict", None)
    return InnMatch(company_id, "matched", "inn_exact")


def _identity_candidates(session, inns: set[str]) -> dict[str, tuple[tuple[int, str | None], ...]]:
    grouped: dict[str, list[tuple[int, str | None]]] = defaultdict(list)
    if inns:
        for company_id, inn, entity_type in session.execute(
            select(Company.id, Company.inn, Company.entity_type).where(Company.inn.in_(inns))
        ):
            grouped[inn].append((company_id, entity_type))
    return {key: tuple(sorted(values)) for key, values in grouped.items()}


def build_fact_provenance(
    record: Mapping[str, Any],
    *,
    artifact: FnsTaxDebtRawArtifact,
    run_id: object,
    match_method: str,
) -> dict[str, Any]:
    return {
        "source_id": SOURCE_ID,
        "dataset_code": DATASET_CODE,
        "official_source": OFFICIAL_SOURCE_PAGE,
        "artifact_sha256": artifact.sha256,
        "artifact_reference": artifact.artifact_reference,
        "worker_run_id": str(run_id),
        "source_document_id": record.get("source_document_id"),
        "source_member": record["source_member"],
        "record_hash": record["record_hash"],
        "source_as_of": artifact.source_as_of.isoformat(),
        "retrieved_at": artifact.retrieved_at.isoformat(),
        "parser_version": PARSER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "matching_method": match_method,
    }


def _artifact_row(
    session,
    *,
    dataset: DataSet,
    claim: ClaimedExecution,
    raw: RawArtifactReference,
) -> FnsTaxDebtRawArtifact:
    artifact = session.scalar(
        select(FnsTaxDebtRawArtifact).where(
            FnsTaxDebtRawArtifact.dataset_id == dataset.id,
            FnsTaxDebtRawArtifact.sha256 == raw.checksum,
        )
    )
    manifest = dict(raw.manifest)
    if artifact is None:
        artifact = FnsTaxDebtRawArtifact(
            dataset_id=dataset.id,
            first_worker_run_id=claim.run_id,
            sha256=raw.checksum,
            artifact_reference=raw.artifact_reference,
            original_file_name=manifest["original_file_name"],
            media_type=manifest["media_type"],
            size_bytes=int(manifest["size_bytes"]),
            manifest=manifest,
            source_as_of=_parse_timestamp(manifest["source_as_of"], "source_as_of"),
            retrieved_at=_parse_timestamp(manifest["retrieved_at"], "retrieved_at"),
        )
        session.add(artifact)
        session.flush()
        return artifact
    expected = {
        "artifact_reference": raw.artifact_reference,
        "original_file_name": manifest["original_file_name"],
        "media_type": manifest["media_type"],
        "size_bytes": int(manifest["size_bytes"]),
        "manifest": manifest,
    }
    if any(getattr(artifact, field) != value for field, value in expected.items()):
        raise RawArtifactImmutabilityError("persisted S02 RAW metadata changed")
    return artifact


def _restore_entry(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **value,
        "document_date": _parse_date(value.get("document_date")),
        "data_date": _parse_date(value.get("data_date")),
        "total_arrears": Decimal(str(value["total_arrears"])),
        "total_penalties": Decimal(str(value["total_penalties"])),
        "total_fines": Decimal(str(value["total_fines"])),
        "total_debt": Decimal(str(value["total_debt"])),
        "items": tuple(
            {
                **item,
                "arrears": Decimal(str(item["arrears"])),
                "penalties": Decimal(str(item["penalties"])),
                "fines": Decimal(str(item["fines"])),
                "total": Decimal(str(item["total"])),
            }
            for item in value["items"]
        ),
        "limitation_states": tuple(value["limitation_states"]),
    }


def publish_tax_debt_result(
    session,
    claim: ClaimedExecution,
    result: HandlerResult,
) -> HandlerResult:
    """Publish normalization and facts in the worker completion transaction."""

    if len(result.raw_artifacts) != 1 or result.staging_result is None:
        raise InvalidDataError("S02 handler result must contain one RAW and one staging result")
    raw = result.raw_artifacts[0]
    staged = load_staged_normalization(
        result.staging_result.staging_pointer,
        expected_sha256=raw.checksum,
    )
    dataset = session.scalar(select(DataSet).where(DataSet.code == DATASET_CODE))
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {DATASET_CODE}")
    artifact = _artifact_row(
        session,
        dataset=dataset,
        claim=claim,
        raw=raw,
    )

    session.execute(
        delete(FnsTaxDebtQuarantineRecord).where(
            FnsTaxDebtQuarantineRecord.artifact_id == artifact.id
        )
    )
    for invalid in staged["quarantine"]:
        session.add(
            FnsTaxDebtQuarantineRecord(
                dataset_id=dataset.id,
                artifact_id=artifact.id,
                source_member=invalid["source_member"],
                source_ordinal=int(invalid["source_ordinal"]),
                raw_hash=invalid["raw_hash"],
                reason_codes=list(invalid["reason_codes"]),
                raw_payload=invalid["raw_payload"],
            )
        )

    entries = tuple(_restore_entry(value) for value in staged["entries"])
    candidates = _identity_candidates(session, {entry["inn"] for entry in entries})
    matched = 0
    unmatched = 0
    conflicts = 0
    database_duplicates = 0
    published = 0
    data_dates: list[date] = []

    for values in entries:
        match = resolve_inn_match(values["inn"], candidates)
        existing_record = session.scalar(
            select(FnsTaxDebtNormalizedRecord).where(
                FnsTaxDebtNormalizedRecord.artifact_id == artifact.id,
                FnsTaxDebtNormalizedRecord.record_hash == values["record_hash"],
            )
        )
        if existing_record is None:
            normalized = FnsTaxDebtNormalizedRecord(
                dataset_id=dataset.id,
                artifact_id=artifact.id,
                company_id=match.company_id,
                source_member=values["source_member"],
                source_ordinal=int(values["source_ordinal"]),
                inn=values["inn"],
                company_name=values.get("company_name"),
                source_document_id=values.get("source_document_id"),
                document_date=values["document_date"],
                data_date=values["data_date"],
                total_arrears=values["total_arrears"],
                total_penalties=values["total_penalties"],
                total_fines=values["total_fines"],
                total_debt=values["total_debt"],
                items=_json_safe(values["items"]),
                record_hash=values["record_hash"],
                normalization_version=NORMALIZATION_VERSION,
                validation_state="valid",
                match_state=match.state,
                match_method=match.method,
                limitation_states=list(LIMITATION_STATES),
            )
            session.add(normalized)
            session.flush()
        else:
            normalized = existing_record
            database_duplicates += 1
            if (
                normalized.match_state != match.state
                or normalized.company_id != match.company_id
                or normalized.match_method != match.method
            ):
                normalized.match_state = match.state
                normalized.company_id = match.company_id
                normalized.match_method = match.method

        if match.state == "unmatched":
            unmatched += 1
            continue
        if match.state == "conflict":
            conflicts += 1
            continue
        matched += 1
        data_dates.append(values["data_date"])
        provenance = build_fact_provenance(
            values,
            artifact=artifact,
            run_id=claim.run_id,
            match_method=match.method or "inn_exact",
        )
        source_reference = (
            f"{artifact.artifact_reference}#record={values['record_hash']}"
        )
        snapshot = session.scalar(
            select(CompanyTaxDebtSnapshot).where(
                CompanyTaxDebtSnapshot.company_id == match.company_id,
                CompanyTaxDebtSnapshot.dataset_id == dataset.id,
                CompanyTaxDebtSnapshot.data_date == values["data_date"],
            )
        )
        if snapshot is None:
            snapshot = CompanyTaxDebtSnapshot(
                company_id=match.company_id,
                dataset_id=dataset.id,
                data_date=values["data_date"],
            )
            session.add(snapshot)
            session.flush()
        snapshot.normalized_record_id = normalized.id
        snapshot.fact_code = TAX_DEBT_FACT_CODE
        snapshot.document_date = values["document_date"]
        snapshot.source_document_id = values.get("source_document_id")
        snapshot.total_arrears = values["total_arrears"]
        snapshot.total_penalties = values["total_penalties"]
        snapshot.total_fines = values["total_fines"]
        snapshot.total_debt = values["total_debt"]
        snapshot.item_count = len(values["items"])
        snapshot.source_reference = source_reference
        snapshot.provenance = provenance
        snapshot.limitation_states = list(LIMITATION_STATES)
        snapshot.retrieved_at = artifact.retrieved_at
        session.execute(
            delete(CompanyTaxDebtItem).where(
                CompanyTaxDebtItem.snapshot_id == snapshot.id
            )
        )
        for item in values["items"]:
            session.add(
                CompanyTaxDebtItem(
                    snapshot_id=snapshot.id,
                    tax_name=item["tax_name"],
                    arrears=item["arrears"],
                    penalties=item["penalties"],
                    fines=item["fines"],
                    total=item["total"],
                )
            )
        published += 1

    coverage = {
        **dict(staged["coverage"]),
        "matched_records": matched,
        "unmatched_records": unmatched,
        "entity_conflicts": conflicts,
        "database_duplicates": database_duplicates,
        "projected_facts": published,
    }
    data_date = max(data_dates) if data_dates else None
    dataset.last_attempt_at = artifact.retrieved_at
    dataset.last_success_at = artifact.retrieved_at
    dataset.source_as_of = artifact.source_as_of
    dataset.retrieved_at = artifact.retrieved_at
    dataset.published_at = datetime.now(timezone.utc)
    dataset.record_count = len(entries)
    dataset.coverage = coverage
    dataset.operational_status = "ready"
    dataset.last_error = None
    dataset.last_error_at = None
    dataset.retry_count = 0
    if data_date is not None:
        dataset.last_data_date = max(
            value for value in (dataset.last_data_date, data_date) if value is not None
        )

    original = result.counters or ExecutionCounters()
    counters = ExecutionCounters(
        records_seen=original.records_seen,
        records_written=len(entries),
        records_rejected=original.records_rejected,
        records_duplicated=original.records_duplicated + database_duplicates,
        records_published=published,
    )
    validation = ValidationResult(
        accepted=True,
        metadata={
            **dict(result.staging_result.validation.metadata),
            "coverage": coverage,
            "fact_code": FACT_CODE,
            "data_date": data_date.isoformat() if data_date else None,
        },
    )
    return replace(
        result,
        staging_result=replace(result.staging_result, validation=validation),
        counters=counters,
    )


def fns_tax_debt_handler(context: HandlerContext) -> HandlerResult:
    """Worker child entrypoint. It never writes normalized/domain DB tables."""

    context.ensure_active(now=datetime.now(timezone.utc))
    metadata = context.schedule_metadata
    required = ("source_path", "artifact_store", "source_as_of", "retrieved_at")
    missing = tuple(field for field in required if not str(metadata.get(field) or "").strip())
    if missing:
        raise InvalidDataError("S02 job metadata missing: " + ", ".join(missing))
    staged = stage_tax_debt_artifact(
        metadata["source_path"],
        artifact_store=metadata["artifact_store"],
        source_as_of=_parse_timestamp(metadata["source_as_of"], "source_as_of"),
        retrieved_at=_parse_timestamp(metadata["retrieved_at"], "retrieved_at"),
        mode=str(metadata.get("mode") or "fixture"),
        expected_sha256=metadata.get("expected_sha256"),
    )
    context.heartbeat()
    parsed = parse_tax_debt_zip(staged.path)
    context.report_counters(
        ExecutionCounters(
            records_seen=int(parsed.coverage["source_records"]),
            records_written=len(parsed.entries),
            records_rejected=len(parsed.quarantine),
            records_duplicated=int(parsed.coverage["identical_duplicates"]),
            records_published=0,
        )
    )
    context.ensure_active(now=datetime.now(timezone.utc))
    staging_path = write_staged_normalization(staged, parsed)
    return HandlerResult(
        raw_artifacts=(
            RawArtifactReference(
                artifact_reference=staged.path.as_uri(),
                checksum=staged.checksum,
                manifest=staged.manifest,
            ),
        ),
        staging_result=StagingResult(
            staging_pointer=staging_path.as_uri(),
            checksum=staged.checksum,
            validation=ValidationResult(
                accepted=True,
                metadata={
                    "parser_version": PARSER_VERSION,
                    "normalization_version": NORMALIZATION_VERSION,
                    "coverage": parsed.coverage,
                },
            ),
            metadata={"replayable": True, "source_id": SOURCE_ID},
        ),
        checksum_metadata={
            "input_sha256": staged.checksum,
            "manifest_sha256": _hash(staged.manifest),
        },
        counters=ExecutionCounters(
            records_seen=int(parsed.coverage["source_records"]),
            records_written=len(parsed.entries),
            records_rejected=len(parsed.quarantine),
            records_duplicated=int(parsed.coverage["identical_duplicates"]),
            records_published=0,
        ),
    )


def register_fns_tax_debt_handler(session, registry: HandlerRegistry):
    return register_handler(
        session,
        registry,
        source_id=SOURCE_ID,
        version=HANDLER_VERSION,
        handler=fns_tax_debt_handler,
        publisher=publish_tax_debt_result,
        approved=True,
        live=False,
        fixture=True,
        metadata={
            "task": "DEV-009",
            "source_contract": FNS_TAX_DEBT_SOURCE_CONTRACT.as_dict(),
        },
    )


def enqueue_fns_tax_debt_fixture_job(
    session,
    *,
    source_path: str | Path,
    artifact_store: str | Path,
    source_as_of: datetime,
    retrieved_at: datetime,
    expected_sha256: str | None = None,
    max_attempts: int = 3,
    timeout_seconds: int = 300,
) -> JobCreation:
    """Create the bounded fixture job; checksum makes reruns idempotent."""

    source_path = Path(source_path).resolve()
    checksum, _ = calculate_sha256(source_path)
    if expected_sha256 is not None and checksum != expected_sha256.lower():
        raise TaxDebtParseError("S02 enqueue checksum mismatch")
    source_as_of = _aware_utc(source_as_of, "source_as_of")
    retrieved_at = _aware_utc(retrieved_at, "retrieved_at")
    return create_job(
        session,
        source_id=SOURCE_ID,
        job_type="fns_tax_debt_fixture",
        handler_version=HANDLER_VERSION,
        idempotency_key=(
            f"{SOURCE_ID}:{checksum}:{source_as_of.isoformat()}:{NORMALIZATION_VERSION}"
        ),
        schedule_metadata={
            "source_path": str(source_path),
            "artifact_store": str(Path(artifact_store).resolve()),
            "source_as_of": source_as_of.isoformat(),
            "retrieved_at": retrieved_at.isoformat(),
            "expected_sha256": expected_sha256 or checksum,
            "mode": "fixture",
        },
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
    )
