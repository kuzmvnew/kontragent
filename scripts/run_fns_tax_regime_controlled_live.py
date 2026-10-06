"""Fail-closed HOME operator for the C6 FNS Tax Regime family.

``preflight`` is read-only: it discovers both official passports, validates
their atomic bundle identity, checks durable handler/source registration and
estimates disk requirements.  ``enqueue`` creates at most one durable Worker
job for the pinned bundle; it never executes the job or enables a scheduler.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal
from app.ingestion.fns_bulk_worker import (
    OFFICIAL_HOSTS,
    FnsReleaseBundle,
    discover_fns_release_bundle,
    enqueue_bulk_release_bundle,
)
from app.ingestion.fns_tax_regime import (
    FAMILY_DATASET_CODE,
    HANDLER_VERSION,
    IP_DATASET_CODE,
    IP_SOURCE_PAGE_URL,
    LEGAL_DATASET_CODE,
    LEGAL_SOURCE_PAGE_URL,
    SOURCE_ID,
    _family_spec,
    _member_specs,
)
from app.models.source import DataSet
from app.models.worker import WorkerHandlerRegistration, WorkerPublicationState
from app.services.factory_scale_service import FactoryScaleConfig


CONFIRM_TOKEN = "FNS_TAX_REGIME_CONTROLLED_LIVE"
MINIMUM_PRODUCTION_DISK_FREE_PERCENT = 10
MINIMUM_FIXED_FREE_BYTES = 5 * 1024**3
PEAK_STORAGE_MULTIPLIER = 4


@dataclass(frozen=True)
class DiskSnapshot:
    total: int
    used: int
    free: int


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _head_headers(url: str) -> Mapping[str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
        raise ValueError(f"unapproved FNS URL: {url}")
    request = Request(
        url,
        method="HEAD",
        headers={"User-Agent": "next.company-c6-preflight/1.0"},
    )
    with urlopen(request, timeout=60) as response:
        if int(getattr(response, "status", response.getcode())) != 200:
            raise RuntimeError(f"official FNS HEAD did not return HTTP 200: {url}")
        return dict(response.headers.items())


def _content_length(headers: Mapping[str, str], *, label: str) -> int:
    raw = next(
        (value for key, value in headers.items() if key.lower() == "content-length"),
        None,
    )
    if raw is None:
        raise RuntimeError(f"official FNS {label} has no Content-Length")
    try:
        size = int(raw)
    except ValueError as error:
        raise RuntimeError(
            f"official FNS {label} has invalid Content-Length"
        ) from error
    if size <= 0:
        raise RuntimeError(f"official FNS {label} is empty")
    return size


def _disk_anchor(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists():
        raise RuntimeError(f"no existing filesystem parent for RAW root: {path}")
    return candidate


def _resource_report(
    bundle: FnsReleaseBundle,
    *,
    raw_root: Path,
    head: Callable[[str], Mapping[str, str]] = _head_headers,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    config: FactoryScaleConfig | None = None,
) -> dict[str, Any]:
    artifacts: dict[str, dict[str, Any]] = {}
    total_download_bytes = 0
    for name, release in sorted(bundle.releases.items()):
        artifact_headers = head(release.artifact_url)
        xsd_headers = head(release.xsd_url)
        artifact_size = _content_length(
            artifact_headers, label=f"{name} artifact"
        )
        xsd_size = _content_length(xsd_headers, label=f"{name} XSD")
        total_download_bytes += artifact_size + xsd_size
        artifacts[name] = {
            "artifact_url": release.artifact_url,
            "artifact_size_bytes": artifact_size,
            "artifact_etag": next(
                (
                    value
                    for key, value in artifact_headers.items()
                    if key.lower() == "etag"
                ),
                None,
            ),
            "artifact_advertised_sha256": next(
                (
                    value
                    for key, value in artifact_headers.items()
                    if key.lower() == "x-amz-meta-sha256"
                ),
                None,
            ),
            "xsd_url": release.xsd_url,
            "xsd_size_bytes": xsd_size,
        }

    safety = config or FactoryScaleConfig.from_environment()
    usage = disk_usage(_disk_anchor(raw_root))
    snapshot = DiskSnapshot(
        total=int(usage.total),
        used=int(usage.used),
        free=int(usage.free),
    )
    estimated_peak = total_download_bytes * PEAK_STORAGE_MULTIPLIER
    free_after = snapshot.free - estimated_peak
    free_percent_after = (
        free_after * 100.0 / snapshot.total if snapshot.total else 0.0
    )
    configured_floor = int(safety.min_disk_free_percent)
    blockers = []
    if configured_floor < MINIMUM_PRODUCTION_DISK_FREE_PERCENT:
        blockers.append("production_disk_free_percent_was_lowered")
    if free_after < MINIMUM_FIXED_FREE_BYTES:
        blockers.append("fixed_free_space_floor_not_met_after_estimated_peak")
    if free_percent_after < MINIMUM_PRODUCTION_DISK_FREE_PERCENT:
        blockers.append("production_disk_free_percent_not_met_after_estimated_peak")
    return {
        "artifacts": artifacts,
        "download_bytes": total_download_bytes,
        "estimated_peak_staging_and_raw_bytes": estimated_peak,
        "estimation_policy": (
            "4x compressed ZIP+XSD total: download/RAW plus normalized JSONL, "
            "SQLite coalescing and temporary output"
        ),
        "raw_root": str(raw_root.resolve()),
        "filesystem": snapshot.__dict__,
        "free_bytes_after_estimated_peak": free_after,
        "free_percent_after_estimated_peak": round(free_percent_after, 3),
        "configured_factory_min_disk_free_percent": configured_floor,
        "required_min_disk_free_percent": MINIMUM_PRODUCTION_DISK_FREE_PERCENT,
        "required_fixed_free_bytes_after_peak": MINIMUM_FIXED_FREE_BYTES,
        "blockers": blockers,
    }


def _publication_identity(state: WorkerPublicationState | None) -> str | None:
    validation = dict(state.validation_metadata or {}) if state else {}
    return (validation.get("validation") or {}).get("release_identity")


def build_preflight_report(
    session: Session,
    *,
    bundle: FnsReleaseBundle,
    raw_root: Path,
    head: Callable[[str], Mapping[str, str]] = _head_headers,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    config: FactoryScaleConfig | None = None,
) -> dict[str, Any]:
    approval = session.get(
        WorkerHandlerRegistration,
        (SOURCE_ID, HANDLER_VERSION),
    )
    state = session.get(WorkerPublicationState, SOURCE_ID)
    datasets = {
        dataset.code: dataset
        for dataset in session.scalars(
            select(DataSet).where(
                DataSet.code.in_(
                    (FAMILY_DATASET_CODE, LEGAL_DATASET_CODE, IP_DATASET_CODE)
                )
            )
        )
    }
    blockers: list[str] = []
    if approval is None:
        blockers.append("durable_handler_approval_missing")
    elif not approval.approved or not approval.enabled or approval.live_mode:
        blockers.append("durable_handler_approval_inactive_or_live_mode")
    required_datasets = {
        FAMILY_DATASET_CODE,
        LEGAL_DATASET_CODE,
        IP_DATASET_CODE,
    }
    if set(datasets) != required_datasets:
        blockers.append("family_or_member_dataset_registration_missing")
    expected_source_urls = {
        FAMILY_DATASET_CODE: LEGAL_SOURCE_PAGE_URL,
        LEGAL_DATASET_CODE: LEGAL_SOURCE_PAGE_URL,
        IP_DATASET_CODE: IP_SOURCE_PAGE_URL,
    }
    source_url_mismatches = sorted(
        code
        for code, expected_url in expected_source_urls.items()
        if code in datasets and datasets[code].source_url != expected_url
    )
    if source_url_mismatches:
        blockers.append("dataset_source_url_mismatch")

    resources = _resource_report(
        bundle,
        raw_root=raw_root,
        head=head,
        disk_usage=disk_usage,
        config=config,
    )
    blockers.extend(resources["blockers"])
    current_identity = _publication_identity(state)
    return {
        "status": "READY_FOR_CONTROLLED_LIVE" if not blockers else "BLOCKED",
        "source_id": SOURCE_ID,
        "handler_version": HANDLER_VERSION,
        "family_dataset_code": FAMILY_DATASET_CODE,
        "member_dataset_codes": [LEGAL_DATASET_CODE, IP_DATASET_CODE],
        "bundle": bundle.as_metadata(),
        "current_publication_identity": current_identity,
        "new_release_exists": current_identity != bundle.identity,
        "durable_handler": (
            {
                "approved": approval.approved,
                "enabled": approval.enabled,
                "live_mode": approval.live_mode,
            }
            if approval is not None
            else None
        ),
        "datasets": {
            code: {
                "enabled": dataset.enabled,
                "source_url": dataset.source_url,
                "operational_status": str(dataset.operational_status),
                "last_success_at": dataset.last_success_at,
                "last_data_date": dataset.last_data_date,
                "official_actual_until": dataset.official_actual_until,
            }
            for code, dataset in sorted(datasets.items())
        },
        "resources": resources,
        "source_url_mismatches": source_url_mismatches,
        "blockers": blockers,
        "production_mutation": False,
    }


def _discover_bundle() -> FnsReleaseBundle:
    return discover_fns_release_bundle(_member_specs())


def _preflight(raw_root: Path) -> tuple[dict[str, Any], FnsReleaseBundle]:
    bundle = _discover_bundle()
    with SessionLocal() as session:
        report = build_preflight_report(
            session,
            bundle=bundle,
            raw_root=raw_root,
        )
        session.rollback()
    return report, bundle


def _enqueue(raw_root: Path, *, confirm: str) -> dict[str, Any]:
    if confirm != CONFIRM_TOKEN:
        raise RuntimeError("controlled-live confirmation token differs")
    report, bundle = _preflight(raw_root)
    if report["status"] != "READY_FOR_CONTROLLED_LIVE":
        raise RuntimeError(
            "controlled-live preflight is blocked: "
            + ", ".join(report["blockers"])
        )
    with SessionLocal() as session:
        state = session.get(WorkerPublicationState, SOURCE_ID)
        validation = dict(state.validation_metadata or {}) if state else {}
        current_identity = _publication_identity(state)
        same_release = current_identity == bundle.identity
        creation = enqueue_bulk_release_bundle(
            session,
            spec=_family_spec(),
            bundle=bundle,
            raw_root=raw_root,
            check_only=same_release,
            replay_pointer=state.active_pointer if same_release and state else None,
            replay_checksum=(
                str(validation.get("checksum") or "") if same_release else None
            ),
            extra_schedule_metadata={
                "controlled_live": True,
                "controlled_live_scope": SOURCE_ID,
                "operator": "c6-home-controlled-live-v1",
            },
        )
        session.commit()
        return {
            "status": "ENQUEUED" if creation.created else "ALREADY_ENQUEUED",
            "source_id": SOURCE_ID,
            "handler_version": HANDLER_VERSION,
            "job_id": str(creation.job.id),
            "job_status": creation.job.status,
            "job_type": creation.job.job_type,
            "check_only": same_release,
            "release_identity": bundle.identity,
            "production_scheduler_enabled": False,
            "worker_command": (
                "python -m scripts.run_data_readiness_scheduler "
                "--controlled-start --allow-source fns_tax_regime "
                "--allow-lane source_control --work-budget 1"
            ),
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="C6 FNS Tax Regime controlled-live HOME operator"
    )
    parser.add_argument(
        "command",
        choices=("preflight", "enqueue"),
    )
    parser.add_argument(
        "--raw-root",
        default=os.environ.get("FNS_RAW_ROOT", "var/raw/fns"),
    )
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    raw_root = Path(args.raw_root)
    if args.command == "preflight":
        report, _bundle = _preflight(raw_root)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default))
        if report["status"] != "READY_FOR_CONTROLLED_LIVE":
            raise SystemExit(2)
        return
    result = _enqueue(raw_root, confirm=args.confirm)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default))


if __name__ == "__main__":
    main()
