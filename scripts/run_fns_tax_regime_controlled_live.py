"""Fail-closed HOME operator for the C6 FNS Tax Regime family.

``preflight`` is read-only: it discovers both official passports, validates
their atomic bundle identity, checks durable handler/source registration and
estimates disk requirements.  ``enqueue`` creates at most one durable Worker
job for the pinned bundle; it never executes the job or enables a scheduler.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal
from app.ingestion.fns_bulk_worker import (
    OFFICIAL_HOSTS,
    FnsReleaseBundle,
    discover_fns_release_bundle,
    enqueue_bulk_release_bundle,
    release_operational_status,
    validate_official_release_url,
)
from app.contracts.company_view_v1 import DataState
from app.contracts.data_readiness import OperationalStatus
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
from app.models.tax_regime import CompanyTaxRegimeSnapshot
from app.models.worker import WorkerHandlerRegistration, WorkerPublicationState
from app.services.factory_scale_service import FactoryScaleConfig
from app.services.fns_tax_regime_readiness import (
    evaluate_family_readiness,
    validate_same_release_identity_chain,
)
from app.services.fns_tax_regime_publication_admission import (
    PublicationLifecycle,
    canonical_fingerprint,
    canonical_sha256,
    validate_publication_lifecycle,
)
from app.worker.errors import InvalidDataError


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


def _bundle_validation_errors(bundle: FnsReleaseBundle, *, now: datetime) -> list[str]:
    """Validate direct preflight inputs as strictly as discovered passports."""

    specs = _member_specs()
    if set(bundle.releases) != set(specs):
        return ["required_member_set_mismatch"]
    errors = []
    for name, spec in specs.items():
        release = bundle.releases[name]
        if release.source_page_url != spec.source_page_url:
            errors.append(f"{name}:source_page_mismatch")
        try:
            _artifact_name, artifact_version = validate_official_release_url(
                spec, release.artifact_url, artifact=True
            )
            _xsd_name, xsd_version = validate_official_release_url(
                spec, release.xsd_url, artifact=False
            )
        except InvalidDataError:
            errors.append(f"{name}:official_url_invalid")
        else:
            if artifact_version != xsd_version:
                errors.append(f"{name}:structure_version_mismatch")
    if release_operational_status(bundle.actual_until, now=now) != OperationalStatus.CURRENT:
        errors.append("discovered_bundle_not_current")
    return errors


def _accepted_pointer_matches_bundle(
    state: WorkerPublicationState | None, bundle: FnsReleaseBundle, raw_root: Path
) -> bool:
    """Pin SAME_RELEASE replay to the descriptor path publication creates."""

    pointer = state.active_pointer if state else None
    if not isinstance(pointer, str):
        return False
    expected = (
        raw_root / SOURCE_ID / "bundles" / bundle.identity / "normalized-bundle.json"
    ).resolve().as_uri()
    return pointer == expected


def _accepted_descriptor_verdict(
    proof: Any, bundle: FnsReleaseBundle, raw_root: Path
) -> dict[str, Any]:
    """Read-only proof that the accepted descriptor and members match discovery."""

    path = (
        raw_root / SOURCE_ID / "bundles" / bundle.identity / "normalized-bundle.json"
    ).resolve()
    result: dict[str, Any] = {
        "valid": False,
        "reasons": [],
        "actual_checksum": None,
        "members": {},
    }
    reasons: list[str] = result["reasons"]
    if proof.active_pointer != path.as_uri():
        reasons.append("accepted_descriptor_pointer_mismatch")
        return result
    try:
        payload = path.read_bytes()
    except OSError:
        reasons.append("accepted_descriptor_missing")
        return result
    actual_checksum = sha256(payload).hexdigest()
    result["actual_checksum"] = actual_checksum
    if actual_checksum != proof.checksum:
        reasons.append("accepted_descriptor_checksum_mismatch")
        return result
    try:
        descriptor = json.loads(payload)
    except (ValueError, UnicodeError):
        reasons.append("accepted_descriptor_json_invalid")
        return result
    if not isinstance(descriptor, dict):
        reasons.append("accepted_descriptor_structure_invalid")
        return result
    if (
        type(descriptor.get("manifest_version")) is not int
        or descriptor["manifest_version"] != 1
        or descriptor.get("immutable") is not True
        or descriptor.get("source_id") != SOURCE_ID
        or descriptor.get("release_identity") != bundle.identity
    ):
        reasons.append("accepted_descriptor_family_mismatch")
    members = descriptor.get("members")
    if not isinstance(members, dict) or set(members) != {"legal", "ip"}:
        reasons.append("accepted_descriptor_member_map_invalid")
        return result
    for name, code in (("legal", LEGAL_DATASET_CODE), ("ip", IP_DATASET_CODE)):
        member = members[name]
        member_reasons: list[str] = []
        if not isinstance(member, dict):
            member_reasons.append("structure_invalid")
            member = {}
        release = member.get("release")
        release = release if isinstance(release, dict) else {}
        fresh = bundle.releases[name]
        if member.get("dataset_code") != code or any(
            release.get(field) != expected for field, expected in (
                ("release_identity", fresh.identity),
                ("source_data_date", fresh.source_data_date.isoformat()),
                ("source_page_url", fresh.source_page_url),
                ("artifact_url", fresh.artifact_url),
                ("xsd_url", fresh.xsd_url),
            )
        ):
            member_reasons.append("release_mismatch")
        normalized = member.get("normalized_sha256")
        artifact = member.get("artifact_sha256")
        if not canonical_sha256(normalized) or not canonical_sha256(artifact):
            member_reasons.append("checksum_invalid")
        else:
            expected_member_path = (
                raw_root / SOURCE_ID / artifact / f"normalized-{normalized}.jsonl"
            ).resolve()
            if member.get("staging_pointer") != expected_member_path.as_uri():
                member_reasons.append("pointer_mismatch")
            else:
                try:
                    with expected_member_path.open("rb") as stream:
                        digest = sha256()
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                except OSError:
                    member_reasons.append("normalized_file_missing")
                else:
                    if digest.hexdigest() != normalized:
                        member_reasons.append("normalized_checksum_mismatch")
        result["members"][name] = {"valid": not member_reasons, "reasons": member_reasons}
        if member_reasons:
            reasons.append(f"accepted_descriptor_{name}_invalid")
    result["valid"] = not reasons
    return result


def _published_fact_count(session: Session) -> int:
    return int(session.scalar(
        select(func.count())
        .select_from(CompanyTaxRegimeSnapshot)
        .join(DataSet, CompanyTaxRegimeSnapshot.dataset_id == DataSet.id)
        .where(DataSet.code.in_((LEGAL_DATASET_CODE, IP_DATASET_CODE)))
    ) or 0)


def _admission_fingerprint(
    *, bundle: FnsReleaseBundle, state: Any, datasets: Mapping[str, Any],
    approval: Any, fact_count: int, lifecycle: Any, readiness: Any,
    descriptor: Mapping[str, Any] | None,
) -> str:
    """Bind all safety-relevant DB observations, including mode-stable changes."""

    dataset_fields = (
        "id", "enabled", "source_url", "operational_status", "last_error",
        "last_success_at", "last_data_date", "source_as_of", "published_at",
        "official_actual_until", "freshness_policy", "freshness_threshold_seconds",
        "record_count", "coverage",
    )
    evidence = {
        "fresh_bundle": bundle.as_metadata(),
        "publication_lifecycle": lifecycle.lifecycle.value,
        "publication_proof": lifecycle.proof.as_evidence() if lifecycle.proof else None,
        "publication_validation_metadata": (
            state.validation_metadata if state is not None else None
        ),
        "published_fact_count": fact_count,
        "datasets": {
            code: {field: getattr(row, field, None) for field in dataset_fields}
            for code, row in sorted(datasets.items())
        },
        "handler": (
            {"source_id": getattr(approval, "source_id", SOURCE_ID),
             "handler_version": getattr(approval, "handler_version", HANDLER_VERSION),
             "approved": approval.approved, "enabled": approval.enabled,
             "live_mode": approval.live_mode}
            if approval is not None else None
        ),
        "family_readiness": {"state": readiness.state.value, "reason": readiness.reason},
        "descriptor": descriptor,
    }
    return canonical_fingerprint(evidence)


def build_preflight_report(
    session: Session,
    *,
    bundle: FnsReleaseBundle,
    raw_root: Path,
    head: Callable[[str], Mapping[str, str]] = _head_headers,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    config: FactoryScaleConfig | None = None,
    now: datetime | None = None,
    locked_evidence: tuple[Any, Any, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if locked_evidence is None:
        approval = session.get(WorkerHandlerRegistration, (SOURCE_ID, HANDLER_VERSION))
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
    else:
        approval, state, datasets = locked_evidence
    fact_count = _published_fact_count(session)
    lifecycle = validate_publication_lifecycle(
        state, datasets, published_fact_count=fact_count
    )
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

    current_identity = lifecycle.proof.release_identity if lifecycle.proof else None
    if lifecycle.lifecycle == PublicationLifecycle.CORRUPT:
        release_mode = "CORRUPT_PUBLICATION"
        blockers.extend(lifecycle.reasons)
    elif lifecycle.lifecycle == PublicationLifecycle.NO_PUBLICATION:
        release_mode = "INITIAL_RELEASE"
    else:
        release_mode = (
            "SAME_RELEASE" if current_identity == bundle.identity else "NEW_RELEASE"
        )
    readiness = evaluate_family_readiness(datasets, now=now)
    bundle_errors = _bundle_validation_errors(bundle, now=now)
    identity_chain = None
    pointer_valid = None
    descriptor_verdict = None
    if release_mode == "SAME_RELEASE":
        identity_chain = validate_same_release_identity_chain(bundle, datasets, state)
        blockers.extend(identity_chain.reasons)
        pointer_valid = _accepted_pointer_matches_bundle(state, bundle, raw_root)
        if not pointer_valid:
            blockers.append("same_release_replay_pointer_mismatch")
        if not bundle_errors:
            descriptor_verdict = _accepted_descriptor_verdict(
                lifecycle.proof, bundle, raw_root
            )
            blockers.extend(descriptor_verdict["reasons"])
        if readiness.state != DataState.FOUND:
            blockers.append("same_release_family_readiness_not_current")

    if bundle_errors:
        blockers.append("discovered_bundle_invalid")
        resources = None
    else:
        resources = _resource_report(
            bundle,
            raw_root=raw_root,
            head=head,
            disk_usage=disk_usage,
            config=config,
        )
        blockers.extend(resources["blockers"])
    admission_fingerprint = _admission_fingerprint(
        bundle=bundle, state=state, datasets=datasets, approval=approval,
        fact_count=fact_count, lifecycle=lifecycle, readiness=readiness,
        descriptor=descriptor_verdict,
    )
    return {
        "status": "READY_FOR_CONTROLLED_LIVE" if not blockers else "BLOCKED",
        "source_id": SOURCE_ID,
        "handler_version": HANDLER_VERSION,
        "family_dataset_code": FAMILY_DATASET_CODE,
        "member_dataset_codes": [LEGAL_DATASET_CODE, IP_DATASET_CODE],
        "bundle": bundle.as_metadata(),
        "publication_lifecycle": lifecycle.lifecycle.value,
        "publication_proof_valid": lifecycle.lifecycle == PublicationLifecycle.ACCEPTED_VALID,
        "publication_proof_reasons": list(lifecycle.reasons),
        "publication_generation": lifecycle.proof.generation if lifecycle.proof else None,
        "canonical_checksum_verdict": (
            "VALID" if lifecycle.proof else
            "NOT_APPLICABLE" if lifecycle.lifecycle == PublicationLifecycle.NO_PUBLICATION
            else "INVALID"
        ),
        "accepted_descriptor_verdict": descriptor_verdict,
        "publication_proof_fingerprint": (
            canonical_fingerprint(lifecycle.proof.as_evidence()) if lifecycle.proof else None
        ),
        "admission_fingerprint": admission_fingerprint,
        "release_mode": release_mode,
        "family_readiness_state": readiness.state.value,
        "family_readiness_reason": readiness.reason,
        "current_publication_identity": current_identity,
        "discovered_release_identity": bundle.identity,
        "member_identity_verification": (
            {
                "valid": identity_chain.valid and pointer_valid,
                "reasons": list(identity_chain.reasons) + (
                    [] if pointer_valid else ["same_release_replay_pointer_mismatch"]
                ),
                "replay_pointer_matches_bundle": pointer_valid,
                "expected_family_identity": identity_chain.expected_family_identity,
                "persisted_publication_identity": identity_chain.persisted_publication_identity,
                "persisted_publication_members": identity_chain.persisted_publication_members,
                "persisted_family_identity": identity_chain.persisted_family_identity,
                "expected_members": identity_chain.expected_members,
                "persisted_family_members": identity_chain.persisted_family_members,
                "persisted_child_members": identity_chain.persisted_child_members,
                "members": identity_chain.member_verification,
                "source_dates": identity_chain.source_dates,
            }
            if identity_chain else None
        ),
        "new_release_exists": (
            None if lifecycle.lifecycle == PublicationLifecycle.CORRUPT
            else current_identity != bundle.identity
        ),
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
        "bundle_validation_errors": bundle_errors,
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
        # Serialize the acceptance decision with publication/registration writes.
        family = session.scalar(
            select(DataSet)
            .where(DataSet.code == FAMILY_DATASET_CODE)
            .with_for_update()
        )
        datasets = {
            row.code: row for row in session.scalars(
                select(DataSet)
                .where(DataSet.code.in_((LEGAL_DATASET_CODE, IP_DATASET_CODE)))
                .order_by(DataSet.code)
                .with_for_update()
            )
        }
        if family is not None:
            datasets[FAMILY_DATASET_CODE] = family
        state = session.scalar(
            select(WorkerPublicationState)
            .where(WorkerPublicationState.source_id == SOURCE_ID)
            .with_for_update()
        )
        approval = session.scalar(
            select(WorkerHandlerRegistration)
            .where(
                WorkerHandlerRegistration.source_id == SOURCE_ID,
                WorkerHandlerRegistration.handler_version == HANDLER_VERSION,
            )
            .with_for_update()
        )
        locked_report = build_preflight_report(
            session,
            bundle=bundle,
            raw_root=raw_root,
            locked_evidence=(approval, state, datasets),
        )
        if (
            locked_report["status"] != "READY_FOR_CONTROLLED_LIVE"
            or locked_report["admission_fingerprint"] != report["admission_fingerprint"]
        ):
            raise RuntimeError("controlled_live_preflight_stale: " + ", ".join(
                locked_report["blockers"] or ("admission_fingerprint_changed",)
            ))
        validation = state.validation_metadata if state and isinstance(state.validation_metadata, Mapping) else {}
        same_release = locked_report["release_mode"] == "SAME_RELEASE"
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
