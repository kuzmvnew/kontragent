"""Fail-closed operator CLI for the bounded S02 controlled-live pilot.

The module deliberately contains orchestration and validation only. Parsing,
normalization, publication, worker execution, monitoring, rollback, Risk and
Summary remain owned by their accepted application services.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import create_engine, func, inspect, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.ingestion.fns_tax_debt_pipeline import (
    BaselinePreparationConfig,
    ControlledLivePilotConfig,
    approve_fns_tax_debt_baseline_handler,
    approve_fns_tax_debt_controlled_live_handler,
    calculate_sha256,
    controlled_live_semantic_job_identity,
    enqueue_fns_tax_debt_baseline_job,
    enqueue_fns_tax_debt_controlled_live_job,
    get_fns_tax_debt_pilot_monitoring,
    register_fns_tax_debt_baseline_handler,
    register_fns_tax_debt_controlled_live_handler,
    release_freshness,
    rollback_fns_tax_debt_generation,
)
from app.models.company import Company
from app.models.source import DataSet
from app.models.tax_debt import (
    CompanyTaxDebtItem,
    CompanyTaxDebtSnapshot,
    FnsTaxDebtNormalizedRecord,
    FnsTaxDebtPilotState,
    FnsTaxDebtPublicationGeneration,
    FnsTaxDebtRawArtifact,
    TAX_DEBT_FACT_CODE,
)
from app.models.worker import (
    WorkerHandlerRegistration,
    WorkerJob,
    WorkerPublicationState,
    WorkerRawManifest,
    WorkerRun,
)
from app.providers.fns_tax_debt_provider import (
    DATASET_ID,
    FnsTaxDebtOfficialClient,
    TaxDebtDiscovery,
    TaxDebtOfficialRelease,
    validate_tax_debt_release,
)
from app.services.s02_tax_debt_vertical_slice_service import (
    calculate_s02_vertical_slice_from_persisted,
)
from app.sources.fns_tax_debt import (
    BASELINE_HANDLER_VERSION,
    CONTROLLED_LIVE_HANDLER_VERSION,
    CONTROLLED_LIVE_PILOT_ENABLED,
    DATASET_CODE,
    HANDLER_VERSION,
    MASS_INGESTION_ENABLED,
    NORMALIZATION_VERSION,
    OFFICIAL_SOURCE_PAGE,
    PARSER_VERSION,
    PILOT_ENVIRONMENT,
    SOURCE_ID,
)
from app.worker.execution import WorkerExecutor
from app.worker.registry import HandlerRegistry


# The two format constants are accepted parser contract values. Importing them
# from the pipeline would expose private orchestration concerns, so the manifest
# contract names them explicitly here.
EXPECTED_XML_VERSION = "4.01"
EXPECTED_INFORMATION_TYPE = "ОТКРДАННЫЕ6"
APPROVE_TOKEN = "S02_CONTROLLED_LIVE_APPROVE"
BASELINE_APPROVE_TOKEN = "S02_BASELINE_APPROVE"
BASELINE_PREPARE_TOKEN = "S02_BASELINE_PREPARE"
ENQUEUE_TOKEN = "S02_CONTROLLED_LIVE_ENQUEUE"
RETRY_TOKEN = "S02_CONTROLLED_LIVE_RETRY"
RUN_TOKEN = "S02_CONTROLLED_LIVE_RUN"
ROLLBACK_TOKEN = "S02_CONTROLLED_LIVE_ROLLBACK"
DEFAULT_TIMEOUT_SECONDS = 3600
MIN_TIMEOUT_SECONDS = 600
MAX_TIMEOUT_SECONDS = 7200
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
CHECKSUM_RE = re.compile(r"[0-9a-f]{64}\Z")
DOWNLOAD_HOST = "file.nalog.ru"
DOWNLOAD_DIRECTORY = f"/opendata/{DATASET_ID}/"
CURRENT_DATASET_STATUSES = frozenset({"ready", "current"})
BASELINE_JOB_CONTRACTS = frozenset(
    {
        ("fns_tax_debt_baseline", BASELINE_HANDLER_VERSION),
        ("fns_tax_debt_fixture", HANDLER_VERSION),
    }
)
S02_ARTIFACT_JOB_CONTRACTS = frozenset(
    {
        *BASELINE_JOB_CONTRACTS,
        ("fns_tax_debt_controlled_live", CONTROLLED_LIVE_HANDLER_VERSION),
    }
)
DATASET_METADATA_FIELDS = (
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
)

EXIT_CODES = {
    "SOURCE_PACKAGE_CHANGED": 20,
    "SOURCE_PACKAGE_STALE": 21,
    "CHECKSUM_MISMATCH": 22,
    "COHORT_INVALID": 23,
    "MAIN_SHA_MISMATCH": 24,
    "MAIN_SHA_NOT_VERIFIED": 25,
    "BASELINE_NOT_READY": 26,
    "DURABLE_APPROVAL_MISSING": 27,
    "QUEUE_NOT_EXCLUSIVE": 28,
    "JOB_MISMATCH": 29,
    "RUN_FAILED": 30,
    "ROLLBACK_NOT_AVAILABLE": 31,
    "CONFIRMATION_REQUIRED": 32,
    "MANIFEST_INVALID": 33,
    "STATUS_NOT_FOUND": 34,
    "EVIDENCE_NOT_AVAILABLE": 35,
    "OPERATOR_ERROR": 40,
}

PUBLIC_ERROR_MESSAGES = {
    "SOURCE_PACKAGE_CHANGED": "S02 official rediscovery did not match the frozen package",
    "SOURCE_PACKAGE_STALE": "official S02 source package is stale",
    "CHECKSUM_MISMATCH": "artifact bytes could not be verified",
    "COHORT_INVALID": "cohort is invalid",
    "MAIN_SHA_MISMATCH": "runtime Git revision does not match the approved revision",
    "MAIN_SHA_NOT_VERIFIED": "runtime Git revision could not be verified",
    "BASELINE_NOT_READY": "exact persisted S02 baseline chain is not ready",
    "DURABLE_APPROVAL_MISSING": "exact durable S02 approval is missing",
    "QUEUE_NOT_EXCLUSIVE": "the runnable worker queue is not exclusive",
    "JOB_MISMATCH": "the selected worker job does not match the approved S02 job",
    "RUN_FAILED": "S02 worker execution failed",
    "ROLLBACK_NOT_AVAILABLE": "S02 rollback is not available",
    "CONFIRMATION_REQUIRED": "the exact confirmation token is required",
    "MANIFEST_INVALID": "source package is invalid",
    "STATUS_NOT_FOUND": "the selected status record was not found",
    "EVIDENCE_NOT_AVAILABLE": "S02 evidence is not available",
    "OPERATOR_ERROR": "operator command failed",
}

SAFE_DETAIL_FIELDS = frozenset(
    {
        "actual_main_sha",
        "expected_main_sha",
        "actual_sha256",
        "actual_size",
        "kind",
        "current_generation",
        "failed_checks",
        "missing_table_count",
        "raw_artifact_count",
        "baseline_fact_count",
    }
)
SAFE_BASELINE_CHECKS = frozenset(
    {
        "required_tables",
        "dataset_missing",
        "dataset_code",
        "dataset_status",
        "dataset_error_recovery",
        "dataset_source_as_of",
        "dataset_retrieved_at",
        "dataset_last_data_date",
        "dataset_published_at",
        "dataset_record_count",
        "dataset_coverage",
        "dataset_official_actual_until",
        "dataset_time_order",
        "dataset_actual_until_order",
        "publication_state_missing",
        "publication_source",
        "publication_active_pointer",
        "publication_published_by_run",
        "publication_generation",
        "publication_validation_metadata",
        "publication_raw_pointer",
        "publication_checksum",
        "publication_run_missing",
        "publication_run_status",
        "publication_run_finished_at",
        "publication_run_counters",
        "publication_job_missing",
        "publication_job_source",
        "publication_job_type",
        "publication_job_handler",
        "publication_job_status",
        "raw_artifact_missing",
        "raw_artifact_dataset",
        "raw_artifact_pointer",
        "raw_artifact_checksum",
        "raw_artifact_run",
        "raw_artifact_source_as_of",
        "raw_artifact_retrieved_at",
        "baseline_generation_missing",
        "baseline_generation",
        "baseline_scope",
        "baseline_status",
        "baseline_dataset",
        "baseline_artifact",
        "baseline_run",
        "baseline_staging_pointer",
        "baseline_raw_pointer",
        "baseline_checksum",
        "baseline_source_as_of",
        "baseline_retrieved_at",
        "baseline_last_data_date",
        "baseline_official_actual_until",
        "baseline_record_count",
        "baseline_coverage",
        "baseline_counters",
        "baseline_validation_metadata",
        "baseline_dataset_metadata",
        "baseline_published_at",
        "baseline_job_source",
        "baseline_job_type",
        "baseline_job_handler",
        "baseline_job_status",
        "baseline_run_missing",
        "baseline_run_status",
        "baseline_run_finished_at",
        "baseline_artifact_missing",
        "baseline_artifact_dataset",
        "baseline_artifact_owner",
        "baseline_raw_manifest",
        "baseline_cohort",
        "baseline_facts_outside_cohort",
        "baseline_fact_count",
        "active_generation_missing",
        "active_generation",
        "active_scope",
        "active_status",
        "active_dataset",
        "active_run_missing",
        "active_run_status",
        "active_run_finished_at",
        "active_run_counters",
        "active_counters",
        "active_job_missing",
        "active_job_source",
        "active_job_type",
        "active_job_handler",
        "active_job_status",
        "active_artifact_missing",
        "active_artifact_dataset",
        "active_artifact_pointer",
        "active_artifact_checksum",
        "active_artifact_source_as_of",
        "active_artifact_retrieved_at",
        "active_artifact_owner",
        "active_raw_manifest",
        "active_staging_pointer",
        "active_pointer_run",
        "active_pointer_validation",
        "active_pointer_checksum",
        "active_validation_metadata",
        "active_dataset_metadata",
        "active_coverage",
        "active_facts_outside_cohort",
        "active_fact_count",
        "active_fact_integrity",
        "pilot_state_missing",
        "pilot_dataset",
        "pilot_cohort",
        "pilot_generation",
        "pilot_baseline_generation",
        "pilot_normalized_generation",
        "pilot_fact_generation",
        "pilot_query_generation",
        "pilot_raw_pointer",
        "pilot_checksum",
        "pilot_source_as_of",
        "pilot_retrieved_at",
        "pilot_data_date",
        "pilot_baseline_data_date",
        "rollback_pointer",
        "rollback_generation",
        "rollback_status",
        "rollback_fact_generation",
        "rollback_normalized_generation",
    }
)

MANIFEST_FIELDS = frozenset(
    {
        "dataset_id",
        "official_page",
        "artifact_requested_url",
        "artifact_final_url",
        "artifact_filename",
        "artifact_size",
        "artifact_sha256",
        "xsd_requested_url",
        "xsd_final_url",
        "xsd_filename",
        "xsd_size",
        "xsd_sha256",
        "last_modified",
        "data_as_of",
        "source_as_of",
        "retrieved_at",
        "official_actual_until",
        "structure_version",
        "real_xml_version",
        "information_type",
        "validation_result",
        "main_sha",
    }
)


class OperatorError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})

    @property
    def public_message(self) -> str:
        return PUBLIC_ERROR_MESSAGES.get(
            self.code, PUBLIC_ERROR_MESSAGES["OPERATOR_ERROR"]
        )

    def public_details(self) -> dict[str, Any]:
        """Return only fields whose type and vocabulary are safe for CLI JSON."""

        safe: dict[str, Any] = {}
        for key, value in self.details.items():
            if key not in SAFE_DETAIL_FIELDS:
                continue
            if key in {"actual_main_sha", "expected_main_sha"}:
                if isinstance(value, str) and SHA_RE.fullmatch(value):
                    safe[key] = value
            elif key == "actual_sha256":
                if isinstance(value, str) and CHECKSUM_RE.fullmatch(value):
                    safe[key] = value
            elif key == "kind":
                if value in {"artifact", "xsd"}:
                    safe[key] = value
            elif key == "failed_checks":
                if isinstance(value, (list, tuple)) and all(
                    isinstance(item, str) and item in SAFE_BASELINE_CHECKS
                    for item in value
                ):
                    safe[key] = list(value)
            elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                safe[key] = value
        return safe


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime, UUID, Path)):
        return (
            str(value) if not isinstance(value, (date, datetime)) else value.isoformat()
        )
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def emit_json(payload: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            _json_safe(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _parse_date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise OperatorError(
            "MANIFEST_INVALID", f"{field} must be an ISO date"
        ) from error


def _parse_timestamp(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise OperatorError(
            "MANIFEST_INVALID", f"{field} must be an ISO timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OperatorError("MANIFEST_INVALID", f"{field} must contain a timezone")
    return parsed.astimezone(timezone.utc)


def _validation_passed(value: Any) -> bool:
    if isinstance(value, str):
        return value == "PASS"
    return isinstance(value, Mapping) and value.get("status") == "PASS"


def _download_url(value: Any, field: str, filename: str) -> str:
    text = str(value or "")
    parsed = urlparse(text)
    try:
        port = parsed.port
    except ValueError as error:
        raise OperatorError(
            "MANIFEST_INVALID", f"{field} has an invalid port"
        ) from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != DOWNLOAD_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or parsed.path != f"{DOWNLOAD_DIRECTORY}{filename}"
    ):
        raise OperatorError(
            "MANIFEST_INVALID",
            f"{field} is not an approved S02 download coordinate",
        )
    return text


def load_source_package(path: str | Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperatorError(
            "MANIFEST_INVALID", "source package could not be read"
        ) from error
    if not isinstance(raw, dict):
        raise OperatorError("MANIFEST_INVALID", "source package must be a JSON object")
    missing = sorted(MANIFEST_FIELDS - raw.keys())
    unknown = sorted(raw.keys() - MANIFEST_FIELDS)
    if missing or unknown:
        raise OperatorError(
            "MANIFEST_INVALID",
            "source package fields do not match the strict contract",
        )
    if raw["dataset_id"] != DATASET_ID:
        raise OperatorError(
            "MANIFEST_INVALID", "dataset_id is not the accepted S02 dataset"
        )
    if raw["official_page"] != OFFICIAL_SOURCE_PAGE:
        raise OperatorError("MANIFEST_INVALID", "official_page is not pinned")
    for prefix in ("artifact", "xsd"):
        filename = str(raw[f"{prefix}_filename"] or "")
        if not filename or Path(filename).name != filename:
            raise OperatorError("MANIFEST_INVALID", f"{prefix}_filename is invalid")
        for coordinate in ("requested", "final"):
            field = f"{prefix}_{coordinate}_url"
            raw[field] = _download_url(raw[field], field, filename)
        size = raw[f"{prefix}_size"]
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise OperatorError("MANIFEST_INVALID", f"{prefix}_size must be positive")
        checksum = str(raw[f"{prefix}_sha256"] or "").lower()
        if CHECKSUM_RE.fullmatch(checksum) is None:
            raise OperatorError("MANIFEST_INVALID", f"{prefix}_sha256 is invalid")
        raw[f"{prefix}_sha256"] = checksum
    if raw["real_xml_version"] != EXPECTED_XML_VERSION:
        raise OperatorError("MANIFEST_INVALID", "real_xml_version is unsupported")
    if raw["information_type"] != EXPECTED_INFORMATION_TYPE:
        raise OperatorError("MANIFEST_INVALID", "information_type is unsupported")
    if not _validation_passed(raw["validation_result"]):
        raise OperatorError(
            "MANIFEST_INVALID", "source package validation did not PASS"
        )
    if SHA_RE.fullmatch(str(raw["main_sha"] or "").lower()) is None:
        raise OperatorError("MANIFEST_INVALID", "main_sha is not a full Git SHA")
    raw["main_sha"] = str(raw["main_sha"]).lower()
    raw["last_modified"] = _parse_date(raw["last_modified"], "last_modified")
    raw["data_as_of"] = _parse_date(raw["data_as_of"], "data_as_of")
    raw["source_as_of"] = _parse_timestamp(raw["source_as_of"], "source_as_of")
    raw["retrieved_at"] = _parse_timestamp(raw["retrieved_at"], "retrieved_at")
    raw["official_actual_until"] = _parse_date(
        raw["official_actual_until"], "official_actual_until"
    )
    if raw["source_as_of"].date() != raw["last_modified"]:
        raise OperatorError("MANIFEST_INVALID", "source_as_of and last_modified differ")
    structure = str(raw["structure_version"] or "")
    if not structure.isdigit() or len(structure) != 8:
        raise OperatorError("MANIFEST_INVALID", "structure_version is invalid")
    if f"structure-{structure}.zip" not in raw["artifact_filename"]:
        raise OperatorError("MANIFEST_INVALID", "artifact structure version differs")
    if raw["xsd_filename"] != f"structure-{structure}.xsd":
        raise OperatorError("MANIFEST_INVALID", "XSD structure version differs")
    return raw


def source_package_fingerprint(package: Mapping[str, Any]) -> str:
    payload = {
        key: _json_safe(package[key])
        for key in sorted(MANIFEST_FIELDS)
        if key != "validation_result"
    }
    payload["validation_result"] = _json_safe(package["validation_result"])
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def load_cohort(path: str | Path) -> tuple[tuple[str, ...], str]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperatorError("COHORT_INVALID", "cohort could not be read") from error
    values = raw.get("inns") if isinstance(raw, dict) else raw
    if not isinstance(values, list) or any(
        not isinstance(item, str) for item in values
    ):
        raise OperatorError(
            "COHORT_INVALID", 'cohort must be a JSON list or {"inns": [...]} object'
        )
    normalized = [item.strip() for item in values]
    if not normalized or len(normalized) > 100:
        raise OperatorError("COHORT_INVALID", "cohort must contain 1 to 100 INNs")
    if len(set(normalized)) != len(normalized):
        raise OperatorError("COHORT_INVALID", "duplicate INN is forbidden")
    config = ControlledLivePilotConfig(enabled=True, cohort_inns=frozenset(normalized))
    try:
        config.validate()
    except Exception as error:
        raise OperatorError("COHORT_INVALID", "cohort validation failed") from error
    ordered = tuple(sorted(normalized))
    canonical = ("\n".join(ordered) + "\n").encode("ascii")
    return ordered, sha256(canonical).hexdigest()


def resolve_runtime_sha() -> str | None:
    configured = os.getenv("KONTRAGENT_RUNTIME_SHA", "").strip().lower()
    if configured:
        return configured if SHA_RE.fullmatch(configured) else None
    repository = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return None
    value = result.stdout.strip().lower()
    return value if SHA_RE.fullmatch(value) else None


def check_main_sha(*, expected: str, package_sha: str | None = None) -> str:
    expected = expected.strip().lower()
    if SHA_RE.fullmatch(expected) is None:
        raise OperatorError(
            "MAIN_SHA_MISMATCH", "expected main SHA must be a full Git SHA"
        )
    if package_sha is not None and expected != package_sha:
        raise OperatorError(
            "MAIN_SHA_MISMATCH",
            "operator expected SHA differs from source package main_sha",
        )
    actual = resolve_runtime_sha()
    if actual is None:
        raise OperatorError(
            "MAIN_SHA_NOT_VERIFIED", "runtime Git revision is NOT_VERIFIED"
        )
    if actual != expected:
        raise OperatorError(
            "MAIN_SHA_MISMATCH",
            "runtime Git revision differs from expected canonical revision",
            details={"actual_main_sha": actual, "expected_main_sha": expected},
        )
    return actual


def _release_from_package(package: Mapping[str, Any]) -> TaxDebtOfficialRelease:
    release = TaxDebtOfficialRelease(
        discovery_page_url=str(package["official_page"]),
        artifact_url=str(package["artifact_requested_url"]),
        xsd_url=str(package["xsd_requested_url"]),
        source_as_of=package["source_as_of"],
        data_as_of=package["data_as_of"],
        official_actual_until=package["official_actual_until"],
        metadata={"dataset_id": str(package["dataset_id"])},
    )
    try:
        validate_tax_debt_release(release)
    except Exception as error:
        raise OperatorError(
            "MANIFEST_INVALID", "source release metadata is invalid"
        ) from error
    return release


def discover_exact_release(
    package: Mapping[str, Any],
    *,
    now: datetime,
    client: FnsTaxDebtOfficialClient | None = None,
) -> TaxDebtDiscovery:
    frozen = _release_from_package(package)
    try:
        discovery = (client or FnsTaxDebtOfficialClient()).discover(
            discovered_at=now,
            previous=frozen,
        )
    except Exception as error:
        raise OperatorError(
            "SOURCE_PACKAGE_CHANGED", "S02 official rediscovery failed"
        ) from error
    release = discovery.release
    expected = (
        frozen.discovery_page_url,
        frozen.artifact_url,
        frozen.xsd_url,
        frozen.source_as_of,
        frozen.data_as_of,
        frozen.official_actual_until,
    )
    actual = (
        release.discovery_page_url,
        release.artifact_url,
        release.xsd_url,
        release.source_as_of,
        release.data_as_of,
        release.official_actual_until,
    )
    if discovery.changed or actual != expected:
        raise OperatorError(
            "SOURCE_PACKAGE_CHANGED", "official S02 release differs from frozen package"
        )
    return discovery


def verify_local_files(
    package: Mapping[str, Any], artifact: str | Path, xsd: str | Path
) -> tuple[str, str]:
    artifact_path = Path(artifact)
    xsd_path = Path(xsd)
    if artifact_path.name != package["artifact_filename"]:
        raise OperatorError(
            "CHECKSUM_MISMATCH", "artifact filename differs from manifest"
        )
    if xsd_path.name != package["xsd_filename"]:
        raise OperatorError("CHECKSUM_MISMATCH", "XSD filename differs from manifest")
    for kind, path in (("artifact", artifact_path), ("xsd", xsd_path)):
        if not path.is_file():
            raise OperatorError("CHECKSUM_MISMATCH", f"{kind} file does not exist")
        try:
            digest, size = calculate_sha256(path)
        except Exception as error:
            raise OperatorError(
                "CHECKSUM_MISMATCH", f"{kind} bytes could not be verified"
            ) from error
        if digest != package[f"{kind}_sha256"] or size != package[f"{kind}_size"]:
            raise OperatorError(
                "CHECKSUM_MISMATCH",
                f"{kind} bytes differ from source package",
                details={
                    "actual_sha256": digest,
                    "actual_size": size,
                    "kind": kind,
                },
            )
    return str(package["artifact_sha256"]), str(package["xsd_sha256"])


def _runnable_query(now: datetime):
    return (
        select(WorkerJob)
        .where(
            WorkerJob.status.in_(("queued", "retry_scheduled")),
            or_(WorkerJob.next_attempt_at.is_(None), WorkerJob.next_attempt_at <= now),
        )
        .order_by(WorkerJob.created_at, WorkerJob.id)
    )


def runnable_jobs(session: Session, *, now: datetime) -> list[dict[str, Any]]:
    return [
        {
            "job_id": str(job.id),
            "source_id": job.source_id,
            "job_type": job.job_type,
            "handler_version": job.handler_version,
            "status": job.status,
            "next_attempt_at": job.next_attempt_at,
            "created_at": job.created_at,
        }
        for job in session.scalars(_runnable_query(now)).all()
    ]


def _approval(session: Session) -> WorkerHandlerRegistration | None:
    return session.get(
        WorkerHandlerRegistration, (SOURCE_ID, CONTROLLED_LIVE_HANDLER_VERSION)
    )


def require_durable_approval(session: Session) -> WorkerHandlerRegistration:
    approval = _approval(session)
    metadata = dict(approval.metadata_json or {}) if approval is not None else {}
    if (
        approval is None
        or not approval.approved
        or not approval.enabled
        or approval.live_mode
        or metadata.get("mode") != "controlled_live"
        or metadata.get("pilot_environment") != PILOT_ENVIRONMENT
        or metadata.get("handler_version_pin") != CONTROLLED_LIVE_HANDLER_VERSION
        or metadata.get("mass_ingestion_enabled") is not False
    ):
        raise OperatorError(
            "DURABLE_APPROVAL_MISSING", "exact durable S02 approval is missing"
        )
    return approval


def require_baseline_approval(
    session: Session,
    *,
    cohort_sha256: str,
    artifact_sha256: str,
    xsd_sha256: str,
) -> WorkerHandlerRegistration:
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
        or metadata.get("cohort_sha256") != cohort_sha256
        or metadata.get("artifact_sha256") != artifact_sha256
        or metadata.get("xsd_sha256") != xsd_sha256
        or metadata.get("mass_ingestion_enabled") is not False
    ):
        raise OperatorError(
            "DURABLE_APPROVAL_MISSING",
            "exact durable S02 baseline approval is missing",
        )
    return approval


def _dataset_metadata_snapshot(dataset: DataSet) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in DATASET_METADATA_FIELDS:
        value = getattr(dataset, field)
        result[field] = (
            value.astimezone(timezone.utc).isoformat()
            if isinstance(value, datetime) and value.tzinfo is not None
            else _json_safe(value)
        )
    return result


def _worker_run_counters(run: WorkerRun) -> dict[str, int]:
    return {
        "records_seen": run.records_seen,
        "records_written": run.records_written,
        "records_rejected": run.records_rejected,
        "records_duplicated": run.records_duplicated,
        "records_published": run.records_published,
    }


def _usable_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _mapping_dict(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _coverage_actual_until(coverage: Mapping[str, Any]) -> date | None:
    raw_value = coverage.get("official_actual_until")
    try:
        return date.fromisoformat(str(raw_value)) if raw_value else None
    except ValueError:
        return None


def _mapping_contains(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def _metadata_value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, datetime) and isinstance(actual, str):
        try:
            parsed = datetime.fromisoformat(actual)
        except ValueError:
            return False
        return parsed == expected
    if isinstance(expected, date) and isinstance(actual, str):
        try:
            return date.fromisoformat(actual) == expected
        except ValueError:
            return False
    return actual == _json_safe(expected)


def _generation_validation_matches(
    generation: FnsTaxDebtPublicationGeneration,
    *,
    ingestion_mode: str,
) -> bool:
    validation = _mapping_dict(generation.validation_metadata)
    coverage = _mapping_dict(generation.coverage)
    validation_coverage = (
        _mapping_dict(validation.get("coverage")) if validation is not None else None
    )
    if validation is None or coverage is None or validation_coverage is None:
        return False
    return (
        validation.get("raw_pointer") == generation.raw_pointer
        and _mapping_contains(coverage, validation_coverage)
        and validation.get("fact_code") == "tax.debt.amount_as_of_date"
        and validation.get("data_date") == _json_safe(generation.last_data_date)
        and validation.get("ingestion_mode") == ingestion_mode
        and validation.get("fact_generation") == generation.generation
        and validation.get("query_generation") == generation.generation
    )


def _generation_dataset_metadata_matches(
    generation: FnsTaxDebtPublicationGeneration,
    *,
    historical_baseline: bool,
) -> bool:
    metadata = _mapping_dict(generation.dataset_metadata)
    coverage = _mapping_dict(generation.coverage)
    if metadata is None or coverage is None:
        return False
    if historical_baseline and set(metadata) != set(DATASET_METADATA_FIELDS):
        return False
    expected = {
        "last_data_date": generation.last_data_date,
        "source_as_of": generation.source_as_of,
        "retrieved_at": generation.retrieved_at,
        "published_at": generation.published_at,
        "record_count": generation.record_count,
        "coverage": coverage,
        "last_error": None,
        "last_error_at": None,
        "retry_count": 0,
    }
    if not all(
        _metadata_value_matches(metadata.get(key), value)
        for key, value in expected.items()
    ):
        return False
    if metadata.get("operational_status") not in CURRENT_DATASET_STATUSES:
        return False
    if historical_baseline:
        baseline_expected = {
            "last_attempt_at": generation.retrieved_at,
            "last_success_at": generation.retrieved_at,
            "checked_at": generation.published_at,
            "next_retry_at": None,
        }
        return all(
            _metadata_value_matches(metadata.get(key), value)
            for key, value in baseline_expected.items()
        )
    return True


def _generation_coverage_matches(
    generation: FnsTaxDebtPublicationGeneration,
    *,
    cohort: tuple[str, ...],
    require_scope: bool,
) -> bool:
    coverage = _mapping_dict(generation.coverage)
    if not coverage:
        return False
    if not require_scope:
        return True
    covered_cohort = tuple(str(value) for value in coverage.get("cohort_inns") or ())
    return (
        coverage.get("publication_scope") == generation.publication_scope
        and len(covered_cohort) == len(cohort)
        and set(covered_cohort) == set(cohort)
        and coverage.get("cohort_size") == len(cohort)
        and coverage.get("official_actual_until")
        == _json_safe(generation.official_actual_until)
        and coverage.get("fact_generation") == generation.generation
        and coverage.get("query_generation") == generation.generation
    )


def _raw_manifest_matches(
    manifest: WorkerRawManifest | None,
    *,
    run: WorkerRun,
    artifact: FnsTaxDebtRawArtifact,
) -> bool:
    return bool(
        manifest is not None
        and manifest.run_id == run.id
        and manifest.artifact_reference == artifact.artifact_reference
        and manifest.checksum_algorithm == "sha256"
        and manifest.checksum == artifact.sha256
        and manifest.immutable is True
    )


def _normalized_record_hash_matches(record: FnsTaxDebtNormalizedRecord) -> bool:
    payload = {
        "inn": record.inn,
        "company_name": record.company_name,
        "source_document_id": record.source_document_id,
        "document_date": record.document_date,
        "data_date": record.data_date,
        "total_arrears": record.total_arrears,
        "total_penalties": record.total_penalties,
        "total_fines": record.total_fines,
        "total_debt": record.total_debt,
        "items": record.items,
        "normalization_version": record.normalization_version,
        "limitation_states": record.limitation_states,
    }
    canonical = json.dumps(
        _json_safe(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest() == record.record_hash


def _normalized_item_signature(value: Any) -> tuple[Any, ...] | None:
    if not isinstance(value, Mapping):
        return None
    required = {"tax_name", "arrears", "penalties", "fines", "total"}
    if set(value) != required or not isinstance(value.get("tax_name"), str):
        return None
    try:
        amounts = tuple(
            Decimal(str(value[field]))
            for field in ("arrears", "penalties", "fines", "total")
        )
    except (InvalidOperation, TypeError, ValueError):
        return None
    return (value["tax_name"], *amounts)


def _active_fact_integrity_matches(
    session: Session,
    *,
    dataset: DataSet,
    generation: FnsTaxDebtPublicationGeneration,
    artifact: FnsTaxDebtRawArtifact,
    run: WorkerRun,
    cohort: tuple[str, ...],
) -> bool:
    """Validate the complete published fact projection against accepted lineage."""

    facts = session.execute(
        select(CompanyTaxDebtSnapshot, Company)
        .join(Company, Company.id == CompanyTaxDebtSnapshot.company_id)
        .where(
            CompanyTaxDebtSnapshot.dataset_id == dataset.id,
            CompanyTaxDebtSnapshot.publication_generation == generation.generation,
        )
    ).all()
    for snapshot, company in facts:
        provenance = _mapping_dict(snapshot.provenance)
        if provenance is None or company.inn not in cohort:
            return False
        record_hash = provenance.get("record_hash")
        if not isinstance(record_hash, str) or CHECKSUM_RE.fullmatch(record_hash) is None:
            return False
        if snapshot.normalized_record_id is not None:
            normalized = session.get(
                FnsTaxDebtNormalizedRecord, snapshot.normalized_record_id
            )
        else:
            candidates = session.scalars(
                select(FnsTaxDebtNormalizedRecord).where(
                    FnsTaxDebtNormalizedRecord.dataset_id == dataset.id,
                    FnsTaxDebtNormalizedRecord.artifact_id == artifact.id,
                    FnsTaxDebtNormalizedRecord.record_hash == record_hash,
                    FnsTaxDebtNormalizedRecord.company_id == snapshot.company_id,
                    FnsTaxDebtNormalizedRecord.inn == company.inn,
                    FnsTaxDebtNormalizedRecord.data_date == snapshot.data_date,
                )
            ).all()
            if len(candidates) != 1:
                return False
            normalized = candidates[0]
        if normalized is None:
            return False

        expected_source_reference = (
            f"{artifact.artifact_reference}#record={normalized.record_hash}"
        )
        expected_provenance = {
            "source_id": SOURCE_ID,
            "dataset_code": DATASET_CODE,
            "official_source": OFFICIAL_SOURCE_PAGE,
            "artifact_sha256": artifact.sha256,
            "artifact_reference": artifact.artifact_reference,
            "worker_run_id": str(run.id),
            "source_document_id": normalized.source_document_id,
            "source_member": normalized.source_member,
            "record_hash": normalized.record_hash,
            "source_as_of": artifact.source_as_of,
            "retrieved_at": artifact.retrieved_at,
            "parser_version": PARSER_VERSION,
            "normalization_version": NORMALIZATION_VERSION,
            "matching_method": normalized.match_method,
        }
        if set(provenance) != set(expected_provenance) or not all(
            _metadata_value_matches(provenance.get(field), expected)
            for field, expected in expected_provenance.items()
        ):
            return False

        normalized_items = list(normalized.items or ())
        expected_items = [_normalized_item_signature(item) for item in normalized_items]
        if any(item is None for item in expected_items):
            return False
        actual_items = session.scalars(
            select(CompanyTaxDebtItem).where(
                CompanyTaxDebtItem.snapshot_id == snapshot.id
            )
        ).all()
        actual_signatures = [
            (
                item.tax_name,
                item.arrears,
                item.penalties,
                item.fines,
                item.total,
            )
            for item in actual_items
        ]
        if sorted(expected_items) != sorted(actual_signatures):
            return False
        if any(
            item.total != item.arrears + item.penalties + item.fines
            for item in actual_items
        ):
            return False
        if (
            snapshot.item_count != len(normalized_items)
            or snapshot.item_count != len(actual_items)
            or snapshot.total_arrears
            != sum((item.arrears for item in actual_items), Decimal("0.00"))
            or snapshot.total_penalties
            != sum((item.penalties for item in actual_items), Decimal("0.00"))
            or snapshot.total_fines
            != sum((item.fines for item in actual_items), Decimal("0.00"))
            or snapshot.total_debt
            != sum((item.total for item in actual_items), Decimal("0.00"))
        ):
            return False

        if not (
            snapshot.company_id == normalized.company_id
            and normalized.inn == company.inn
            and normalized.dataset_id == dataset.id
            and normalized.artifact_id == artifact.id
            and normalized.validation_state == "valid"
            and normalized.match_state == "matched"
            and normalized.match_method == "inn_exact"
            and normalized.normalization_version == NORMALIZATION_VERSION
            and _normalized_record_hash_matches(normalized)
            and snapshot.dataset_id == dataset.id
            and snapshot.publication_generation == generation.generation
            and snapshot.fact_code == TAX_DEBT_FACT_CODE
            and snapshot.data_date == normalized.data_date
            and snapshot.document_date == normalized.document_date
            and snapshot.source_document_id == normalized.source_document_id
            and snapshot.total_arrears == normalized.total_arrears
            and snapshot.total_penalties == normalized.total_penalties
            and snapshot.total_fines == normalized.total_fines
            and snapshot.total_debt == normalized.total_debt
            and list(snapshot.limitation_states or ())
            == list(normalized.limitation_states or ())
            and snapshot.retrieved_at == artifact.retrieved_at
            and snapshot.source_reference == expected_source_reference
        ):
            return False
    return True


def _dataset_error_recovery_matches(
    session: Session,
    *,
    dataset: DataSet,
    active_generation: FnsTaxDebtPublicationGeneration,
    cohort: tuple[str, ...],
) -> bool:
    """Prove that ``error`` belongs to a later failed, unpublished refresh."""

    if not dataset.last_error or dataset.last_error_at is None:
        return False
    failed_attempt = session.execute(
        select(WorkerRun, WorkerJob)
        .join(WorkerJob, WorkerJob.id == WorkerRun.job_id)
        .where(
            WorkerJob.source_id == SOURCE_ID,
            WorkerJob.job_type == "fns_tax_debt_controlled_live",
            WorkerJob.handler_version == CONTROLLED_LIVE_HANDLER_VERSION,
            WorkerJob.status == "failed",
            WorkerRun.status.in_(("failed", "timed_out", "interrupted")),
            WorkerRun.finished_at == dataset.last_error_at,
            WorkerRun.finished_at > active_generation.published_at,
        )
        .order_by(WorkerRun.attempt_no.desc(), WorkerRun.id.desc())
        .limit(1)
    ).one_or_none()
    if failed_attempt is None:
        return False
    failed_run, failed_job = failed_attempt
    failed_attempt_contract_matches = (
        failed_run.job_id == failed_job.id
        and failed_job.source_id == SOURCE_ID
        and failed_job.job_type == "fns_tax_debt_controlled_live"
        and failed_job.handler_version == CONTROLLED_LIVE_HANDLER_VERSION
        and failed_run.handler_version == failed_job.handler_version
        and failed_run.handler_version == CONTROLLED_LIVE_HANDLER_VERSION
        and failed_job.status == "failed"
        and failed_run.status in {"failed", "timed_out", "interrupted"}
        and failed_run.started_at >= active_generation.published_at
        and failed_run.finished_at is not None
        and failed_run.finished_at > active_generation.published_at
        and failed_run.finished_at == dataset.last_error_at
        and failed_run.records_published == 0
    )
    metadata = _mapping_dict(failed_job.schedule_metadata) or {}
    semantic_fields_are_complete = (
        metadata.get("mode") == "controlled_live"
        and metadata.get("pilot_enabled") is True
        and metadata.get("pilot_environment") == PILOT_ENVIRONMENT
        and tuple(sorted(str(value) for value in metadata.get("cohort_inns") or ()))
        == tuple(sorted(cohort))
        and isinstance(metadata.get("expected_sha256"), str)
        and CHECKSUM_RE.fullmatch(metadata["expected_sha256"]) is not None
        and isinstance(metadata.get("expected_xsd_sha256"), str)
        and CHECKSUM_RE.fullmatch(metadata["expected_xsd_sha256"]) is not None
        and all(
            isinstance(metadata.get(field), str) and bool(metadata[field])
            for field in (
                "source_as_of",
                "data_as_of",
                "official_actual_until",
                "discovery_page_url",
                "artifact_url",
                "xsd_url",
            )
        )
    )
    if (
        not failed_attempt_contract_matches
        or not semantic_fields_are_complete
        or not failed_run.errors
    ):
        return False
    try:
        failed_source_as_of = datetime.fromisoformat(
            metadata["source_as_of"].replace("Z", "+00:00")
        )
        failed_data_as_of = date.fromisoformat(metadata["data_as_of"])
        failed_actual_until = date.fromisoformat(metadata["official_actual_until"])
    except ValueError:
        return False
    if failed_source_as_of.tzinfo is None:
        return False
    stored_semantic_identity = metadata.get("semantic_identity")
    if stored_semantic_identity is not None:
        expected_semantic_identity = controlled_live_semantic_job_identity(
            artifact_sha256=metadata["expected_sha256"],
            xsd_sha256=metadata["expected_xsd_sha256"],
            release=TaxDebtOfficialRelease(
                discovery_page_url=metadata["discovery_page_url"],
                artifact_url=metadata["artifact_url"],
                xsd_url=metadata["xsd_url"],
                source_as_of=failed_source_as_of,
                data_as_of=failed_data_as_of,
                official_actual_until=failed_actual_until,
                metadata={},
            ),
            config=ControlledLivePilotConfig(
                enabled=True, cohort_inns=frozenset(cohort)
            ),
        )
        if stored_semantic_identity != expected_semantic_identity:
            return False
    published_from_failed_run = session.scalar(
        select(FnsTaxDebtPublicationGeneration.id)
        .where(FnsTaxDebtPublicationGeneration.worker_run_id == failed_run.id)
        .limit(1)
    )
    unsafe_queue_entry = session.scalar(
        select(WorkerJob.id)
        .where(WorkerJob.status.in_(("queued", "running", "retry_scheduled")))
        .limit(1)
    )
    return published_from_failed_run is None and unsafe_queue_entry is None


def database_readiness(session: Session, *, cohort: tuple[str, ...]) -> dict[str, Any]:
    table_names = set(inspect(session.get_bind()).get_table_names())
    required_tables = {
        "companies",
        "data_sets",
        "worker_jobs",
        "worker_runs",
        "worker_raw_manifests",
        "worker_handler_registry",
        "worker_publication_state",
        "fns_tax_debt_raw_artifacts",
        "fns_tax_debt_normalized_records",
        "fns_tax_debt_publication_generations",
        "fns_tax_debt_pilot_state",
        "company_tax_debt_snapshots",
    }
    missing_tables = sorted(required_tables - table_names)
    if missing_tables:
        raise OperatorError(
            "BASELINE_NOT_READY",
            "required S02 tables are missing",
            details={
                "failed_checks": ["required_tables"],
                "missing_table_count": len(missing_tables),
            },
        )
    companies = session.scalars(select(Company).where(Company.inn.in_(cohort))).all()
    by_inn = {company.inn: company for company in companies}
    missing_companies = sorted(set(cohort) - by_inn.keys())
    non_legal = sorted(
        inn for inn, company in by_inn.items() if company.entity_type != "legal"
    )
    if missing_companies or non_legal:
        raise OperatorError(
            "COHORT_INVALID",
            "cohort must contain existing Master Company legal entities only",
            details={
                "missing_company_inns": missing_companies,
                "non_legal_inns": non_legal,
            },
        )
    dataset = session.scalar(select(DataSet).where(DataSet.code == DATASET_CODE))
    pointer = session.get(WorkerPublicationState, SOURCE_ID)
    pilot = session.get(FnsTaxDebtPilotState, SOURCE_ID)
    baseline_row: FnsTaxDebtPublicationGeneration | None = None
    active_row: FnsTaxDebtPublicationGeneration | None = None
    baseline_run: WorkerRun | None = None
    baseline_job: WorkerJob | None = None
    baseline_artifact: FnsTaxDebtRawArtifact | None = None
    active_run: WorkerRun | None = None
    active_job: WorkerJob | None = None
    active_artifact: FnsTaxDebtRawArtifact | None = None
    raw_count = 0
    fact_count = 0
    pointer_metadata = (
        dict(pointer.validation_metadata)
        if pointer is not None and isinstance(pointer.validation_metadata, Mapping)
        else {}
    )
    pointer_validation = (
        dict(pointer_metadata.get("validation"))
        if isinstance(pointer_metadata.get("validation"), Mapping)
        else {}
    )
    if dataset is not None:
        baseline_row = session.scalar(
            select(FnsTaxDebtPublicationGeneration).where(
                FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
                FnsTaxDebtPublicationGeneration.generation == 0,
            )
        )
        active_row = session.scalar(
            select(FnsTaxDebtPublicationGeneration).where(
                FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
                FnsTaxDebtPublicationGeneration.status == "active",
            )
        )
        raw_count = int(
            session.scalar(
                select(func.count())
                .select_from(FnsTaxDebtRawArtifact)
                .where(FnsTaxDebtRawArtifact.dataset_id == dataset.id)
            )
            or 0
        )
        fact_count = int(
            session.scalar(
                select(func.count())
                .select_from(CompanyTaxDebtSnapshot)
                .where(
                    CompanyTaxDebtSnapshot.dataset_id == dataset.id,
                    CompanyTaxDebtSnapshot.publication_generation == 0,
                )
            )
            or 0
        )
    if baseline_row is not None:
        baseline_run = session.get(WorkerRun, baseline_row.worker_run_id)
        baseline_job = (
            session.get(WorkerJob, baseline_run.job_id)
            if baseline_run is not None
            else None
        )
        baseline_artifact = session.get(FnsTaxDebtRawArtifact, baseline_row.artifact_id)
    if active_row is not None:
        active_run = session.get(WorkerRun, active_row.worker_run_id)
        active_job = (
            session.get(WorkerJob, active_run.job_id) if active_run is not None else None
        )
        active_artifact = session.get(FnsTaxDebtRawArtifact, active_row.artifact_id)

    dataset_coverage = (
        dict(dataset.coverage)
        if dataset is not None and isinstance(dataset.coverage, Mapping)
        else {}
    )
    dataset_actual_until = (
        dataset.official_actual_until if dataset is not None else None
    ) or _coverage_actual_until(dataset_coverage)
    transitioned = bool(
        active_row is not None
        or (
            pointer is not None
            and (
                (_usable_count(pointer.generation) and pointer.generation > 1)
                or pointer.rollback_pointer
            )
        )
        or (
            pilot is not None
            and any(
                not _usable_count(value) or value > 0
                for value in (
                    pilot.generation,
                    pilot.normalized_generation,
                    pilot.fact_generation,
                    pilot.query_generation,
                )
            )
        )
    )

    failed: list[str] = []

    def require(condition: Any, name: str) -> None:
        if not condition:
            failed.append(name)

    require(dataset is not None, "dataset_missing")
    if dataset is not None:
        require(dataset.code == DATASET_CODE, "dataset_code")
        require(
            dataset.operational_status in CURRENT_DATASET_STATUSES
            or (transitioned and dataset.operational_status == "error"),
            "dataset_status",
        )
        require(dataset.source_as_of is not None, "dataset_source_as_of")
        require(dataset.retrieved_at is not None, "dataset_retrieved_at")
        require(dataset.last_data_date is not None, "dataset_last_data_date")
        require(dataset.published_at is not None, "dataset_published_at")
        require(_usable_count(dataset.record_count), "dataset_record_count")
        require(bool(dataset_coverage), "dataset_coverage")
        require(dataset_actual_until is not None, "dataset_official_actual_until")
        if dataset.source_as_of is not None and dataset.retrieved_at is not None:
            require(dataset.source_as_of <= dataset.retrieved_at, "dataset_time_order")
        if dataset.last_data_date is not None and dataset_actual_until is not None:
            require(
                dataset.last_data_date <= dataset_actual_until,
                "dataset_actual_until_order",
            )

    require(pointer is not None, "publication_state_missing")
    if pointer is not None:
        require(pointer.source_id == SOURCE_ID, "publication_source")
        require(bool(pointer.active_pointer), "publication_active_pointer")
        require(
            pointer.published_by_run_id is not None,
            "publication_published_by_run",
        )
        require(
            _usable_count(pointer.generation)
            and pointer.generation > (1 if transitioned else 0),
            "publication_generation",
        )
        require(bool(pointer_metadata), "publication_validation_metadata")
        require(
            isinstance(pointer_validation.get("raw_pointer"), str)
            and bool(pointer_validation.get("raw_pointer")),
            "publication_raw_pointer",
        )
        require(
            isinstance(pointer_metadata.get("checksum"), str)
            and CHECKSUM_RE.fullmatch(pointer_metadata["checksum"]) is not None,
            "publication_checksum",
        )

    require(baseline_row is not None, "baseline_generation_missing")
    require(baseline_run is not None, "baseline_run_missing")
    require(baseline_job is not None, "publication_job_missing")
    require(baseline_artifact is not None, "baseline_artifact_missing")
    if baseline_row is not None and dataset is not None:
        require(baseline_row.generation == 0, "baseline_generation")
        require(baseline_row.publication_scope == "baseline", "baseline_scope")
        require(baseline_row.status in {"baseline", "rollback"}, "baseline_status")
        require(baseline_row.dataset_id == dataset.id, "baseline_dataset")
        require(bool(baseline_row.staging_pointer), "baseline_staging_pointer")
        require(baseline_row.published_at is not None, "baseline_published_at")
        baseline_coverage = _mapping_dict(baseline_row.coverage) or {}
        require(
            _generation_dataset_metadata_matches(
                baseline_row, historical_baseline=True
            ),
            "baseline_dataset_metadata",
        )
        baseline_mode = (
            "controlled_live"
            if baseline_job is not None
            and baseline_job.job_type == "fns_tax_debt_baseline"
            else "fixture"
        )
        require(
            _generation_validation_matches(
                baseline_row, ingestion_mode=baseline_mode
            ),
            "baseline_validation_metadata",
        )
        require(
            _generation_coverage_matches(
                baseline_row,
                cohort=cohort,
                require_scope=baseline_mode == "controlled_live",
            ),
            "baseline_coverage",
        )
        require(baseline_row.last_data_date is not None, "baseline_last_data_date")
        require(_usable_count(baseline_row.record_count), "baseline_record_count")
        require(
            baseline_row.official_actual_until
            == _coverage_actual_until(baseline_coverage),
            "baseline_official_actual_until",
        )

    if baseline_run is not None:
        require(baseline_run.status == "succeeded", "baseline_run_status")
        require(baseline_run.finished_at is not None, "baseline_run_finished_at")
        require(
            all(
                _usable_count(value)
                for value in _worker_run_counters(baseline_run).values()
            ),
            "publication_run_counters",
        )
        if baseline_row is not None:
            require(
                _mapping_dict(baseline_row.counters)
                == _worker_run_counters(baseline_run),
                "baseline_counters",
            )
    if baseline_job is not None and baseline_run is not None:
        require(baseline_job.source_id == SOURCE_ID, "baseline_job_source")
        require(
            (baseline_job.job_type, baseline_job.handler_version)
            in BASELINE_JOB_CONTRACTS,
            "baseline_job_type",
        )
        require(
            baseline_job.handler_version == baseline_run.handler_version,
            "baseline_job_handler",
        )
        require(baseline_job.status == "succeeded", "baseline_job_status")

    if baseline_artifact is not None and baseline_row is not None and dataset is not None:
        require(
            baseline_row.artifact_id == baseline_artifact.id,
            "baseline_artifact",
        )
        require(
            baseline_artifact.dataset_id == dataset.id,
            "baseline_artifact_dataset",
        )
        require(
            baseline_row.raw_pointer == baseline_artifact.artifact_reference,
            "baseline_raw_pointer",
        )
        require(
            baseline_row.checksum == baseline_artifact.sha256,
            "baseline_checksum",
        )
        require(
            baseline_row.source_as_of == baseline_artifact.source_as_of,
            "baseline_source_as_of",
        )
        require(
            baseline_row.retrieved_at == baseline_artifact.retrieved_at,
            "baseline_retrieved_at",
        )
        first_run = session.get(WorkerRun, baseline_artifact.first_worker_run_id)
        first_job = (
            session.get(WorkerJob, first_run.job_id) if first_run is not None else None
        )
        require(
            first_run is not None
            and first_job is not None
            and baseline_run is not None
            and first_run.id == baseline_run.id
            and first_run.status == "succeeded"
            and first_job.status == "succeeded"
            and first_job.source_id == SOURCE_ID
            and (first_job.job_type, first_job.handler_version)
            in S02_ARTIFACT_JOB_CONTRACTS
            and first_job.handler_version == first_run.handler_version,
            "baseline_artifact_owner",
        )
        if baseline_run is not None:
            manifest = session.scalar(
                select(WorkerRawManifest).where(
                    WorkerRawManifest.run_id == baseline_run.id,
                    WorkerRawManifest.artifact_reference
                    == baseline_artifact.artifact_reference,
                )
            )
            require(
                _raw_manifest_matches(
                    manifest, run=baseline_run, artifact=baseline_artifact
                ),
                "baseline_raw_manifest",
            )

    if baseline_row is not None and dataset is not None:
        outside_baseline = int(
            session.scalar(
                select(func.count())
                .select_from(CompanyTaxDebtSnapshot)
                .join(Company, Company.id == CompanyTaxDebtSnapshot.company_id)
                .where(
                    CompanyTaxDebtSnapshot.dataset_id == dataset.id,
                    CompanyTaxDebtSnapshot.publication_generation == 0,
                    Company.inn.not_in(cohort),
                )
            )
            or 0
        )
        require(outside_baseline == 0, "baseline_facts_outside_cohort")
        if baseline_run is not None:
            require(
                fact_count == baseline_run.records_published,
                "baseline_fact_count",
            )

    current_row = active_row if transitioned else baseline_row
    current_run = active_run if transitioned else baseline_run
    current_job = active_job if transitioned else baseline_job
    current_artifact = active_artifact if transitioned else baseline_artifact
    require(current_run is not None, "publication_run_missing")
    if current_run is not None:
        require(current_run.status == "succeeded", "publication_run_status")
        require(current_run.finished_at is not None, "publication_run_finished_at")
    require(current_job is not None, "publication_job_missing")
    require(current_artifact is not None, "raw_artifact_missing")
    if pointer is not None and current_row is not None:
        require(
            pointer.active_pointer == current_row.staging_pointer,
            "publication_active_pointer",
        )
        require(
            pointer.published_by_run_id == current_row.worker_run_id,
            "publication_published_by_run",
        )
        require(
            pointer_metadata.get("checksum") == current_row.checksum,
            "publication_checksum",
        )
        require(
            pointer_validation == _mapping_dict(current_row.validation_metadata),
            "publication_validation_metadata",
        )
        require(
            pointer_validation.get("raw_pointer") == current_row.raw_pointer,
            "publication_raw_pointer",
        )
    if current_artifact is not None and current_row is not None and dataset is not None:
        require(current_artifact.dataset_id == dataset.id, "raw_artifact_dataset")
        require(
            current_artifact.artifact_reference == current_row.raw_pointer,
            "raw_artifact_pointer",
        )
        require(current_artifact.sha256 == current_row.checksum, "raw_artifact_checksum")
        require(
            current_artifact.source_as_of == current_row.source_as_of,
            "raw_artifact_source_as_of",
        )
        require(
            current_artifact.retrieved_at == current_row.retrieved_at,
            "raw_artifact_retrieved_at",
        )

    if not transitioned:
        if pointer is not None:
            require(pointer.generation == 1, "publication_generation")
            require(pointer.rollback_pointer is None, "rollback_pointer")
        if baseline_row is not None and dataset is not None:
            require(dataset.source_as_of == baseline_row.source_as_of, "dataset_source_as_of")
            require(dataset.retrieved_at == baseline_row.retrieved_at, "dataset_retrieved_at")
            require(
                dataset.last_data_date == baseline_row.last_data_date,
                "dataset_last_data_date",
            )
            require(dataset.record_count == baseline_row.record_count, "dataset_record_count")
            require(
                dataset_actual_until == baseline_row.official_actual_until,
                "dataset_official_actual_until",
            )
            require(dataset_coverage == _mapping_dict(baseline_row.coverage), "dataset_coverage")
            require(
                _dataset_metadata_snapshot(dataset)
                == _mapping_dict(baseline_row.dataset_metadata),
                "baseline_dataset_metadata",
            )
        if pilot is not None and baseline_row is not None and baseline_artifact is not None:
            require(pilot.dataset_id == dataset.id, "pilot_dataset")
            require(
                len(tuple(pilot.cohort_inns or ())) == len(cohort)
                and set(str(value) for value in pilot.cohort_inns or ()) == set(cohort),
                "pilot_cohort",
            )
            require(pilot.generation == 0, "pilot_generation")
            require(pilot.baseline_generation == 0, "pilot_baseline_generation")
            require(pilot.normalized_generation == 0, "pilot_normalized_generation")
            require(pilot.fact_generation == 0, "pilot_fact_generation")
            require(pilot.query_generation == 0, "pilot_query_generation")
            require(
                pilot.active_raw_pointer
                in {None, baseline_artifact.artifact_reference},
                "pilot_raw_pointer",
            )
            require(pilot.active_checksum in {None, baseline_artifact.sha256}, "pilot_checksum")
            require(
                pilot.active_source_as_of in {None, baseline_row.source_as_of},
                "pilot_source_as_of",
            )
            require(
                pilot.active_retrieved_at in {None, baseline_row.retrieved_at},
                "pilot_retrieved_at",
            )
            require(
                pilot.active_data_date in {None, baseline_row.last_data_date},
                "pilot_data_date",
            )
            require(
                pilot.baseline_data_date in {None, baseline_row.last_data_date},
                "pilot_baseline_data_date",
            )
    else:
        require(active_row is not None, "active_generation_missing")
        require(pilot is not None, "pilot_state_missing")
        if active_row is not None and dataset is not None:
            require(active_row.generation > 0, "active_generation")
            require(active_row.publication_scope == "pilot", "active_scope")
            require(active_row.status == "active", "active_status")
            require(active_row.dataset_id == dataset.id, "active_dataset")
            require(
                _generation_validation_matches(
                    active_row, ingestion_mode="controlled_live"
                ),
                "active_validation_metadata",
            )
            require(
                _generation_dataset_metadata_matches(
                    active_row, historical_baseline=False
                ),
                "active_dataset_metadata",
            )
            require(
                _generation_coverage_matches(
                    active_row, cohort=cohort, require_scope=True
                ),
                "active_coverage",
            )
            require(dataset.source_as_of == active_row.source_as_of, "dataset_source_as_of")
            require(dataset.retrieved_at == active_row.retrieved_at, "dataset_retrieved_at")
            require(dataset.last_data_date == active_row.last_data_date, "dataset_last_data_date")
            require(dataset.record_count == active_row.record_count, "dataset_record_count")
            require(
                dataset_actual_until == active_row.official_actual_until,
                "dataset_official_actual_until",
            )
            require(
                baseline_row is not None
                and _mapping_contains(
                    dataset_coverage, _mapping_dict(baseline_row.coverage) or {}
                ),
                "dataset_coverage",
            )
            outside_active = int(
                session.scalar(
                    select(func.count())
                    .select_from(CompanyTaxDebtSnapshot)
                    .join(Company, Company.id == CompanyTaxDebtSnapshot.company_id)
                    .where(
                        CompanyTaxDebtSnapshot.dataset_id == dataset.id,
                        CompanyTaxDebtSnapshot.publication_generation
                        == active_row.generation,
                        Company.inn.not_in(cohort),
                    )
                )
                or 0
            )
            require(outside_active == 0, "active_facts_outside_cohort")
            active_fact_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(CompanyTaxDebtSnapshot)
                    .where(
                        CompanyTaxDebtSnapshot.dataset_id == dataset.id,
                        CompanyTaxDebtSnapshot.publication_generation
                        == active_row.generation,
                    )
                )
                or 0
            )
            if active_run is not None:
                require(
                    active_fact_count == active_run.records_published,
                    "active_fact_count",
                )
        if active_run is not None:
            require(active_run.status == "succeeded", "active_run_status")
            require(active_run.finished_at is not None, "active_run_finished_at")
            require(
                all(
                    _usable_count(value)
                    for value in _worker_run_counters(active_run).values()
                ),
                "active_run_counters",
            )
            if active_row is not None:
                require(
                    _mapping_dict(active_row.counters)
                    == _worker_run_counters(active_run),
                    "active_counters",
                )
        if active_job is not None and active_run is not None:
            require(active_job.source_id == SOURCE_ID, "active_job_source")
            require(
                (
                    active_job.job_type,
                    active_job.handler_version,
                )
                == (
                    "fns_tax_debt_controlled_live",
                    CONTROLLED_LIVE_HANDLER_VERSION,
                ),
                "active_job_type",
            )
            require(active_job.handler_version == active_run.handler_version, "active_job_handler")
            require(active_job.status == "succeeded", "active_job_status")
        if active_artifact is not None and active_row is not None and dataset is not None:
            require(active_artifact.dataset_id == dataset.id, "active_artifact_dataset")
            require(
                active_artifact.artifact_reference == active_row.raw_pointer,
                "active_artifact_pointer",
            )
            require(active_artifact.sha256 == active_row.checksum, "active_artifact_checksum")
            require(
                active_artifact.source_as_of == active_row.source_as_of,
                "active_artifact_source_as_of",
            )
            require(
                active_artifact.retrieved_at == active_row.retrieved_at,
                "active_artifact_retrieved_at",
            )
            first_run = session.get(WorkerRun, active_artifact.first_worker_run_id)
            first_job = session.get(WorkerJob, first_run.job_id) if first_run is not None else None
            require(
                first_run is not None
                and first_job is not None
                and first_run.status == "succeeded"
                and first_job.status == "succeeded"
                and first_job.source_id == SOURCE_ID
                and (first_job.job_type, first_job.handler_version)
                in S02_ARTIFACT_JOB_CONTRACTS
                and first_job.handler_version == first_run.handler_version,
                "active_artifact_owner",
            )
            if active_run is not None:
                manifest = session.scalar(
                    select(WorkerRawManifest).where(
                        WorkerRawManifest.run_id == active_run.id,
                        WorkerRawManifest.artifact_reference
                        == active_artifact.artifact_reference,
                    )
                )
                require(
                    _raw_manifest_matches(
                        manifest, run=active_run, artifact=active_artifact
                    ),
                    "active_raw_manifest",
                )
                require(
                    _active_fact_integrity_matches(
                        session,
                        dataset=dataset,
                        generation=active_row,
                        artifact=active_artifact,
                        run=active_run,
                        cohort=cohort,
                    ),
                    "active_fact_integrity",
                )
        if pilot is not None and active_row is not None and dataset is not None:
            require(pilot.dataset_id == dataset.id, "pilot_dataset")
            require(
                len(tuple(pilot.cohort_inns or ())) == len(cohort)
                and set(str(value) for value in pilot.cohort_inns or ()) == set(cohort),
                "pilot_cohort",
            )
            require(
                pointer is not None and pilot.generation == pointer.generation,
                "pilot_generation",
            )
            require(pilot.baseline_generation == 0, "pilot_baseline_generation")
            require(
                pilot.normalized_generation == active_row.generation,
                "pilot_normalized_generation",
            )
            require(pilot.fact_generation == active_row.generation, "pilot_fact_generation")
            require(pilot.query_generation == active_row.generation, "pilot_query_generation")
            require(pilot.active_raw_pointer == active_row.raw_pointer, "pilot_raw_pointer")
            require(pilot.active_checksum == active_row.checksum, "pilot_checksum")
            require(pilot.active_source_as_of == active_row.source_as_of, "pilot_source_as_of")
            require(pilot.active_retrieved_at == active_row.retrieved_at, "pilot_retrieved_at")
            require(pilot.active_data_date == active_row.last_data_date, "pilot_data_date")
            effective_baseline_data_date = (
                pilot.baseline_data_date
                if pilot.baseline_data_date is not None
                else baseline_row.last_data_date if baseline_row is not None else None
            )
            require(
                baseline_row is not None
                and pilot.baseline_generation == baseline_row.generation == 0
                and effective_baseline_data_date == baseline_row.last_data_date,
                "pilot_baseline_data_date",
            )
        rollback_row = None
        if pointer is not None and dataset is not None and pointer.rollback_pointer:
            rollback_row = session.scalar(
                select(FnsTaxDebtPublicationGeneration).where(
                    FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
                    FnsTaxDebtPublicationGeneration.staging_pointer
                    == pointer.rollback_pointer,
                )
            )
        require(pointer is not None and bool(pointer.rollback_pointer), "rollback_pointer")
        require(rollback_row is not None, "rollback_generation")
        if rollback_row is not None:
            require(rollback_row.status in {"baseline", "rollback"}, "rollback_status")
            if pilot is not None:
                require(
                    pilot.rollback_fact_generation == rollback_row.generation,
                    "rollback_fact_generation",
                )
                require(
                    pilot.rollback_normalized_generation == rollback_row.generation,
                    "rollback_normalized_generation",
                )
        if dataset is not None and dataset.operational_status == "error":
            require(
                active_row is not None
                and _dataset_error_recovery_matches(
                    session,
                    dataset=dataset,
                    active_generation=active_row,
                    cohort=cohort,
                ),
                "dataset_error_recovery",
            )

    if failed:
        raise OperatorError(
            "BASELINE_NOT_READY",
            "exact S02 baseline chain could not be proven",
            details={
                "failed_checks": sorted(set(failed)),
                "raw_artifact_count": raw_count,
                "baseline_fact_count": fact_count,
            },
        )
    return {
        "dataset_id": dataset.id,
        "baseline_generation": baseline_row.generation,
        "worker_generation": pointer.generation,
        "pilot_generation": pilot.generation if pilot else None,
        "effective_baseline_data_date": (
            baseline_row.last_data_date if baseline_row is not None else None
        ),
        "dataset_status_mode": (
            "failed_refresh_recovery"
            if dataset.operational_status == "error"
            else "healthy"
        ),
        "baseline_metadata_captured": True,
    }


def build_preflight_report(
    session: Session,
    *,
    source_package: str | Path,
    artifact: str | Path,
    xsd: str | Path,
    cohort_path: str | Path,
    expected_main_sha: str,
    now: datetime | None = None,
    discovery_client: FnsTaxDebtOfficialClient | None = None,
) -> tuple[dict[str, Any], dict[str, Any], tuple[str, ...], TaxDebtDiscovery]:
    now = now or _utc_now()
    package = load_source_package(source_package)
    cohort, cohort_sha = load_cohort(cohort_path)
    main_sha = check_main_sha(
        expected=expected_main_sha, package_sha=package["main_sha"]
    )
    if release_freshness(package["official_actual_until"], now=now) == "stale":
        raise OperatorError(
            "SOURCE_PACKAGE_STALE", "official S02 source package is stale"
        )
    artifact_sha, xsd_sha = verify_local_files(package, artifact, xsd)
    discovery = discover_exact_release(package, now=now, client=discovery_client)
    readiness = database_readiness(session, cohort=cohort)
    jobs = runnable_jobs(session, now=now)
    report = {
        "status": "READY",
        "main_sha": main_sha,
        "dataset": package["dataset_id"],
        "source_package_fingerprint": source_package_fingerprint(package),
        "artifact_sha256": artifact_sha,
        "xsd_sha256": xsd_sha,
        "release_freshness": "current",
        "cohort_count": len(cohort),
        "cohort_sha256": cohort_sha,
        "baseline_generation": readiness["baseline_generation"],
        "worker_generation": readiness["worker_generation"],
        "runnable_jobs": jobs,
        "mass_ingestion_enabled": MASS_INGESTION_ENABLED,
        "controlled_live_default_enabled": CONTROLLED_LIVE_PILOT_ENABLED,
        "blockers": [],
    }
    if MASS_INGESTION_ENABLED or CONTROLLED_LIVE_PILOT_ENABLED:
        raise OperatorError(
            "OPERATOR_ERROR", "S02 default safety flags are not disabled"
        )
    return report, package, cohort, discovery


def baseline_preparation_readiness(
    session: Session,
    *,
    cohort: tuple[str, ...],
    artifact_sha256: str,
) -> dict[str, Any]:
    """Prove that generation 0 is absent or is the exact completed baseline."""

    required_tables = {
        "companies",
        "data_sets",
        "worker_jobs",
        "worker_runs",
        "worker_handler_registry",
        "worker_raw_manifests",
        "worker_publication_state",
        "fns_tax_debt_raw_artifacts",
        "fns_tax_debt_normalized_records",
        "fns_tax_debt_publication_generations",
        "fns_tax_debt_pilot_state",
        "company_tax_debt_snapshots",
    }
    table_names = set(inspect(session.get_bind()).get_table_names())
    missing_tables = sorted(required_tables - table_names)
    if missing_tables:
        raise OperatorError(
            "BASELINE_NOT_READY",
            "required S02 baseline tables are missing",
            details={"missing_table_count": len(missing_tables)},
        )

    companies = session.scalars(select(Company).where(Company.inn.in_(cohort))).all()
    by_inn: dict[str, list[Company]] = {}
    for company in companies:
        by_inn.setdefault(company.inn, []).append(company)
    if any(
        len(by_inn.get(inn, ())) != 1 or by_inn[inn][0].entity_type != "legal"
        for inn in cohort
    ):
        raise OperatorError(
            "COHORT_INVALID",
            "baseline cohort must resolve to exact Master legal entities",
        )

    dataset = session.scalar(select(DataSet).where(DataSet.code == DATASET_CODE))
    if dataset is None:
        raise OperatorError("BASELINE_NOT_READY", "S02 DataSet is not registered")
    generation = session.scalar(
        select(FnsTaxDebtPublicationGeneration).where(
            FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
            FnsTaxDebtPublicationGeneration.generation == 0,
        )
    )
    if generation is not None:
        run = session.get(WorkerRun, generation.worker_run_id)
        job = session.get(WorkerJob, run.job_id) if run is not None else None
        expected_cohort = list(cohort)
        if (
            generation.publication_scope != "baseline"
            or generation.status != "baseline"
            or generation.checksum != artifact_sha256
            or list(dict(generation.coverage or {}).get("cohort_inns") or ())
            != expected_cohort
            or run is None
            or run.status != "succeeded"
            or job is None
            or job.job_type != "fns_tax_debt_baseline"
            or job.handler_version != BASELINE_HANDLER_VERSION
            or job.status != "succeeded"
        ):
            raise OperatorError(
                "BASELINE_NOT_READY",
                "an incompatible generation-0 baseline already exists",
            )
        exact = database_readiness(session, cohort=cohort)
        outside_count = int(
            session.scalar(
                select(func.count())
                .select_from(CompanyTaxDebtSnapshot)
                .join(Company, Company.id == CompanyTaxDebtSnapshot.company_id)
                .where(
                    CompanyTaxDebtSnapshot.dataset_id == dataset.id,
                    CompanyTaxDebtSnapshot.publication_generation == 0,
                    Company.inn.not_in(cohort),
                )
            )
            or 0
        )
        if outside_count:
            raise OperatorError(
                "BASELINE_NOT_READY",
                "generation-0 facts exist outside the approved cohort",
                details={"baseline_fact_count": outside_count},
            )
        return {
            "state": "existing",
            "dataset_id": dataset.id,
            "job_id": job.id,
            "run_id": run.id,
            "baseline_generation": 0,
            "worker_generation": exact["worker_generation"],
            "outside_cohort_facts": 0,
        }

    pointer = session.get(WorkerPublicationState, SOURCE_ID)
    pilot = session.get(FnsTaxDebtPilotState, SOURCE_ID)
    publication_count = int(
        session.scalar(
            select(func.count())
            .select_from(FnsTaxDebtPublicationGeneration)
            .where(FnsTaxDebtPublicationGeneration.dataset_id == dataset.id)
        )
        or 0
    )
    fact_count = int(
        session.scalar(
            select(func.count())
            .select_from(CompanyTaxDebtSnapshot)
            .where(CompanyTaxDebtSnapshot.dataset_id == dataset.id)
        )
        or 0
    )
    raw_count = int(
        session.scalar(
            select(func.count())
            .select_from(FnsTaxDebtRawArtifact)
            .where(FnsTaxDebtRawArtifact.dataset_id == dataset.id)
        )
        or 0
    )
    normalized_count = int(
        session.scalar(
            select(func.count())
            .select_from(FnsTaxDebtNormalizedRecord)
            .where(FnsTaxDebtNormalizedRecord.dataset_id == dataset.id)
        )
        or 0
    )
    if (
        pointer is not None
        or pilot is not None
        or publication_count
        or fact_count
        or raw_count
        or normalized_count
    ):
        raise OperatorError(
            "BASELINE_NOT_READY",
            "S02 state is not empty and has no exact generation-0 ledger",
            details={
                "baseline_fact_count": fact_count,
                "raw_artifact_count": raw_count,
            },
        )
    if any(
        value is not None
        for value in (
            dataset.last_success_at,
            dataset.last_data_date,
            dataset.source_as_of,
            dataset.retrieved_at,
            dataset.published_at,
        )
    ):
        raise OperatorError(
            "BASELINE_NOT_READY",
            "S02 DataSet has unledgered publication coordinates",
        )
    return {"state": "empty", "dataset_id": dataset.id}


def build_baseline_preparation_report(
    session: Session,
    *,
    source_package: str | Path,
    artifact: str | Path,
    xsd: str | Path,
    cohort_path: str | Path,
    expected_main_sha: str,
    now: datetime | None = None,
    discovery_client: FnsTaxDebtOfficialClient | None = None,
) -> tuple[dict[str, Any], dict[str, Any], tuple[str, ...], TaxDebtDiscovery]:
    now = now or _utc_now()
    package = load_source_package(source_package)
    cohort, cohort_sha = load_cohort(cohort_path)
    try:
        BaselinePreparationConfig(cohort_inns=frozenset(cohort)).validate()
    except Exception as error:
        raise OperatorError(
            "COHORT_INVALID", "baseline cohort must contain at most 40 legal entities"
        ) from error
    main_sha = check_main_sha(
        expected=expected_main_sha, package_sha=package["main_sha"]
    )
    if release_freshness(package["official_actual_until"], now=now) == "stale":
        raise OperatorError(
            "SOURCE_PACKAGE_STALE", "official S02 source package is stale"
        )
    artifact_sha, xsd_sha = verify_local_files(package, artifact, xsd)
    discovery = discover_exact_release(package, now=now, client=discovery_client)
    readiness = baseline_preparation_readiness(
        session,
        cohort=cohort,
        artifact_sha256=artifact_sha,
    )
    return (
        {
            "status": "BASELINE_READY"
            if readiness["state"] == "existing"
            else "READY_FOR_BASELINE",
            "main_sha": main_sha,
            "dataset": package["dataset_id"],
            "source_package_fingerprint": source_package_fingerprint(package),
            "artifact_sha256": artifact_sha,
            "xsd_sha256": xsd_sha,
            "release_freshness": "current",
            "cohort_count": len(cohort),
            "cohort_sha256": cohort_sha,
            **readiness,
        },
        package,
        cohort,
        discovery,
    )


def queue_guard(session: Session, *, expected_job_id: UUID, now: datetime) -> WorkerJob:
    job = session.get(WorkerJob, expected_job_id)
    if job is None:
        raise OperatorError("JOB_MISMATCH", "expected worker job does not exist")
    expected = (
        SOURCE_ID,
        "fns_tax_debt_controlled_live",
        CONTROLLED_LIVE_HANDLER_VERSION,
    )
    actual = (job.source_id, job.job_type, job.handler_version)
    if actual != expected or job.status not in {"queued", "retry_scheduled"}:
        raise OperatorError(
            "JOB_MISMATCH",
            "expected job contract or runnable state differs",
            details={"actual": actual, "status": job.status},
        )
    require_durable_approval(session)
    ordered = session.scalars(_runnable_query(now)).all()
    if not ordered or ordered[0].id != expected_job_id:
        raise OperatorError(
            "QUEUE_NOT_EXCLUSIVE",
            "expected S02 job is not the exact next job claimable by WorkerExecutor",
            details={"next_job_id": str(ordered[0].id) if ordered else None},
        )
    return job


def _generation_state(session: Session) -> dict[str, Any]:
    pointer = session.get(WorkerPublicationState, SOURCE_ID)
    pilot = session.get(FnsTaxDebtPilotState, SOURCE_ID)
    active_identity = None
    rollback_identity = None
    if pointer is not None:
        if pointer.active_pointer:
            active_identity = sha256(pointer.active_pointer.encode("utf-8")).hexdigest()
        if pointer.rollback_pointer:
            rollback_identity = sha256(
                pointer.rollback_pointer.encode("utf-8")
            ).hexdigest()
    return {
        "worker_generation": pointer.generation if pointer else None,
        "active_pointer_identity": active_identity,
        "rollback_pointer_identity": rollback_identity,
        "fact_generation": pilot.fact_generation if pilot else None,
        "query_generation": pilot.query_generation if pilot else None,
        "normalized_generation": pilot.normalized_generation if pilot else None,
        "checksum": pilot.active_checksum if pilot else None,
        "freshness": pilot.freshness if pilot else "unknown",
    }


def _safe_errors(errors: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for error in errors:
        message = str(error.get("message") or "")
        safe.append(
            {
                "kind": error.get("kind"),
                "at": error.get("at"),
                "message_sha256": sha256(message.encode("utf-8")).hexdigest()
                if message
                else None,
            }
        )
    return safe


def command_preflight(args: argparse.Namespace, factory) -> dict[str, Any]:
    with factory() as session:
        try:
            report, _, _, _ = build_preflight_report(
                session,
                source_package=args.source_package,
                artifact=args.artifact,
                xsd=args.xsd,
                cohort_path=args.cohort,
                expected_main_sha=args.expected_main_sha,
            )
            return report
        finally:
            session.rollback()


def _critical_preflight(args: argparse.Namespace, factory):
    with factory() as session:
        try:
            result = build_preflight_report(
                session,
                source_package=args.source_package,
                artifact=args.artifact,
                xsd=args.xsd,
                cohort_path=args.cohort,
                expected_main_sha=args.expected_main_sha,
            )
            return result
        finally:
            session.rollback()


def _require_token(actual: str, expected: str) -> None:
    if actual != expected:
        raise OperatorError(
            "CONFIRMATION_REQUIRED", f"exact confirmation token required: {expected}"
        )


def command_approve(args: argparse.Namespace, factory) -> dict[str, Any]:
    _require_token(args.confirm_controlled_live, APPROVE_TOKEN)
    _, _, _, _ = _critical_preflight(args, factory)
    approved_at = _argument_timestamp(args.approved_at)
    with factory() as session:
        try:
            record = approve_fns_tax_debt_controlled_live_handler(
                session, approved_by=args.approved_by, approved_at=approved_at
            )
            session.commit()
            metadata = dict(record.metadata_json or {})
            return {
                "status": "APPROVED",
                "source_id": record.source_id,
                "handler_version": record.handler_version,
                "approved_by": metadata.get("approved_by"),
                "approved_at": metadata.get("approved_at"),
                "approval_state": {
                    "approved": record.approved,
                    "enabled": record.enabled,
                    "live_mode": record.live_mode,
                },
            }
        except Exception:
            session.rollback()
            raise


def command_approve_baseline(args: argparse.Namespace, factory) -> dict[str, Any]:
    _require_token(args.confirm_baseline_approval, BASELINE_APPROVE_TOKEN)
    with factory() as session:
        try:
            report, package, cohort, _ = build_baseline_preparation_report(
                session,
                source_package=args.source_package,
                artifact=args.artifact,
                xsd=args.xsd,
                cohort_path=args.cohort,
                expected_main_sha=args.expected_main_sha,
            )
        finally:
            session.rollback()
    approved_at = _argument_timestamp(args.approved_at)
    with factory() as session:
        try:
            record = approve_fns_tax_debt_baseline_handler(
                session,
                approved_by=args.approved_by,
                approved_at=approved_at,
                config=BaselinePreparationConfig(cohort_inns=frozenset(cohort)),
                artifact_sha256=package["artifact_sha256"],
                xsd_sha256=package["xsd_sha256"],
            )
            session.commit()
            metadata = dict(record.metadata_json or {})
            return {
                "status": "BASELINE_APPROVED",
                "source_id": record.source_id,
                "handler_version": record.handler_version,
                "approved_by": metadata.get("approved_by"),
                "approved_at": metadata.get("approved_at"),
                "cohort_sha256": report["cohort_sha256"],
                "source_package_fingerprint": report["source_package_fingerprint"],
            }
        except Exception:
            session.rollback()
            raise


def command_enqueue(args: argparse.Namespace, factory) -> dict[str, Any]:
    _require_token(args.confirm_enqueue, ENQUEUE_TOKEN)
    if not MIN_TIMEOUT_SECONDS <= args.timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise OperatorError(
            "OPERATOR_ERROR",
            f"timeout-seconds must be within {MIN_TIMEOUT_SECONDS}..{MAX_TIMEOUT_SECONDS}",
        )
    _, package, cohort, discovery = _critical_preflight(args, factory)
    retrieved_at = _argument_timestamp(args.retrieved_at)
    config = ControlledLivePilotConfig(enabled=True, cohort_inns=frozenset(cohort))
    with factory() as session:
        try:
            require_durable_approval(session)
            creation = enqueue_fns_tax_debt_controlled_live_job(
                session,
                source_path=args.artifact,
                xsd_path=args.xsd,
                artifact_store=args.artifact_store,
                discovery=discovery,
                config=config,
                retrieved_at=retrieved_at,
                expected_sha256=package["artifact_sha256"],
                expected_xsd_sha256=package["xsd_sha256"],
                timeout_seconds=args.timeout_seconds,
            )
            if creation.created or creation.job.status in {"queued", "retry_scheduled"}:
                queue_guard(session, expected_job_id=creation.job.id, now=_utc_now())
            elif creation.job.status != "succeeded":
                raise OperatorError(
                    "JOB_MISMATCH",
                    "idempotent S02 job exists in a non-runnable terminal state",
                    details={"status": creation.job.status},
                )
            session.commit()
            _, cohort_sha = load_cohort(args.cohort)
            return {
                "status": "ENQUEUED" if creation.created else "REUSED",
                "job_id": creation.job.id,
                "idempotency_key": creation.job.idempotency_key,
                "created": creation.created,
                "handler_version": creation.job.handler_version,
                "cohort_sha256": cohort_sha,
                "source_package_fingerprint": source_package_fingerprint(package),
            }
        except Exception:
            session.rollback()
            raise


def command_retry_failed(args: argparse.Namespace, factory) -> dict[str, Any]:
    """Explicitly requeue one exact failed Run A job after operator review."""

    _require_token(args.confirm_retry, RETRY_TOKEN)
    report, package, cohort, discovery = _critical_preflight(args, factory)
    try:
        job_id = UUID(args.job_id)
    except ValueError as error:
        raise OperatorError("JOB_MISMATCH", "job-id must be a UUID") from error

    now = _utc_now()
    with factory() as session:
        try:
            job = session.scalar(
                select(WorkerJob).where(WorkerJob.id == job_id).with_for_update()
            )
            metadata = dict(job.schedule_metadata or {}) if job is not None else {}
            expected_metadata = {
                "source_path": str(Path(args.artifact).resolve()),
                "xsd_path": str(Path(args.xsd).resolve()),
                "expected_sha256": package["artifact_sha256"],
                "expected_xsd_sha256": package["xsd_sha256"],
                "mode": "controlled_live",
                "pilot_enabled": True,
                "pilot_environment": PILOT_ENVIRONMENT,
                "cohort_inns": sorted(cohort),
                "discovery_page_url": discovery.release.discovery_page_url,
                "artifact_url": discovery.release.artifact_url,
                "xsd_url": discovery.release.xsd_url,
                "official_actual_until": (
                    discovery.release.official_actual_until.isoformat()
                ),
                "data_as_of": discovery.release.data_as_of.isoformat(),
                "source_as_of": discovery.release.source_as_of.isoformat(),
            }
            expected_semantic_identity = controlled_live_semantic_job_identity(
                artifact_sha256=package["artifact_sha256"],
                xsd_sha256=package["xsd_sha256"],
                release=discovery.release,
                config=ControlledLivePilotConfig(
                    enabled=True, cohort_inns=frozenset(cohort)
                ),
            )
            stored_semantic_identity = metadata.get("semantic_identity")
            if (
                job is None
                or (
                    job.source_id,
                    job.job_type,
                    job.handler_version,
                    job.status,
                )
                != (
                    SOURCE_ID,
                    "fns_tax_debt_controlled_live",
                    CONTROLLED_LIVE_HANDLER_VERSION,
                    "failed",
                )
                or any(
                    metadata.get(key) != value
                    for key, value in expected_metadata.items()
                )
                or (
                    stored_semantic_identity is not None
                    and stored_semantic_identity != expected_semantic_identity
                )
            ):
                raise OperatorError(
                    "JOB_MISMATCH",
                    "failed job does not match the current approved S02 package and cohort",
                )
            previous_run = session.scalar(
                select(WorkerRun)
                .where(WorkerRun.job_id == job.id)
                .order_by(WorkerRun.attempt_no.desc())
                .limit(1)
            )
            if (
                previous_run is None
                or previous_run.status != "failed"
                or previous_run.attempt_no >= job.max_attempts
            ):
                raise OperatorError(
                    "JOB_MISMATCH",
                    "failed job has no reviewed retry capacity",
                )
            require_durable_approval(session)
            if session.scalar(_runnable_query(now).limit(1)) is not None:
                raise OperatorError(
                    "QUEUE_NOT_EXCLUSIVE",
                    "another worker job is runnable before the reviewed retry",
                )
            job.status = "queued"
            job.next_attempt_at = None
            job.updated_at = now
            session.flush()
            queue_guard(session, expected_job_id=job.id, now=now)
            session.commit()
            return {
                "status": "RETRY_QUEUED",
                "job_id": job.id,
                "previous_run_id": previous_run.id,
                "previous_attempt_no": previous_run.attempt_no,
                "next_attempt_no": previous_run.attempt_no + 1,
                "cohort_sha256": report["cohort_sha256"],
                "source_package_fingerprint": report["source_package_fingerprint"],
            }
        except Exception:
            session.rollback()
            raise


class _ClaimClock:
    """Use the guard instant for claim eligibility, then resume real UTC time."""

    def __init__(self, claim_at: datetime) -> None:
        self.claim_at = claim_at
        self.first = True

    def __call__(self) -> datetime:
        if self.first:
            self.first = False
            return self.claim_at
        return _utc_now()


class _GuardedSessionFactory:
    """Give WorkerExecutor the already-guarded session for its one claim."""

    def __init__(self, guarded_session: Session, fallback_factory) -> None:
        self.guarded_session = guarded_session
        self.fallback_factory = fallback_factory
        self._used = False

    def __call__(self):
        if not self._used:
            self._used = True
            return self.guarded_session
        return self.fallback_factory()


def _start_repeatable_queue_guard(factory) -> Session:
    session = factory()
    try:
        # The guard query and WorkerExecutor's SELECT ... FOR UPDATE must see
        # one immutable runnable-queue snapshot. A concurrent insert/update
        # is therefore invisible or causes serialization failure; it cannot
        # make a different job appear between proof and claim.
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    except Exception as error:
        session.close()
        raise OperatorError(
            "QUEUE_NOT_EXCLUSIVE",
            "cannot establish a repeatable-read queue guard transaction",
            details={"exception_type": error.__class__.__name__},
        ) from error
    return session


def baseline_queue_guard(
    session: Session,
    *,
    expected_job_id: UUID,
    now: datetime,
    cohort_sha256: str,
    artifact_sha256: str,
    xsd_sha256: str,
) -> WorkerJob:
    job = session.get(WorkerJob, expected_job_id)
    expected = (SOURCE_ID, "fns_tax_debt_baseline", BASELINE_HANDLER_VERSION)
    metadata = dict(job.schedule_metadata or {}) if job is not None else {}
    scheduled_cohort = tuple(
        sorted(str(value) for value in metadata.get("cohort_inns") or ())
    )
    scheduled_cohort_sha = sha256(
        ("".join(f"{inn}\n" for inn in scheduled_cohort)).encode("ascii")
    ).hexdigest()
    if (
        job is None
        or (job.source_id, job.job_type, job.handler_version) != expected
        or job.status not in {"queued", "retry_scheduled"}
        or metadata.get("mode") != "controlled_live"
        or metadata.get("job_mode") != "official_baseline"
        or metadata.get("expected_sha256") != artifact_sha256
        or metadata.get("expected_xsd_sha256") != xsd_sha256
        or scheduled_cohort_sha != cohort_sha256
    ):
        raise OperatorError(
            "JOB_MISMATCH", "expected generation-0 worker job is not runnable"
        )
    require_baseline_approval(
        session,
        cohort_sha256=cohort_sha256,
        artifact_sha256=artifact_sha256,
        xsd_sha256=xsd_sha256,
    )
    ordered = session.scalars(_runnable_query(now)).all()
    if not ordered or ordered[0].id != expected_job_id:
        raise OperatorError(
            "QUEUE_NOT_EXCLUSIVE",
            "expected S02 baseline job is not the exact next WorkerExecutor claim",
            details={"next_job_id": str(ordered[0].id) if ordered else None},
        )
    return job


def command_prepare_baseline(args: argparse.Namespace, factory) -> dict[str, Any]:
    _require_token(args.confirm_prepare_baseline, BASELINE_PREPARE_TOKEN)
    if not MIN_TIMEOUT_SECONDS <= args.timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise OperatorError(
            "OPERATOR_ERROR",
            f"timeout-seconds must be within {MIN_TIMEOUT_SECONDS}..{MAX_TIMEOUT_SECONDS}",
        )
    with factory() as session:
        try:
            report, package, cohort, discovery = build_baseline_preparation_report(
                session,
                source_package=args.source_package,
                artifact=args.artifact,
                xsd=args.xsd,
                cohort_path=args.cohort,
                expected_main_sha=args.expected_main_sha,
            )
        finally:
            session.rollback()

    with factory() as session:
        require_baseline_approval(
            session,
            cohort_sha256=report["cohort_sha256"],
            artifact_sha256=report["artifact_sha256"],
            xsd_sha256=report["xsd_sha256"],
        )
        if report["state"] == "existing":
            session.rollback()
            return {
                **report,
                "status": "BASELINE_READY",
                "created": False,
                "outside_cohort_facts": 0,
            }
        try:
            creation = enqueue_fns_tax_debt_baseline_job(
                session,
                source_path=args.artifact,
                xsd_path=args.xsd,
                artifact_store=args.artifact_store,
                discovery=discovery,
                config=BaselinePreparationConfig(cohort_inns=frozenset(cohort)),
                # Reuse the frozen acquisition coordinate so Run A stages the
                # same checksum-addressed official manifest.
                retrieved_at=package["retrieved_at"],
                expected_sha256=package["artifact_sha256"],
                expected_xsd_sha256=package["xsd_sha256"],
                timeout_seconds=args.timeout_seconds,
            )
            if creation.job.status not in {"queued", "retry_scheduled"}:
                raise OperatorError(
                    "JOB_MISMATCH",
                    "idempotent baseline job is not runnable or succeeded",
                    details={"status": creation.job.status},
                )
            baseline_queue_guard(
                session,
                expected_job_id=creation.job.id,
                now=_utc_now(),
                cohort_sha256=report["cohort_sha256"],
                artifact_sha256=report["artifact_sha256"],
                xsd_sha256=report["xsd_sha256"],
            )
            session.commit()
            job_id = creation.job.id
            created = creation.created
        except Exception:
            session.rollback()
            raise

    guard_at = _utc_now()
    registry = HandlerRegistry()
    guard_session = _start_repeatable_queue_guard(factory)
    try:
        baseline_queue_guard(
            guard_session,
            expected_job_id=job_id,
            now=guard_at,
            cohort_sha256=report["cohort_sha256"],
            artifact_sha256=report["artifact_sha256"],
            xsd_sha256=report["xsd_sha256"],
        )
        register_fns_tax_debt_baseline_handler(guard_session, registry)
    except Exception:
        guard_session.rollback()
        guard_session.close()
        raise
    executor = WorkerExecutor(
        session_factory=_GuardedSessionFactory(guard_session, factory),
        registry=registry,
        worker_id=args.worker_id,
        clock=_ClaimClock(guard_at),
    )
    try:
        run_id = executor.run_once()
    except Exception as error:
        raise OperatorError(
            "RUN_FAILED",
            "S02 generation-0 Worker execution failed",
            details={"exception_type": error.__class__.__name__},
        ) from error
    finally:
        guard_session.close()
    if run_id is None:
        raise OperatorError("JOB_MISMATCH", "WorkerExecutor did not claim baseline job")

    with factory() as session:
        run = session.get(WorkerRun, run_id)
        if run is None or run.job_id != job_id or run.status != "succeeded":
            raise OperatorError("RUN_FAILED", "S02 baseline WorkerRun did not succeed")
        readiness = database_readiness(session, cohort=cohort)
        outside_count = int(
            session.scalar(
                select(func.count())
                .select_from(CompanyTaxDebtSnapshot)
                .join(Company, Company.id == CompanyTaxDebtSnapshot.company_id)
                .where(
                    CompanyTaxDebtSnapshot.publication_generation == 0,
                    Company.inn.not_in(cohort),
                )
            )
            or 0
        )
        if outside_count:
            raise OperatorError(
                "RUN_FAILED", "S02 baseline published facts outside approved cohort"
            )
        return {
            **report,
            "status": "BASELINE_READY",
            "created": created,
            "job_id": job_id,
            "run_id": run.id,
            "baseline_generation": readiness["baseline_generation"],
            "worker_generation": readiness["worker_generation"],
            "counters": _worker_run_counters(run),
            "outside_cohort_facts": outside_count,
        }


def command_run_once(args: argparse.Namespace, factory) -> dict[str, Any]:
    _require_token(args.confirm_run, RUN_TOKEN)
    check_main_sha(expected=args.expected_main_sha)
    try:
        job_id = UUID(args.job_id)
    except ValueError as error:
        raise OperatorError("JOB_MISMATCH", "job-id must be a UUID") from error
    guard_at = _utc_now()
    registry = HandlerRegistry()
    guard_session = _start_repeatable_queue_guard(factory)
    try:
        before = _generation_state(guard_session)
        queue_guard(guard_session, expected_job_id=job_id, now=guard_at)
        register_fns_tax_debt_controlled_live_handler(guard_session, registry)
    except Exception:
        guard_session.rollback()
        guard_session.close()
        raise
    guarded_factory = _GuardedSessionFactory(guard_session, factory)
    executor = WorkerExecutor(
        session_factory=guarded_factory,
        registry=registry,
        worker_id=args.worker_id,
        clock=_ClaimClock(guard_at),
    )
    try:
        run_id = executor.run_once()
    except Exception as error:
        raise OperatorError(
            "RUN_FAILED",
            "S02 worker execution failed",
            details={"exception_type": error.__class__.__name__},
        ) from error
    finally:
        guard_session.close()
    if run_id is None:
        raise OperatorError("JOB_MISMATCH", "WorkerExecutor did not claim a job")
    with factory() as session:
        run = session.get(WorkerRun, run_id)
        if run is None or run.job_id != job_id:
            raise OperatorError(
                "JOB_MISMATCH", "returned WorkerRun is not linked to expected job"
            )
        after = _generation_state(session)
        return {
            "status": run.status,
            "job_id": job_id,
            "run_id": run.id,
            "fencing_token": run.fencing_token,
            "handler_version": run.handler_version,
            "counters": {
                "records_seen": run.records_seen,
                "records_written": run.records_written,
                "records_rejected": run.records_rejected,
                "records_duplicated": run.records_duplicated,
                "records_published": run.records_published,
            },
            "generation_before": before,
            "generation_after": after,
        }


def command_status(args: argparse.Namespace, factory) -> dict[str, Any]:
    now = _utc_now()
    with factory() as session:
        try:
            monitoring = get_fns_tax_debt_pilot_monitoring(session, now=now)
            raw_pointer = monitoring.pop("active_raw_pointer", None)
            monitoring["active_raw_pointer_identity"] = (
                sha256(str(raw_pointer).encode("utf-8")).hexdigest()
                if raw_pointer
                else None
            )
            monitoring["errors"] = _safe_errors(list(monitoring.get("errors") or []))
            selected = None
            last_run = None
            if args.job_id:
                try:
                    job_id = UUID(args.job_id)
                except ValueError as error:
                    raise OperatorError(
                        "JOB_MISMATCH", "job-id must be a UUID"
                    ) from error
                job = session.get(WorkerJob, job_id)
                if job is None:
                    raise OperatorError(
                        "STATUS_NOT_FOUND", "selected job does not exist"
                    )
                selected = {
                    "job_id": job.id,
                    "source_id": job.source_id,
                    "job_type": job.job_type,
                    "handler_version": job.handler_version,
                    "status": job.status,
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                }
                run = session.scalar(
                    select(WorkerRun)
                    .where(WorkerRun.job_id == job.id)
                    .order_by(WorkerRun.started_at.desc())
                    .limit(1)
                )
                if run:
                    last_run = {
                        "run_id": run.id,
                        "status": run.status,
                        "started_at": run.started_at,
                        "finished_at": run.finished_at,
                        "errors": _safe_errors(list(run.errors or [])),
                    }
            state = _generation_state(session)
            return {
                "status": "OK",
                "monitoring": monitoring,
                "selected_job": selected,
                "last_run": last_run,
                "publication": state,
            }
        finally:
            session.rollback()


def command_rollback(args: argparse.Namespace, factory) -> dict[str, Any]:
    _require_token(args.confirm_rollback, ROLLBACK_TOKEN)
    check_main_sha(expected=args.expected_main_sha)
    with factory() as session:
        try:
            before = _generation_state(session)
            if (
                before["worker_generation"] is None
                or before["rollback_pointer_identity"] is None
            ):
                raise OperatorError(
                    "ROLLBACK_NOT_AVAILABLE", "S02 rollback generation is unavailable"
                )
            if before["worker_generation"] != args.expected_generation:
                raise OperatorError(
                    "ROLLBACK_NOT_AVAILABLE",
                    "current generation differs from expected-generation",
                    details={"current_generation": before["worker_generation"]},
                )
            try:
                rollback_fns_tax_debt_generation(
                    session,
                    expected_generation=args.expected_generation,
                    now=_utc_now(),
                )
            except Exception as error:
                raise OperatorError(
                    "ROLLBACK_NOT_AVAILABLE", "S02 rollback operation failed"
                ) from error
            session.commit()
        except Exception:
            session.rollback()
            raise
    with factory() as session:
        return {
            "status": "ROLLED_BACK",
            "before": before,
            "after": _generation_state(session),
        }


def command_evidence(args: argparse.Namespace, factory) -> dict[str, Any]:
    with factory() as session:
        try:
            try:
                fact, risk, summary, projection = (
                    calculate_s02_vertical_slice_from_persisted(
                        session, args.company_id, calculated_at=_utc_now()
                    )
                )
            except Exception as error:
                raise OperatorError(
                    "EVIDENCE_NOT_AVAILABLE", "S02 evidence calculation failed"
                ) from error
            fact_data = _json_safe(fact)
            risk_data = _json_safe(risk)
            summary_data = _json_safe(summary)
            projection_data = _json_safe(projection)
            return {
                "status": "OK",
                "company_id": args.company_id,
                "fact": {
                    "state": fact_data.get("state"),
                    "amount": fact_data.get("amount"),
                    "amount_as_of_date": fact_data.get("amount_as_of_date"),
                    "total_arrears": fact_data.get("total_arrears"),
                    "total_penalties": fact_data.get("total_penalties"),
                    "total_fines": fact_data.get("total_fines"),
                },
                "risk": {
                    "result": risk_data.get("overall_result"),
                    "factors": [
                        {
                            "factor_code": item.get("factor_code"),
                            "severity": item.get("severity"),
                            "hard_blocker": item.get("hard_blocker"),
                        }
                        for item in risk_data.get("factors") or []
                    ],
                },
                "summary": {
                    "conclusion": summary_data.get("overall_conclusion"),
                },
                "coverage_freshness": {
                    "freshness": projection_data.get("freshness"),
                    "coverage": projection_data.get("coverage"),
                },
                "limitations": projection_data.get("limitation_states") or [],
            }
        finally:
            session.rollback()


def _argument_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OperatorError("OPERATOR_ERROR", "timestamp must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OperatorError("OPERATOR_ERROR", "timestamp must contain a timezone")
    return parsed.astimezone(timezone.utc)


def _add_database(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", help="override canonical DATABASE_URL")


def _add_preflight_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-package", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--xsd", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--expected-main-sha", required=True)
    _add_database(parser)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight")
    _add_preflight_inputs(preflight)
    approve = sub.add_parser("approve")
    _add_preflight_inputs(approve)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--approved-at", required=True)
    approve.add_argument("--confirm-controlled-live", required=True)
    baseline_approve = sub.add_parser("approve-baseline")
    _add_preflight_inputs(baseline_approve)
    baseline_approve.add_argument("--approved-by", required=True)
    baseline_approve.add_argument("--approved-at", required=True)
    baseline_approve.add_argument("--confirm-baseline-approval", required=True)
    prepare_baseline = sub.add_parser("prepare-baseline")
    _add_preflight_inputs(prepare_baseline)
    prepare_baseline.add_argument("--artifact-store", type=Path, required=True)
    prepare_baseline.add_argument("--worker-id", required=True)
    prepare_baseline.add_argument(
        "--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS
    )
    prepare_baseline.add_argument("--confirm-prepare-baseline", required=True)
    enqueue = sub.add_parser("enqueue")
    _add_preflight_inputs(enqueue)
    enqueue.add_argument("--artifact-store", type=Path, required=True)
    enqueue.add_argument("--retrieved-at", required=True)
    enqueue.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    enqueue.add_argument("--confirm-enqueue", required=True)
    retry = sub.add_parser("retry-failed")
    _add_preflight_inputs(retry)
    retry.add_argument("--job-id", required=True)
    retry.add_argument("--confirm-retry", required=True)
    run = sub.add_parser("run-once")
    _add_database(run)
    run.add_argument("--job-id", required=True)
    run.add_argument("--worker-id", required=True)
    run.add_argument("--expected-main-sha", required=True)
    run.add_argument("--confirm-run", required=True)
    status = sub.add_parser("status")
    _add_database(status)
    status.add_argument("--job-id")
    rollback = sub.add_parser("rollback")
    _add_database(rollback)
    rollback.add_argument("--expected-generation", type=int, required=True)
    rollback.add_argument("--expected-main-sha", required=True)
    rollback.add_argument("--confirm-rollback", required=True)
    evidence = sub.add_parser("evidence")
    _add_database(evidence)
    evidence.add_argument("--company-id", type=int, required=True)
    return parser.parse_args(argv)


def session_factory(database_url: str | None):
    if database_url:
        engine = create_engine(database_url, pool_pre_ping=True)
        return sessionmaker(
            bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
    from app.database.postgres import SessionLocal

    return SessionLocal


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], dict[str, Any]]] = {
    "preflight": command_preflight,
    "approve": command_approve,
    "approve-baseline": command_approve_baseline,
    "prepare-baseline": command_prepare_baseline,
    "enqueue": command_enqueue,
    "retry-failed": command_retry_failed,
    "run-once": command_run_once,
    "status": command_status,
    "rollback": command_rollback,
    "evidence": command_evidence,
}


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    try:
        result = COMMANDS[args.command](args, session_factory(args.database_url))
    except OperatorError as error:
        emit_json(
            {
                "status": "BLOCKED",
                "error": error.code,
                "message": error.public_message,
                "details": error.public_details(),
                "blockers": [error.code],
            }
        )
        return EXIT_CODES.get(error.code, EXIT_CODES["OPERATOR_ERROR"])
    except Exception:
        emit_json(
            {
                "status": "BLOCKED",
                "error": "OPERATOR_ERROR",
                "message": "unexpected operator failure",
                "details": {},
                "blockers": ["OPERATOR_ERROR"],
            }
        )
        return EXIT_CODES["OPERATOR_ERROR"]
    emit_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
