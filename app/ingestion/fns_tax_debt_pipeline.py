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
from typing import Any, Mapping, Protocol
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile, is_zipfile

from lxml import etree
from sqlalchemy import delete, func, select

from app.models.company import Company
from app.models.source import DataSet
from app.models.tax_debt import (
    CompanyTaxDebtItem,
    CompanyTaxDebtSnapshot,
    FnsTaxDebtNormalizedRecord,
    FnsTaxDebtPilotState,
    FnsTaxDebtPublicationGeneration,
    FnsTaxDebtQuarantineRecord,
    FnsTaxDebtRawArtifact,
    TAX_DEBT_FACT_CODE,
)
from app.models.worker import (
    WorkerHandlerRegistration,
    WorkerJob,
    WorkerPublicationState,
    WorkerRun,
)
from app.providers.fns_tax_debt_provider import (
    TaxDebtDiscovery,
    TaxDebtOfficialRelease,
    validate_tax_debt_release,
)
from app.sources.fns_tax_debt import (
    BASELINE_COHORT_LIMIT,
    BASELINE_HANDLER_VERSION,
    CONTROLLED_LIVE_HANDLER_VERSION,
    CONTROLLED_LIVE_PILOT_ENABLED,
    DATASET_CODE,
    FACT_CODE,
    FNS_TAX_DEBT_SOURCE_CONTRACT,
    HANDLER_VERSION,
    NORMALIZATION_VERSION,
    OFFICIAL_SOURCE_PAGE,
    PARSER_VERSION,
    PILOT_COHORT_LIMIT,
    PILOT_ENVIRONMENT,
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
    HandlerNotRegisteredError,
    LegalBlockError,
    LeaseLostError,
    SchemaMismatchError,
    TemporaryInfrastructureError,
)
from app.worker.execution import (
    ClaimedExecution,
    JobCreation,
    create_job,
    register_handler,
)
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


class TaxDebtFreshnessError(InvalidDataError):
    pass


class TaxDebtPilotAlertSink(Protocol):
    """Delivery adapter boundary; DEV-010 provides monitoring events only."""

    def deliver(self, event: Mapping[str, Any]) -> None: ...


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


@dataclass(frozen=True)
class ControlledLivePilotConfig:
    enabled: bool = CONTROLLED_LIVE_PILOT_ENABLED
    environment: str = PILOT_ENVIRONMENT
    cohort_inns: frozenset[str] = frozenset()
    handler_version: str = CONTROLLED_LIVE_HANDLER_VERSION

    def validate(self) -> None:
        if not self.enabled:
            raise LegalBlockError("S02 controlled live pilot is disabled")
        if self.environment != PILOT_ENVIRONMENT:
            raise LegalBlockError(
                "S02 controlled live pilot environment marker is invalid"
            )
        if self.handler_version != CONTROLLED_LIVE_HANDLER_VERSION:
            raise LegalBlockError("S02 controlled live handler version is not pinned")
        if not self.cohort_inns:
            raise LegalBlockError("S02 controlled live cohort is empty")
        if len(self.cohort_inns) > PILOT_COHORT_LIMIT:
            raise LegalBlockError(
                f"S02 controlled live cohort exceeds {PILOT_COHORT_LIMIT} legal entities"
            )
        invalid = sorted(
            inn for inn in self.cohort_inns if not is_valid_legal_entity_inn(inn)
        )
        if invalid:
            raise LegalBlockError(
                "S02 controlled live cohort contains invalid legal-entity INN"
            )


@dataclass(frozen=True)
class BaselinePreparationConfig:
    """Exact bounded scope for the explicit generation-0 preparation."""

    cohort_inns: frozenset[str]
    handler_version: str = BASELINE_HANDLER_VERSION

    def validate(self) -> None:
        if not self.cohort_inns:
            raise LegalBlockError("S02 baseline cohort is empty")
        if len(self.cohort_inns) > BASELINE_COHORT_LIMIT:
            raise LegalBlockError(
                f"S02 baseline cohort exceeds {BASELINE_COHORT_LIMIT} legal entities"
            )
        invalid = sorted(
            inn for inn in self.cohort_inns if not is_valid_legal_entity_inn(inn)
        )
        if invalid:
            raise LegalBlockError(
                "S02 baseline cohort contains invalid legal-entity INN"
            )


def release_freshness(
    official_actual_until: date | None,
    *,
    now: datetime,
) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must contain a timezone")
    if official_actual_until is None:
        return "unknown"
    return (
        "stale"
        if now.astimezone(timezone.utc).date() > official_actual_until
        else "current"
    )


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
        raise TemporaryInfrastructureError(
            f"cannot read source artifact: {error}"
        ) from error
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


def _write_or_reuse_raw_manifest(
    path: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist the first RAW observation and reuse it on exact-release replay.

    ``retrieved_at`` is an observation coordinate rather than part of the
    checksum-addressed source identity.  A later approved execution of the
    same official bytes must keep the original immutable RAW manifest instead
    of trying to rewrite that timestamp.  Every source, schema, cohort and
    parser coordinate remains fail-closed.
    """

    payload = (_canonical_json(manifest) + "\n").encode("utf-8")
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return dict(manifest)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RawArtifactImmutabilityError(
                f"immutable S02 RAW manifest cannot be read: {path}"
            ) from error
        requested_identity = {
            key: value for key, value in manifest.items() if key != "retrieved_at"
        }
        existing_identity = {
            key: value for key, value in existing.items() if key != "retrieved_at"
        }
        if (
            requested_identity != existing_identity
            or not str(existing.get("retrieved_at") or "").strip()
        ):
            raise RawArtifactImmutabilityError(
                f"immutable artifact member differs: {path}"
            )
        return existing


def stage_tax_debt_xsd(
    xsd_path: str | Path,
    *,
    artifact_store: str | Path,
    expected_sha256: str,
    xsd_url: str,
) -> tuple[Path, str]:
    """Pin the exact discovered XSD by checksum before XML validation."""

    source = Path(xsd_path).resolve()
    checksum, size_bytes = calculate_sha256(source)
    if checksum != expected_sha256.lower():
        raise TaxDebtSchemaError(
            f"S02 XSD checksum mismatch: expected {expected_sha256}, got {checksum}"
        )
    if size_bytes <= 0:
        raise TaxDebtSchemaError("S02 XSD is empty")
    target_dir = Path(artifact_store).resolve() / SOURCE_ID / "xsd" / checksum
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "structure.xsd"
    try:
        payload = source.read_bytes()
        _write_once(target, payload)
        _write_once(
            target_dir / "manifest.json",
            (
                _canonical_json(
                    {
                        "source_id": SOURCE_ID,
                        "kind": "pinned_xsd",
                        "official_xsd_url": xsd_url,
                        "sha256": checksum,
                        "size_bytes": size_bytes,
                    }
                )
                + "\n"
            ).encode("utf-8"),
        )
    except OSError as error:
        raise TemporaryInfrastructureError(
            f"cannot persist S02 XSD: {error}"
        ) from error
    return target, checksum


def stage_tax_debt_artifact(
    source_path: str | Path,
    *,
    artifact_store: str | Path,
    source_as_of: datetime,
    retrieved_at: datetime,
    mode: str = "fixture",
    expected_sha256: str | None = None,
    pilot_enabled: bool = CONTROLLED_LIVE_PILOT_ENABLED,
    pilot_environment: str | None = None,
    cohort_inns: tuple[str, ...] = (),
    discovery_page_url: str | None = None,
    artifact_url: str | None = None,
    xsd_path: str | Path | None = None,
    xsd_url: str | None = None,
    expected_xsd_sha256: str | None = None,
    official_actual_until: date | None = None,
    data_as_of: date | None = None,
) -> StagedTaxDebtArtifact:
    """Retain one caller-supplied official-format ZIP under its checksum."""

    if mode not in {"fixture", "controlled_live"}:
        raise LegalBlockError(f"unsupported S02 ingestion mode: {mode}")
    source_as_of = _aware_utc(source_as_of, "source_as_of")
    retrieved_at = _aware_utc(retrieved_at, "retrieved_at")
    pinned_xsd_path: Path | None = None
    pinned_xsd_sha256: str | None = None
    if mode == "controlled_live":
        ControlledLivePilotConfig(
            enabled=pilot_enabled,
            environment=str(pilot_environment or ""),
            cohort_inns=frozenset(cohort_inns),
        ).validate()
        required_live = {
            "discovery_page_url": discovery_page_url,
            "artifact_url": artifact_url,
            "xsd_path": xsd_path,
            "xsd_url": xsd_url,
            "expected_xsd_sha256": expected_xsd_sha256,
            "official_actual_until": official_actual_until,
            "data_as_of": data_as_of,
        }
        missing_live = sorted(
            field
            for field, value in required_live.items()
            if value is None or value == ""
        )
        if missing_live:
            raise LegalBlockError(
                "S02 controlled live metadata missing: " + ", ".join(missing_live)
            )
        if discovery_page_url != OFFICIAL_SOURCE_PAGE:
            raise LegalBlockError("S02 controlled live discovery page is not pinned")
        assert artifact_url is not None
        assert xsd_url is not None
        assert official_actual_until is not None
        assert data_as_of is not None
        validate_tax_debt_release(
            TaxDebtOfficialRelease(
                discovery_page_url=discovery_page_url,
                artifact_url=artifact_url,
                xsd_url=xsd_url,
                source_as_of=source_as_of,
                data_as_of=data_as_of,
                official_actual_until=official_actual_until,
                metadata={},
            )
        )
        if release_freshness(official_actual_until, now=retrieved_at) == "stale":
            raise TaxDebtFreshnessError("S02 official release is stale")
        assert xsd_path is not None
        assert expected_xsd_sha256 is not None
        pinned_xsd_path, pinned_xsd_sha256 = stage_tax_debt_xsd(
            xsd_path,
            artifact_store=artifact_store,
            expected_sha256=expected_xsd_sha256,
            xsd_url=xsd_url,
        )

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
    if mode == "controlled_live":
        manifest.update(
            {
                "pilot_environment": pilot_environment,
                "cohort_inns": sorted(cohort_inns),
                "cohort_limit": PILOT_COHORT_LIMIT,
                "discovery_page_url": discovery_page_url,
                "artifact_url": artifact_url,
                "xsd_url": xsd_url,
                "xsd_reference": pinned_xsd_path.as_uri() if pinned_xsd_path else None,
                "xsd_sha256": pinned_xsd_sha256,
                "official_actual_until": official_actual_until.isoformat()
                if official_actual_until
                else None,
                "data_as_of": data_as_of.isoformat() if data_as_of else None,
            }
        )

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
                with (
                    source.open("rb") as input_stream,
                    target.open("xb") as output_stream,
                ):
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
        manifest = _write_or_reuse_raw_manifest(manifest_path, manifest)
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
    source_document_id = str(raw["document"].get("ИдДок") or "").strip()
    raw_document_date = str(raw["document"].get("ДатаДок") or "").strip()
    taxpayer = raw["taxpayer"] or {}
    inn = str(taxpayer.get("ИННЮЛ") or taxpayer.get("ИНН") or "").strip()
    data_date = _parse_date(document.attrib.get("ДатаСост"))
    document_date = _parse_date(raw_document_date)
    reasons: list[str] = []
    if not source_document_id:
        reasons.append("missing_document_id")
    if not raw_document_date:
        reasons.append("missing_document_date")
    elif document_date is None:
        reasons.append("invalid_document_date")
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
        if not str(raw_item.get("ОбщСумНедоим") or "").strip():
            reasons.append("missing_total_debt")
            continue
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
        "source_document_id": source_document_id,
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


def _load_xml_schema(path: str | Path) -> etree.XMLSchema:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        document = etree.parse(str(Path(path).resolve()), parser)
        return etree.XMLSchema(document)
    except (OSError, etree.XMLSyntaxError, etree.XMLSchemaParseError) as error:
        raise TaxDebtSchemaError(f"cannot load pinned S02 XSD: {error}") from error


def parse_tax_debt_zip(
    path: str | Path,
    *,
    xsd_path: str | Path | None = None,
    cohort_inns: frozenset[str] | None = None,
) -> ParsedTaxDebtArtifact:
    """Deterministically parse the supported FNS ZIP/XML snapshot structure."""

    artifact = Path(path)
    if artifact.suffix.lower() != ".zip" or not is_zipfile(artifact):
        raise TaxDebtSchemaError("S02 parser expected a ZIP container")
    candidates: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    source_records = 0
    schema_versions: set[str] = set()
    xml_members = 0
    schema = _load_xml_schema(xsd_path) if xsd_path is not None else None
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
                    xml_bytes = archive.read(member)
                    if schema is not None:
                        secure_parser = etree.XMLParser(
                            resolve_entities=False,
                            no_network=True,
                            load_dtd=False,
                            huge_tree=False,
                        )
                        schema_document = etree.fromstring(xml_bytes, secure_parser)
                        if not schema.validate(schema_document):
                            detail = schema.error_log.last_error
                            raise TaxDebtSchemaError(
                                f"S02 XML member {member} failed pinned XSD validation: "
                                f"{detail.message if detail is not None else 'unknown schema error'}"
                            )
                    root = ET.fromstring(xml_bytes)
                except TaxDebtSchemaError:
                    raise
                except (
                    ET.ParseError,
                    etree.XMLSyntaxError,
                    OSError,
                    RuntimeError,
                ) as error:
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
                documents = [
                    node for node in root if local_name(node.tag) == "Документ"
                ]
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
                    if cohort_inns is not None:
                        taxpayer = next(
                            (
                                node
                                for node in document
                                if local_name(node.tag) == "СведНП"
                            ),
                            None,
                        )
                        inn = str(
                            (taxpayer.attrib if taxpayer is not None else {}).get(
                                "ИННЮЛ"
                            )
                            or (taxpayer.attrib if taxpayer is not None else {}).get(
                                "ИНН"
                            )
                            or ""
                        ).strip()
                        if inn not in cohort_inns:
                            continue
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
        "cohort_records": len(candidates) + len(quarantine)
        if cohort_inns is not None
        else source_records,
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
        raise InvalidDataError(
            f"cannot replay S02 staged normalization: {error}"
        ) from error
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


def _identity_candidates(
    session, inns: set[str]
) -> dict[str, tuple[tuple[int, str | None], ...]]:
    grouped: dict[str, list[tuple[int, str | None]]] = defaultdict(list)
    if inns:
        for company_id, inn, entity_type in session.execute(
            select(Company.id, Company.inn, Company.entity_type).where(
                Company.inn.in_(inns)
            )
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


def _dataset_metadata(dataset: DataSet) -> dict[str, Any]:
    """Capture the exact shared dataset state that predates the pilot scope."""

    result: dict[str, Any] = {}
    for field in (
        "last_attempt_at",
        "last_success_at",
        "last_data_date",
        "source_as_of",
        "retrieved_at",
        "checked_at",
        "published_at",
        "record_count",
        "coverage",
        "operational_status",
        "last_error",
        "last_error_at",
        "retry_count",
        "next_retry_at",
        "next_expected_update_at",
        "auto_update_status",
    ):
        value = getattr(dataset, field)
        result[field] = (
            value.astimezone(timezone.utc).isoformat()
            if isinstance(value, datetime) and value.tzinfo is not None
            else _json_safe(value)
        )
    return result


def _restore_dataset_metadata(dataset: DataSet, metadata: Mapping[str, Any]) -> None:
    datetime_fields = {
        "last_attempt_at",
        "last_success_at",
        "source_as_of",
        "retrieved_at",
        "checked_at",
        "published_at",
        "last_error_at",
        "next_retry_at",
        "next_expected_update_at",
    }
    for field, value in metadata.items():
        if field == "last_data_date":
            value = _parse_date(value)
        elif field in datetime_fields and value is not None:
            value = _parse_timestamp(value, field)
        setattr(dataset, field, value)


def _capture_baseline_generation(
    session,
    *,
    dataset: DataSet,
    pilot: FnsTaxDebtPilotState,
) -> FnsTaxDebtPublicationGeneration:
    baseline = session.scalar(
        select(FnsTaxDebtPublicationGeneration).where(
            FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
            FnsTaxDebtPublicationGeneration.generation == 0,
        )
    )
    if baseline is not None:
        return baseline

    pointer = session.get(WorkerPublicationState, SOURCE_ID, with_for_update=True)
    if (
        pointer is None
        or pointer.active_pointer is None
        or pointer.published_by_run_id is None
    ):
        raise LegalBlockError("S02 controlled live pilot requires an existing baseline")
    pointer_validation = dict(pointer.validation_metadata or {})
    validation = dict(pointer_validation.get("validation") or {})
    raw_pointer = str(validation.get("raw_pointer") or "")
    checksum = str(pointer_validation.get("checksum") or "")
    artifact = session.scalar(
        select(FnsTaxDebtRawArtifact).where(
            FnsTaxDebtRawArtifact.dataset_id == dataset.id,
            (FnsTaxDebtRawArtifact.artifact_reference == raw_pointer)
            if raw_pointer
            else (FnsTaxDebtRawArtifact.sha256 == checksum),
        )
    )
    if artifact is None:
        raise LegalBlockError("S02 baseline RAW artifact is unavailable")
    baseline_actual_until = _parse_date(
        dict(dataset.coverage or {}).get("official_actual_until")
    )
    if (
        dataset.source_as_of is None
        or dataset.last_data_date is None
        or dataset.retrieved_at is None
        or baseline_actual_until is None
    ):
        raise LegalBlockError("S02 baseline freshness metadata is incomplete")
    run = session.get(WorkerRun, pointer.published_by_run_id)
    counters = (
        {
            "records_seen": run.records_seen,
            "records_written": run.records_written,
            "records_rejected": run.records_rejected,
            "records_duplicated": run.records_duplicated,
            "records_published": run.records_published,
        }
        if run is not None
        else {}
    )
    baseline = FnsTaxDebtPublicationGeneration(
        dataset_id=dataset.id,
        artifact_id=artifact.id,
        worker_run_id=pointer.published_by_run_id,
        generation=0,
        publication_scope="baseline",
        status="baseline",
        staging_pointer=pointer.active_pointer,
        raw_pointer=artifact.artifact_reference,
        checksum=artifact.sha256,
        source_as_of=dataset.source_as_of,
        retrieved_at=dataset.retrieved_at,
        official_actual_until=baseline_actual_until,
        last_data_date=dataset.last_data_date,
        record_count=int(dataset.record_count or 0),
        coverage=dict(dataset.coverage or {}),
        counters=counters,
        validation_metadata=validation,
        dataset_metadata=_dataset_metadata(dataset),
        published_at=dataset.published_at or pointer.updated_at,
    )
    session.add(baseline)
    session.flush()
    pilot.baseline_generation = 0
    pilot.baseline_data_date = dataset.last_data_date
    pilot.normalized_generation = 0
    pilot.fact_generation = 0
    pilot.query_generation = 0
    pilot.active_raw_pointer = artifact.artifact_reference
    pilot.active_checksum = artifact.sha256
    pilot.active_source_as_of = dataset.source_as_of
    pilot.active_retrieved_at = dataset.retrieved_at
    return baseline


def _controlled_live_generation(
    session,
    *,
    dataset: DataSet,
    artifact: FnsTaxDebtRawArtifact,
    claim: ClaimedExecution,
    raw: RawArtifactReference,
) -> tuple[int, FnsTaxDebtPilotState]:
    manifest = dict(raw.manifest)
    if manifest.get("pilot_environment") != PILOT_ENVIRONMENT:
        raise LegalBlockError("S02 publication has no valid pilot environment marker")
    cohort = frozenset(str(value) for value in manifest.get("cohort_inns") or ())
    ControlledLivePilotConfig(enabled=True, cohort_inns=cohort).validate()
    approval = session.get(
        WorkerHandlerRegistration,
        (SOURCE_ID, CONTROLLED_LIVE_HANDLER_VERSION),
    )
    approval_metadata = dict(approval.metadata_json or {}) if approval else {}
    if (
        approval is None
        or not approval.approved
        or not approval.enabled
        or approval_metadata.get("pilot_environment") != PILOT_ENVIRONMENT
        or approval_metadata.get("mode") != "controlled_live"
    ):
        raise HandlerNotRegisteredError(
            "S02 controlled live registry approval is inactive"
        )
    if claim.handler_version != CONTROLLED_LIVE_HANDLER_VERSION:
        raise LegalBlockError(
            "S02 controlled live publication used an unpinned handler"
        )

    existing = session.scalar(
        select(FnsTaxDebtPublicationGeneration).where(
            FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
            FnsTaxDebtPublicationGeneration.artifact_id == artifact.id,
            FnsTaxDebtPublicationGeneration.publication_scope == "pilot",
        )
    )
    if existing is not None:
        raise InvalidDataError("S02 artifact already has a publication generation")

    pilot = session.scalar(
        select(FnsTaxDebtPilotState)
        .where(FnsTaxDebtPilotState.source_id == SOURCE_ID)
        .with_for_update()
    )
    if pilot is None:
        pilot = FnsTaxDebtPilotState(
            source_id=SOURCE_ID,
            dataset_id=dataset.id,
            pilot_environment=PILOT_ENVIRONMENT,
            enabled=True,
            cohort_inns=sorted(cohort),
            generation=0,
            baseline_generation=0,
            normalized_generation=0,
            fact_generation=0,
            query_generation=0,
            counters={},
            freshness="unknown",
            errors=[],
        )
        session.add(pilot)
        session.flush()
    elif not pilot.enabled or pilot.pilot_environment != PILOT_ENVIRONMENT:
        raise LegalBlockError("S02 controlled live pilot state is disabled")
    elif frozenset(str(value) for value in (pilot.cohort_inns or ())) != cohort:
        raise LegalBlockError(
            "S02 controlled live cohort differs from approved pilot scope"
        )

    _capture_baseline_generation(session, dataset=dataset, pilot=pilot)

    current_generation = session.scalar(
        select(FnsTaxDebtPublicationGeneration)
        .where(
            FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
            FnsTaxDebtPublicationGeneration.status == "active",
        )
        .with_for_update()
    )
    previous_rollback = session.scalar(
        select(FnsTaxDebtPublicationGeneration)
        .where(
            FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
            FnsTaxDebtPublicationGeneration.status == "rollback",
        )
        .with_for_update()
    )
    if previous_rollback is not None:
        previous_rollback.status = "superseded"
    if current_generation is not None:
        current_generation.status = "rollback"
    next_generation = (
        int(
            session.scalar(
                select(func.max(FnsTaxDebtPublicationGeneration.generation)).where(
                    FnsTaxDebtPublicationGeneration.dataset_id == dataset.id
                )
            )
            or 0
        )
        + 1
    )
    return next_generation, pilot


def _assert_controlled_live_entries(
    entries: tuple[dict[str, Any], ...],
    *,
    manifest: Mapping[str, Any],
) -> None:
    cohort = frozenset(str(value) for value in manifest.get("cohort_inns") or ())
    ControlledLivePilotConfig(enabled=True, cohort_inns=cohort).validate()
    outside = sorted({entry["inn"] for entry in entries} - cohort)
    if outside:
        raise LegalBlockError(
            "S02 publication outside the approved cohort is prohibited"
        )


def _assert_baseline_entries(
    entries: tuple[dict[str, Any], ...],
    *,
    manifest: Mapping[str, Any],
) -> None:
    cohort = frozenset(str(value) for value in manifest.get("cohort_inns") or ())
    BaselinePreparationConfig(cohort_inns=cohort).validate()
    outside = sorted({entry["inn"] for entry in entries} - cohort)
    if outside:
        raise LegalBlockError(
            "S02 baseline publication outside the approved cohort is prohibited"
        )


def publish_tax_debt_result(
    session,
    claim: ClaimedExecution,
    result: HandlerResult,
) -> HandlerResult:
    """Publish normalization and facts in the worker completion transaction."""

    if len(result.raw_artifacts) != 1 or result.staging_result is None:
        raise InvalidDataError(
            "S02 handler result must contain one RAW and one staging result"
        )
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
    ingestion_mode = raw.manifest.get("ingestion_mode")
    baseline = claim.handler_version == BASELINE_HANDLER_VERSION
    controlled_live = ingestion_mode == "controlled_live" and not baseline
    if baseline and ingestion_mode != "controlled_live":
        raise LegalBlockError("S02 baseline requires the verified official pipeline")

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
    publication_generation = 0
    pilot_state: FnsTaxDebtPilotState | None = None
    if controlled_live:
        _assert_controlled_live_entries(entries, manifest=raw.manifest)
        publication_generation, pilot_state = _controlled_live_generation(
            session,
            dataset=dataset,
            artifact=artifact,
            claim=claim,
            raw=raw,
        )
    elif baseline:
        _assert_baseline_entries(entries, manifest=raw.manifest)
        existing_pointer = session.get(WorkerPublicationState, SOURCE_ID)
        existing_generation = session.scalar(
            select(FnsTaxDebtPublicationGeneration).where(
                FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
                FnsTaxDebtPublicationGeneration.generation == 0,
            )
        )
        existing_facts = int(
            session.scalar(
                select(func.count())
                .select_from(CompanyTaxDebtSnapshot)
                .where(CompanyTaxDebtSnapshot.dataset_id == dataset.id)
            )
            or 0
        )
        if (
            existing_pointer is not None
            or existing_generation is not None
            or existing_facts
        ):
            raise LegalBlockError("S02 generation-0 baseline already exists")
    candidates = _identity_candidates(session, {entry["inn"] for entry in entries})
    matched = 0
    unmatched = 0
    conflicts = 0
    database_duplicates = 0
    published = 0
    # The active full-snapshot date belongs to the source artifact, not to the
    # subset of records that happen to match entities in our database.  Derive
    # it before matching so a valid snapshot with zero matches still advances
    # dataset freshness and makes absent companies NOT_FOUND.
    snapshot_data_date = max(
        (values["data_date"] for values in entries),
        default=None,
    )
    if controlled_live or baseline:
        official_data_as_of = _parse_date(raw.manifest.get("data_as_of"))
        if official_data_as_of is None:
            raise InvalidDataError(f"S02 {ingestion_mode} data_as_of is invalid")
        if snapshot_data_date is not None and snapshot_data_date != official_data_as_of:
            raise InvalidDataError(
                f"S02 {ingestion_mode} cohort record date differs from official discovery metadata"
            )
        snapshot_data_date = official_data_as_of

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
                CompanyTaxDebtSnapshot.publication_generation == publication_generation,
            )
        )
        if snapshot is None:
            snapshot = CompanyTaxDebtSnapshot(
                company_id=match.company_id,
                dataset_id=dataset.id,
                data_date=values["data_date"],
                publication_generation=publication_generation,
            )
            session.add(snapshot)
            session.flush()
        baseline_owner = None
        if controlled_live and publication_generation > 0:
            baseline_owner = session.scalar(
                select(CompanyTaxDebtSnapshot.id).where(
                    CompanyTaxDebtSnapshot.normalized_record_id == normalized.id,
                    CompanyTaxDebtSnapshot.publication_generation == 0,
                    CompanyTaxDebtSnapshot.id != snapshot.id,
                )
            )
        # The schema deliberately permits only one provenance owner for a
        # normalized row. Same-release Run A reuses generation-0 normalization;
        # keep that immutable baseline link and let the pilot snapshot rely on
        # its full source_reference/provenance instead of stealing the FK.
        snapshot.normalized_record_id = (
            None if baseline_owner is not None else normalized.id
        )
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
    if controlled_live or baseline:
        coverage.update(
            {
                "publication_scope": "pilot" if controlled_live else "baseline",
                "cohort_size": len(raw.manifest["cohort_inns"]),
                "cohort_inns": list(raw.manifest["cohort_inns"]),
                "official_actual_until": raw.manifest["official_actual_until"],
                "fact_generation": publication_generation,
                "query_generation": publication_generation,
            }
        )
        if controlled_live:
            coverage["pilot_environment"] = PILOT_ENVIRONMENT
    data_date = snapshot_data_date
    # Controlled-live facts have their own publication scope.  The shared
    # dataset row remains the baseline for every company outside the cohort.
    if not controlled_live:
        actual_until = _parse_date(raw.manifest.get("official_actual_until"))
        if baseline and actual_until is None:
            raise TaxDebtFreshnessError("S02 official_actual_until is invalid")
        dataset.last_attempt_at = artifact.retrieved_at
        dataset.last_success_at = artifact.retrieved_at
        dataset.source_as_of = artifact.source_as_of
        dataset.retrieved_at = artifact.retrieved_at
        dataset.checked_at = datetime.now(timezone.utc)
        dataset.published_at = dataset.checked_at
        if baseline:
            dataset.official_actual_until = actual_until
        dataset.record_count = len(entries)
        dataset.coverage = coverage
        dataset.operational_status = "ready"
        dataset.last_error = None
        dataset.last_error_at = None
        dataset.retry_count = 0
        if data_date is not None:
            dataset.last_data_date = data_date

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
            "ingestion_mode": raw.manifest.get("ingestion_mode"),
            "raw_pointer": raw.artifact_reference,
            "fact_generation": publication_generation,
            "query_generation": publication_generation,
        },
    )
    if baseline:
        actual_until = _parse_date(raw.manifest.get("official_actual_until"))
        if actual_until is None:
            raise TaxDebtFreshnessError("S02 official_actual_until is invalid")
        generation_row = FnsTaxDebtPublicationGeneration(
            dataset_id=dataset.id,
            artifact_id=artifact.id,
            worker_run_id=claim.run_id,
            generation=0,
            publication_scope="baseline",
            status="baseline",
            staging_pointer=result.staging_result.staging_pointer,
            raw_pointer=raw.artifact_reference,
            checksum=raw.checksum,
            source_as_of=artifact.source_as_of,
            retrieved_at=artifact.retrieved_at,
            official_actual_until=actual_until,
            last_data_date=data_date,
            record_count=len(entries),
            coverage=coverage,
            counters=counters.as_dict(),
            validation_metadata=validation.metadata,
            dataset_metadata=_dataset_metadata(dataset),
            published_at=dataset.published_at,
        )
        session.add(generation_row)
    if controlled_live:
        assert pilot_state is not None
        actual_until = _parse_date(raw.manifest.get("official_actual_until"))
        if actual_until is None:
            raise TaxDebtFreshnessError("S02 official_actual_until is invalid")
        published_at = datetime.now(timezone.utc)
        generation_row = FnsTaxDebtPublicationGeneration(
            dataset_id=dataset.id,
            artifact_id=artifact.id,
            worker_run_id=claim.run_id,
            generation=publication_generation,
            publication_scope="pilot",
            status="active",
            staging_pointer=result.staging_result.staging_pointer,
            raw_pointer=raw.artifact_reference,
            checksum=raw.checksum,
            source_as_of=artifact.source_as_of,
            retrieved_at=artifact.retrieved_at,
            official_actual_until=actual_until,
            last_data_date=data_date,
            record_count=len(entries),
            coverage=coverage,
            counters=counters.as_dict(),
            validation_metadata=validation.metadata,
            dataset_metadata={
                "last_attempt_at": artifact.retrieved_at.isoformat(),
                "last_success_at": artifact.retrieved_at.isoformat(),
                "last_data_date": data_date.isoformat() if data_date else None,
                "source_as_of": artifact.source_as_of.isoformat(),
                "retrieved_at": artifact.retrieved_at.isoformat(),
                "published_at": published_at.isoformat(),
                "record_count": len(entries),
                "coverage": coverage,
                "operational_status": "ready",
                "last_error": None,
                "last_error_at": None,
                "retry_count": 0,
            },
            published_at=published_at,
        )
        session.add(generation_row)
        pilot_state.dataset_id = dataset.id
        pilot_state.last_success_at = published_at
        pilot_state.active_raw_pointer = raw.artifact_reference
        pilot_state.active_checksum = raw.checksum
        pilot_state.active_source_as_of = artifact.source_as_of
        pilot_state.active_retrieved_at = artifact.retrieved_at
        worker_state = session.get(WorkerPublicationState, SOURCE_ID)
        pilot_state.generation = int(worker_state.generation if worker_state else 0) + 1
        pilot_state.rollback_normalized_generation = pilot_state.normalized_generation
        pilot_state.rollback_fact_generation = pilot_state.fact_generation
        pilot_state.normalized_generation = publication_generation
        pilot_state.fact_generation = publication_generation
        pilot_state.query_generation = publication_generation
        pilot_state.active_data_date = data_date
        pilot_state.counters = counters.as_dict()
        pilot_state.freshness = release_freshness(
            actual_until,
            now=published_at,
        )
        pilot_state.errors = []
        pilot_state.updated_at = published_at
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
    missing = tuple(
        field for field in required if not str(metadata.get(field) or "").strip()
    )
    if missing:
        raise InvalidDataError("S02 job metadata missing: " + ", ".join(missing))
    mode = str(metadata.get("mode") or "fixture")
    official_actual_until = _parse_date(metadata.get("official_actual_until"))
    data_as_of = _parse_date(metadata.get("data_as_of"))
    cohort_inns = tuple(str(value) for value in metadata.get("cohort_inns") or ())
    staged = stage_tax_debt_artifact(
        metadata["source_path"],
        artifact_store=metadata["artifact_store"],
        source_as_of=_parse_timestamp(metadata["source_as_of"], "source_as_of"),
        retrieved_at=_parse_timestamp(metadata["retrieved_at"], "retrieved_at"),
        mode=mode,
        expected_sha256=metadata.get("expected_sha256"),
        pilot_enabled=bool(metadata.get("pilot_enabled", False)),
        pilot_environment=metadata.get("pilot_environment"),
        cohort_inns=cohort_inns,
        discovery_page_url=metadata.get("discovery_page_url"),
        artifact_url=metadata.get("artifact_url"),
        xsd_path=metadata.get("xsd_path"),
        xsd_url=metadata.get("xsd_url"),
        expected_xsd_sha256=metadata.get("expected_xsd_sha256"),
        official_actual_until=official_actual_until,
        data_as_of=data_as_of,
    )
    context.heartbeat()
    parsed = parse_tax_debt_zip(
        staged.path,
        xsd_path=_file_uri_to_path(staged.manifest["xsd_reference"])
        if mode == "controlled_live"
        else None,
        cohort_inns=frozenset(cohort_inns) if mode == "controlled_live" else None,
    )
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


def _cohort_sha256(cohort_inns: frozenset[str]) -> str:
    return sha256(
        "".join(f"{inn}\n" for inn in sorted(cohort_inns)).encode("ascii")
    ).hexdigest()


def approve_fns_tax_debt_baseline_handler(
    session,
    *,
    approved_by: str,
    approved_at: datetime,
    config: BaselinePreparationConfig,
    artifact_sha256: str,
    xsd_sha256: str,
) -> WorkerHandlerRegistration:
    """Durably approve one exact cohort and official package for generation 0."""

    config.validate()
    if not approved_by.strip():
        raise ValueError("approved_by is required")
    approved_at = _aware_utc(approved_at, "approved_at")
    if len(artifact_sha256) != 64 or len(xsd_sha256) != 64:
        raise ValueError("baseline artifact and XSD checksums must be SHA-256")
    metadata = {
        "task": "S02-baseline-bootstrap",
        "mode": "official_baseline",
        "publication_scope": "baseline",
        "handler_version_pin": config.handler_version,
        "cohort_limit": BASELINE_COHORT_LIMIT,
        "cohort_sha256": _cohort_sha256(config.cohort_inns),
        "artifact_sha256": artifact_sha256.lower(),
        "xsd_sha256": xsd_sha256.lower(),
        "approved_by": approved_by.strip(),
        "approved_at": approved_at.isoformat(),
        "mass_ingestion_enabled": False,
    }
    record = session.get(
        WorkerHandlerRegistration,
        (SOURCE_ID, config.handler_version),
    )
    if record is None:
        record = WorkerHandlerRegistration(
            source_id=SOURCE_ID,
            handler_version=config.handler_version,
            approved=True,
            enabled=True,
            live_mode=False,
            metadata_json=metadata,
        )
        session.add(record)
    else:
        record.approved = True
        record.enabled = True
        record.live_mode = False
        record.metadata_json = metadata
    session.flush()
    return record


def register_fns_tax_debt_baseline_handler(
    session,
    registry: HandlerRegistry,
):
    """Register the explicit generation-0 handler after durable approval."""

    approval = session.get(
        WorkerHandlerRegistration,
        (SOURCE_ID, BASELINE_HANDLER_VERSION),
    )
    metadata = dict(approval.metadata_json or {}) if approval is not None else {}
    if (
        approval is None
        or not approval.approved
        or not approval.enabled
        or approval.live_mode
        or metadata.get("mode") != "official_baseline"
        or metadata.get("publication_scope") != "baseline"
        or metadata.get("handler_version_pin") != BASELINE_HANDLER_VERSION
    ):
        raise HandlerNotRegisteredError(
            "S02 baseline handler requires explicit durable registry approval"
        )
    return register_handler(
        session,
        registry,
        source_id=SOURCE_ID,
        version=BASELINE_HANDLER_VERSION,
        handler=fns_tax_debt_handler,
        publisher=publish_tax_debt_result,
        approved=True,
        live=False,
        fixture=False,
        metadata=metadata,
    )


def approve_fns_tax_debt_controlled_live_handler(
    session,
    *,
    approved_by: str,
    approved_at: datetime,
) -> WorkerHandlerRegistration:
    """Explicit durable approval action; no approval row exists by default."""

    if not approved_by.strip():
        raise ValueError("approved_by is required")
    approved_at = _aware_utc(approved_at, "approved_at")
    record = session.get(
        WorkerHandlerRegistration,
        (SOURCE_ID, CONTROLLED_LIVE_HANDLER_VERSION),
    )
    metadata = {
        "task": "DEV-010",
        "mode": "controlled_live",
        "pilot_environment": PILOT_ENVIRONMENT,
        "handler_version_pin": CONTROLLED_LIVE_HANDLER_VERSION,
        "cohort_limit": PILOT_COHORT_LIMIT,
        "approved_by": approved_by.strip(),
        "approved_at": approved_at.isoformat(),
        "mass_ingestion_enabled": False,
    }
    if record is None:
        record = WorkerHandlerRegistration(
            source_id=SOURCE_ID,
            handler_version=CONTROLLED_LIVE_HANDLER_VERSION,
            approved=True,
            enabled=True,
            live_mode=False,
            metadata_json=metadata,
        )
        session.add(record)
    else:
        record.approved = True
        record.enabled = True
        record.live_mode = False
        record.metadata_json = metadata
    session.flush()
    return record


def register_fns_tax_debt_controlled_live_handler(
    session,
    registry: HandlerRegistry,
):
    """Register only a pre-approved, version-pinned pilot handler."""

    approval = session.get(
        WorkerHandlerRegistration,
        (SOURCE_ID, CONTROLLED_LIVE_HANDLER_VERSION),
    )
    metadata = dict(approval.metadata_json or {}) if approval is not None else {}
    if (
        approval is None
        or not approval.approved
        or not approval.enabled
        or approval.live_mode
        or metadata.get("mode") != "controlled_live"
        or metadata.get("pilot_environment") != PILOT_ENVIRONMENT
        or metadata.get("handler_version_pin") != CONTROLLED_LIVE_HANDLER_VERSION
    ):
        raise HandlerNotRegisteredError(
            "S02 controlled live handler requires explicit durable registry approval"
        )
    return register_handler(
        session,
        registry,
        source_id=SOURCE_ID,
        version=CONTROLLED_LIVE_HANDLER_VERSION,
        handler=fns_tax_debt_handler,
        publisher=publish_tax_debt_result,
        approved=True,
        live=False,
        fixture=False,
        metadata=metadata,
    )


def record_tax_debt_discovery(
    session,
    discovery: TaxDebtDiscovery,
    *,
    config: ControlledLivePilotConfig,
    artifact_checksum: str,
) -> tuple[FnsTaxDebtPilotState, bool]:
    config.validate()
    validate_tax_debt_release(discovery.release)
    if len(artifact_checksum) != 64:
        raise ValueError("artifact_checksum must be SHA-256")
    dataset = session.scalar(select(DataSet).where(DataSet.code == DATASET_CODE))
    state = session.scalar(
        select(FnsTaxDebtPilotState)
        .where(FnsTaxDebtPilotState.source_id == SOURCE_ID)
        .with_for_update()
    )
    if state is None:
        state = FnsTaxDebtPilotState(
            source_id=SOURCE_ID,
            dataset_id=dataset.id if dataset is not None else None,
            pilot_environment=PILOT_ENVIRONMENT,
            enabled=True,
            cohort_inns=sorted(config.cohort_inns),
            generation=0,
            baseline_generation=0,
            normalized_generation=0,
            fact_generation=0,
            query_generation=0,
            counters={},
            freshness="unknown",
            errors=[],
        )
        session.add(state)
    elif (
        state.cohort_inns
        and frozenset(str(value) for value in state.cohort_inns) != config.cohort_inns
    ):
        raise LegalBlockError("S02 discovery cohort differs from approved pilot scope")
    previous = (
        state.discovered_artifact_url,
        state.discovered_xsd_url,
        state.discovered_source_as_of,
        state.official_actual_until,
        state.discovered_checksum,
    )
    current = (
        discovery.release.artifact_url,
        discovery.release.xsd_url,
        discovery.release.source_as_of,
        discovery.release.official_actual_until,
        artifact_checksum.lower(),
    )
    changed = previous != current
    state.dataset_id = dataset.id if dataset is not None else state.dataset_id
    state.enabled = True
    state.cohort_inns = sorted(config.cohort_inns)
    state.last_discovery_at = discovery.discovered_at
    state.discovered_artifact_url = discovery.release.artifact_url
    state.discovered_xsd_url = discovery.release.xsd_url
    state.discovered_checksum = artifact_checksum.lower()
    state.discovered_source_as_of = discovery.release.source_as_of
    state.official_actual_until = discovery.release.official_actual_until
    state.freshness = release_freshness(
        discovery.release.official_actual_until,
        now=discovery.discovered_at,
    )
    state.updated_at = discovery.discovered_at
    session.flush()
    return state, changed


def enqueue_fns_tax_debt_controlled_live_job(
    session,
    *,
    source_path: str | Path,
    xsd_path: str | Path,
    artifact_store: str | Path,
    discovery: TaxDebtDiscovery,
    config: ControlledLivePilotConfig,
    retrieved_at: datetime,
    expected_sha256: str | None = None,
    expected_xsd_sha256: str | None = None,
    max_attempts: int = 3,
    timeout_seconds: int = 300,
) -> JobCreation:
    """Create one exact-artifact pilot job after discovery and registry approval."""

    config.validate()
    validate_tax_debt_release(discovery.release)
    retrieved_at = _aware_utc(retrieved_at, "retrieved_at")
    if (
        release_freshness(
            discovery.release.official_actual_until,
            now=retrieved_at,
        )
        == "stale"
    ):
        raise TaxDebtFreshnessError("S02 official release is stale")
    approval = session.get(
        WorkerHandlerRegistration,
        (SOURCE_ID, config.handler_version),
    )
    approval_metadata = dict(approval.metadata_json or {}) if approval else {}
    if (
        approval is None
        or not approval.approved
        or not approval.enabled
        or approval.live_mode
        or approval_metadata.get("mode") != "controlled_live"
        or approval_metadata.get("pilot_environment") != config.environment
        or approval_metadata.get("handler_version_pin") != config.handler_version
    ):
        raise HandlerNotRegisteredError(
            "S02 controlled live job requires explicit durable registry approval"
        )

    source_path = Path(source_path).resolve()
    xsd_path = Path(xsd_path).resolve()
    checksum, _ = calculate_sha256(source_path)
    xsd_checksum, _ = calculate_sha256(xsd_path)
    if expected_sha256 is not None and checksum != expected_sha256.lower():
        raise TaxDebtParseError("S02 controlled live enqueue checksum mismatch")
    if expected_xsd_sha256 is not None and xsd_checksum != expected_xsd_sha256.lower():
        raise TaxDebtSchemaError("S02 controlled live enqueue XSD checksum mismatch")

    record_tax_debt_discovery(
        session,
        discovery,
        config=config,
        artifact_checksum=checksum,
    )
    return create_job(
        session,
        source_id=SOURCE_ID,
        job_type="fns_tax_debt_controlled_live",
        handler_version=config.handler_version,
        idempotency_key=(
            f"{SOURCE_ID}:controlled_live:{checksum}:{xsd_checksum}:"
            f"{NORMALIZATION_VERSION}"
        ),
        schedule_metadata={
            "source_path": str(source_path),
            "xsd_path": str(xsd_path),
            "artifact_store": str(Path(artifact_store).resolve()),
            "source_as_of": discovery.release.source_as_of.isoformat(),
            "retrieved_at": retrieved_at.isoformat(),
            "expected_sha256": checksum,
            "expected_xsd_sha256": xsd_checksum,
            "mode": "controlled_live",
            "pilot_enabled": True,
            "pilot_environment": config.environment,
            "cohort_inns": sorted(config.cohort_inns),
            "discovery_page_url": discovery.release.discovery_page_url,
            "artifact_url": discovery.release.artifact_url,
            "xsd_url": discovery.release.xsd_url,
            "official_actual_until": discovery.release.official_actual_until.isoformat(),
            "data_as_of": discovery.release.data_as_of.isoformat(),
        },
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
    )


def enqueue_fns_tax_debt_baseline_job(
    session,
    *,
    source_path: str | Path,
    xsd_path: str | Path,
    artifact_store: str | Path,
    discovery: TaxDebtDiscovery,
    config: BaselinePreparationConfig,
    retrieved_at: datetime,
    expected_sha256: str,
    expected_xsd_sha256: str,
    max_attempts: int = 3,
    timeout_seconds: int = 300,
) -> JobCreation:
    """Create the one bounded official generation-0 job idempotently."""

    config.validate()
    validate_tax_debt_release(discovery.release)
    retrieved_at = _aware_utc(retrieved_at, "retrieved_at")
    if (
        release_freshness(discovery.release.official_actual_until, now=retrieved_at)
        == "stale"
    ):
        raise TaxDebtFreshnessError("S02 official release is stale")
    source_path = Path(source_path).resolve()
    xsd_path = Path(xsd_path).resolve()
    checksum, _ = calculate_sha256(source_path)
    xsd_checksum, _ = calculate_sha256(xsd_path)
    if checksum != expected_sha256.lower():
        raise TaxDebtParseError("S02 baseline enqueue checksum mismatch")
    if xsd_checksum != expected_xsd_sha256.lower():
        raise TaxDebtSchemaError("S02 baseline enqueue XSD checksum mismatch")

    approval = session.get(
        WorkerHandlerRegistration,
        (SOURCE_ID, config.handler_version),
    )
    metadata = dict(approval.metadata_json or {}) if approval is not None else {}
    if (
        approval is None
        or not approval.approved
        or not approval.enabled
        or approval.live_mode
        or metadata.get("mode") != "official_baseline"
        or metadata.get("publication_scope") != "baseline"
        or metadata.get("handler_version_pin") != config.handler_version
        or metadata.get("cohort_sha256") != _cohort_sha256(config.cohort_inns)
        or metadata.get("artifact_sha256") != checksum
        or metadata.get("xsd_sha256") != xsd_checksum
    ):
        raise HandlerNotRegisteredError(
            "S02 baseline job requires exact durable package and cohort approval"
        )

    return create_job(
        session,
        source_id=SOURCE_ID,
        job_type="fns_tax_debt_baseline",
        handler_version=config.handler_version,
        idempotency_key=(
            f"{SOURCE_ID}:baseline:{checksum}:{xsd_checksum}:"
            f"{_cohort_sha256(config.cohort_inns)}:{NORMALIZATION_VERSION}"
        ),
        schedule_metadata={
            "source_path": str(source_path),
            "xsd_path": str(xsd_path),
            "artifact_store": str(Path(artifact_store).resolve()),
            "source_as_of": discovery.release.source_as_of.isoformat(),
            "retrieved_at": retrieved_at.isoformat(),
            "expected_sha256": checksum,
            "expected_xsd_sha256": xsd_checksum,
            # Official staging deliberately stays byte-identical to Run A.
            "mode": "controlled_live",
            "job_mode": "official_baseline",
            "pilot_enabled": True,
            "pilot_environment": PILOT_ENVIRONMENT,
            "cohort_inns": sorted(config.cohort_inns),
            "discovery_page_url": discovery.release.discovery_page_url,
            "artifact_url": discovery.release.artifact_url,
            "xsd_url": discovery.release.xsd_url,
            "official_actual_until": discovery.release.official_actual_until.isoformat(),
            "data_as_of": discovery.release.data_as_of.isoformat(),
        },
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
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


def rollback_fns_tax_debt_generation(
    session,
    *,
    expected_generation: int,
    now: datetime | None = None,
) -> FnsTaxDebtPilotState:
    """Atomically restore S02 pointer, dataset metadata and active fact generation."""

    now = _aware_utc(now or datetime.now(timezone.utc), "now")
    pointer = session.scalar(
        select(WorkerPublicationState)
        .where(WorkerPublicationState.source_id == SOURCE_ID)
        .with_for_update()
    )
    pilot = session.scalar(
        select(FnsTaxDebtPilotState)
        .where(FnsTaxDebtPilotState.source_id == SOURCE_ID)
        .with_for_update()
    )
    if pointer is None or pilot is None or pointer.rollback_pointer is None:
        raise LookupError("S02 rollback generation is unavailable")
    if (
        pointer.generation != expected_generation
        or pilot.generation != expected_generation
    ):
        raise LeaseLostError("S02 publication generation changed before rollback")
    if (
        pilot.rollback_fact_generation is None
        or pilot.rollback_normalized_generation is None
    ):
        raise LookupError("S02 rollback fact generation is unavailable")
    current = session.scalar(
        select(FnsTaxDebtPublicationGeneration)
        .where(
            FnsTaxDebtPublicationGeneration.dataset_id == pilot.dataset_id,
            FnsTaxDebtPublicationGeneration.generation == pilot.fact_generation,
        )
        .with_for_update()
    )
    target = session.scalar(
        select(FnsTaxDebtPublicationGeneration)
        .where(
            FnsTaxDebtPublicationGeneration.dataset_id == pilot.dataset_id,
            FnsTaxDebtPublicationGeneration.generation
            == pilot.rollback_fact_generation,
        )
        .with_for_update()
    )
    if current is None or target is None:
        raise LookupError("S02 rollback generation metadata is unavailable")
    if (
        current.status != "active"
        or target.status not in {"baseline", "rollback"}
        or pointer.active_pointer != current.staging_pointer
        or pointer.rollback_pointer != target.staging_pointer
    ):
        raise InvalidDataError("S02 rollback pointers and generation ledger differ")
    dataset = session.get(DataSet, pilot.dataset_id, with_for_update=True)
    if dataset is None:
        raise LookupError("S02 rollback dataset is unavailable")

    pointer.active_pointer, pointer.rollback_pointer = (
        target.staging_pointer,
        current.staging_pointer,
    )
    pointer.generation += 1
    pointer.published_by_run_id = target.worker_run_id
    pointer.validation_metadata = {
        "checksum": target.checksum,
        "validation": target.validation_metadata,
        "staging": {
            "replayable": True,
            "source_id": SOURCE_ID,
            "rollback": True,
        },
    }
    pointer.updated_at = now

    # PostgreSQL partial unique indexes are checked row-by-row, so use a
    # transient non-active state instead of a two-row executemany swap.
    current.status = "superseded"
    session.flush()
    target.status = "active"
    session.flush()
    current.status = "rollback"
    pilot.active_raw_pointer = target.raw_pointer
    pilot.active_checksum = target.checksum
    pilot.active_source_as_of = target.source_as_of
    pilot.active_retrieved_at = target.retrieved_at
    pilot.generation = pointer.generation
    pilot.normalized_generation = target.generation
    pilot.fact_generation = target.generation
    pilot.query_generation = target.generation
    pilot.rollback_normalized_generation = current.generation
    pilot.rollback_fact_generation = current.generation
    pilot.active_data_date = target.last_data_date
    pilot.last_success_at = target.published_at
    pilot.official_actual_until = target.official_actual_until
    pilot.counters = target.counters
    pilot.freshness = release_freshness(target.official_actual_until, now=now)
    pilot.updated_at = now

    if target.publication_scope == "baseline":
        _restore_dataset_metadata(dataset, target.dataset_metadata)
    return pilot


def record_fns_tax_debt_pilot_error(
    session,
    *,
    kind: str,
    message: str,
    observed_at: datetime,
) -> None:
    observed_at = _aware_utc(observed_at, "observed_at")
    state = session.get(FnsTaxDebtPilotState, SOURCE_ID, with_for_update=True)
    if state is None:
        return
    state.errors = [
        *list(state.errors or [])[-19:],
        {"kind": kind, "message": message, "at": observed_at.isoformat()},
    ]
    state.updated_at = observed_at


def get_fns_tax_debt_pilot_monitoring(
    session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only monitoring surface; alert delivery remains an adapter boundary."""

    now = _aware_utc(now or datetime.now(timezone.utc), "now")
    state = session.get(FnsTaxDebtPilotState, SOURCE_ID)
    recent_runs = session.scalars(
        select(WorkerRun)
        .join(WorkerJob, WorkerRun.job_id == WorkerJob.id)
        .where(WorkerJob.source_id == SOURCE_ID)
        .order_by(WorkerRun.started_at.desc())
        .limit(10)
    ).all()
    run_errors = [error for run in recent_runs for error in list(run.errors or [])]
    if state is None:
        return {
            "source_id": SOURCE_ID,
            "pilot_environment": PILOT_ENVIRONMENT,
            "enabled": False,
            "last_discovery": None,
            "last_success": None,
            "active_checksum": None,
            "active_raw_pointer": None,
            "generation": 0,
            "normalized_generation": 0,
            "fact_generation": 0,
            "query_generation": 0,
            "data_date": None,
            "source_as_of": None,
            "retrieved_at": None,
            "counters": {},
            "freshness": "unknown",
            "errors": run_errors,
        }
    return {
        "source_id": SOURCE_ID,
        "pilot_environment": state.pilot_environment,
        "enabled": state.enabled,
        "last_discovery": state.last_discovery_at,
        "last_success": state.last_success_at,
        "active_checksum": state.active_checksum,
        "active_raw_pointer": state.active_raw_pointer,
        "generation": state.generation,
        "normalized_generation": state.normalized_generation,
        "fact_generation": state.fact_generation,
        "query_generation": state.query_generation,
        "data_date": state.active_data_date,
        "source_as_of": state.active_source_as_of,
        "retrieved_at": state.active_retrieved_at,
        "counters": dict(state.counters or {}),
        "freshness": release_freshness(state.official_actual_until, now=now),
        "errors": [*list(state.errors or []), *run_errors],
    }
