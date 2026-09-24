"""Fail-closed operator CLI for the bounded S02 controlled-live pilot.

The module deliberately contains orchestration and validation only. Parsing,
normalization, publication, worker execution, monitoring, rollback, Risk and
Summary remain owned by their accepted application services.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
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
    CompanyTaxDebtSnapshot,
    FnsTaxDebtNormalizedRecord,
    FnsTaxDebtPilotState,
    FnsTaxDebtPublicationGeneration,
    FnsTaxDebtRawArtifact,
)
from app.models.worker import (
    WorkerHandlerRegistration,
    WorkerJob,
    WorkerPublicationState,
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
    MASS_INGESTION_ENABLED,
    OFFICIAL_SOURCE_PAGE,
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
RUN_TOKEN = "S02_CONTROLLED_LIVE_RUN"
ROLLBACK_TOKEN = "S02_CONTROLLED_LIVE_ROLLBACK"
DEFAULT_TIMEOUT_SECONDS = 3600
MIN_TIMEOUT_SECONDS = 600
MAX_TIMEOUT_SECONDS = 7200
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
CHECKSUM_RE = re.compile(r"[0-9a-f]{64}\Z")
DOWNLOAD_HOST = "file.nalog.ru"
DOWNLOAD_DIRECTORY = f"/opendata/{DATASET_ID}/"

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
        "pilot_dataset",
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


def database_readiness(session: Session, *, cohort: tuple[str, ...]) -> dict[str, Any]:
    table_names = set(inspect(session.get_bind()).get_table_names())
    required_tables = {
        "companies",
        "data_sets",
        "worker_jobs",
        "worker_runs",
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
    run: WorkerRun | None = None
    job: WorkerJob | None = None
    artifact: FnsTaxDebtRawArtifact | None = None
    raw_count = 0
    fact_count = 0
    pointer_metadata = (
        dict(pointer.validation_metadata)
        if pointer is not None and isinstance(pointer.validation_metadata, Mapping)
        else {}
    )
    validation = (
        dict(pointer_metadata.get("validation"))
        if isinstance(pointer_metadata.get("validation"), Mapping)
        else {}
    )
    raw_pointer = validation.get("raw_pointer")
    checksum = pointer_metadata.get("checksum")
    if dataset is not None:
        baseline_row = session.scalar(
            select(FnsTaxDebtPublicationGeneration).where(
                FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
                FnsTaxDebtPublicationGeneration.generation == 0,
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
        if isinstance(raw_pointer, str) and raw_pointer:
            artifact = session.scalar(
                select(FnsTaxDebtRawArtifact).where(
                    FnsTaxDebtRawArtifact.dataset_id == dataset.id,
                    FnsTaxDebtRawArtifact.artifact_reference == raw_pointer,
                )
            )
    if pointer is not None and pointer.published_by_run_id is not None:
        run = session.get(WorkerRun, pointer.published_by_run_id)
    if run is not None:
        job = session.get(WorkerJob, run.job_id)

    baseline_actual_until = None
    coverage: dict[str, Any] = {}
    if dataset is not None and isinstance(dataset.coverage, Mapping):
        coverage = dict(dataset.coverage)
        raw_actual_until = dict(dataset.coverage or {}).get("official_actual_until")
        try:
            baseline_actual_until = (
                date.fromisoformat(str(raw_actual_until)) if raw_actual_until else None
            )
        except ValueError:
            baseline_actual_until = None

    failed: list[str] = []

    def require(condition: Any, name: str) -> None:
        if not condition:
            failed.append(name)

    require(dataset is not None, "dataset_missing")
    if dataset is not None:
        require(dataset.code == DATASET_CODE, "dataset_code")
        require(dataset.operational_status == "ready", "dataset_status")
        require(dataset.source_as_of is not None, "dataset_source_as_of")
        require(dataset.retrieved_at is not None, "dataset_retrieved_at")
        require(dataset.last_data_date is not None, "dataset_last_data_date")
        require(dataset.published_at is not None, "dataset_published_at")
        require(_usable_count(dataset.record_count), "dataset_record_count")
        require(bool(coverage), "dataset_coverage")
        require(baseline_actual_until is not None, "dataset_official_actual_until")
        if dataset.source_as_of is not None and dataset.retrieved_at is not None:
            require(dataset.source_as_of <= dataset.retrieved_at, "dataset_time_order")
        if dataset.last_data_date is not None and baseline_actual_until is not None:
            require(
                dataset.last_data_date <= baseline_actual_until,
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
            _usable_count(pointer.generation) and pointer.generation > 0,
            "publication_generation",
        )
        require(bool(pointer_metadata), "publication_validation_metadata")
        require(
            isinstance(raw_pointer, str) and bool(raw_pointer),
            "publication_raw_pointer",
        )
        require(
            isinstance(checksum, str) and CHECKSUM_RE.fullmatch(checksum) is not None,
            "publication_checksum",
        )

    require(run is not None, "publication_run_missing")
    if run is not None:
        require(run.status == "succeeded", "publication_run_status")
        require(run.finished_at is not None, "publication_run_finished_at")
        require(
            all(_usable_count(value) for value in _worker_run_counters(run).values()),
            "publication_run_counters",
        )
    require(job is not None, "publication_job_missing")
    if job is not None and run is not None:
        require(job.source_id == SOURCE_ID, "publication_job_source")
        require(
            job.job_type
            in {
                "fns_tax_debt_baseline",
                "fns_tax_debt_fixture",
                "fns_tax_debt_controlled_live",
            },
            "publication_job_type",
        )
        require(job.handler_version == run.handler_version, "publication_job_handler")
        require(job.status == "succeeded", "publication_job_status")

    require(artifact is not None, "raw_artifact_missing")
    if artifact is not None and dataset is not None and run is not None:
        require(artifact.dataset_id == dataset.id, "raw_artifact_dataset")
        require(artifact.artifact_reference == raw_pointer, "raw_artifact_pointer")
        require(artifact.sha256 == checksum, "raw_artifact_checksum")
        require(artifact.first_worker_run_id == run.id, "raw_artifact_run")
        require(
            artifact.source_as_of == dataset.source_as_of,
            "raw_artifact_source_as_of",
        )
        require(
            artifact.retrieved_at == dataset.retrieved_at,
            "raw_artifact_retrieved_at",
        )

    require(baseline_row is not None, "baseline_generation_missing")
    if (
        baseline_row is not None
        and dataset is not None
        and pointer is not None
        and run is not None
        and artifact is not None
    ):
        require(baseline_row.generation == 0, "baseline_generation")
        require(baseline_row.publication_scope == "baseline", "baseline_scope")
        require(baseline_row.status == "baseline", "baseline_status")
        require(baseline_row.dataset_id == dataset.id, "baseline_dataset")
        require(baseline_row.artifact_id == artifact.id, "baseline_artifact")
        require(baseline_row.worker_run_id == run.id, "baseline_run")
        require(
            baseline_row.staging_pointer == pointer.active_pointer,
            "baseline_staging_pointer",
        )
        require(
            baseline_row.raw_pointer == artifact.artifact_reference,
            "baseline_raw_pointer",
        )
        require(baseline_row.checksum == artifact.sha256, "baseline_checksum")
        require(
            baseline_row.source_as_of == dataset.source_as_of,
            "baseline_source_as_of",
        )
        require(
            baseline_row.retrieved_at == dataset.retrieved_at,
            "baseline_retrieved_at",
        )
        require(
            baseline_row.last_data_date == dataset.last_data_date,
            "baseline_last_data_date",
        )
        require(
            baseline_row.official_actual_until == baseline_actual_until,
            "baseline_official_actual_until",
        )
        require(
            baseline_row.record_count == dataset.record_count,
            "baseline_record_count",
        )
        require(_mapping_dict(baseline_row.coverage) == coverage, "baseline_coverage")
        require(
            _mapping_dict(baseline_row.counters) == _worker_run_counters(run),
            "baseline_counters",
        )
        require(
            _mapping_dict(baseline_row.validation_metadata) == validation,
            "baseline_validation_metadata",
        )
        require(
            _mapping_dict(baseline_row.dataset_metadata)
            == _dataset_metadata_snapshot(dataset),
            "baseline_dataset_metadata",
        )
        require(
            baseline_row.published_at == dataset.published_at, "baseline_published_at"
        )

    if pilot is not None and dataset is not None and artifact is not None:
        require(pilot.dataset_id == dataset.id, "pilot_dataset")
        require(pilot.generation == 0, "pilot_generation")
        require(pilot.baseline_generation == 0, "pilot_baseline_generation")
        require(pilot.normalized_generation == 0, "pilot_normalized_generation")
        require(pilot.fact_generation == 0, "pilot_fact_generation")
        require(pilot.query_generation == 0, "pilot_query_generation")
        require(
            pilot.active_raw_pointer in {None, artifact.artifact_reference},
            "pilot_raw_pointer",
        )
        require(pilot.active_checksum in {None, artifact.sha256}, "pilot_checksum")
        require(
            pilot.active_source_as_of in {None, dataset.source_as_of},
            "pilot_source_as_of",
        )
        require(
            pilot.active_retrieved_at in {None, dataset.retrieved_at},
            "pilot_retrieved_at",
        )
        require(
            pilot.baseline_data_date in {None, dataset.last_data_date},
            "pilot_data_date",
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
    if pointer is not None or fact_count or raw_count or normalized_count:
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
