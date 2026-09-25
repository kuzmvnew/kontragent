"""Persistent detector, controller, allowlisted playbooks and verification."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contracts.data_readiness import retry_delay
from app.database.postgres import SessionLocal
from app.incidents.classification import Classification, classify_failure, fingerprint
from app.incidents.engineering import (
    EngineeringAdapter,
    branch_name,
    create_repair_package,
    detect_agent_runtime,
)
from app.incidents.notifications import DEFAULT_NOTIFIER, NotificationAdapter
from app.incidents.safe import safe_text, safe_value
from app.models.incident import (
    SourceAutomationPolicy,
    SourceIncident,
    SourceIncidentAction,
)
from app.models.source import DataSet
from app.models.worker import (
    WorkerJob,
    WorkerPublicationState,
    WorkerRawManifest,
    WorkerRun,
)
from app.services.data_readiness_scheduler import (
    SCHEDULED_SOURCE_DATASET_CODES,
    run_due_updates,
)
from app.services.data_readiness_service import effective_status
from app.worker.execution import RetryPolicy, recover_stale_runs, retry_job_now

logger = logging.getLogger("nextcompany.incidents")
TERMINAL_STATUSES = {"RESOLVED", "CANCELLED"}
ACTIONABLE_STATUSES = {"OPEN", "RETRY_SCHEDULED", "WAITING_SOURCE"}
SOURCE_WAIT_CATEGORIES = {
    "SOURCE_UNAVAILABLE",
    "SOURCE_STALE",
    "SOURCE_INVALID_ARTIFACT",
    "SOURCE_SCHEMA_VIOLATION",
}
DATASET_WORKER_SOURCE_IDS = {"fns_tax_debt": "S02"}
WORKER_DATASET_SOURCE_IDS = {value: key for key, value in DATASET_WORKER_SOURCE_IDS.items()}
COOLDOWN_VALUES = {60, 300, 900, 3600, 21600, 86400}
MAX_ATTEMPT_VALUES = set(range(1, 11))
WORKER_SERVICE = "nextcompany-source-worker.service"
RAW_ROOT = Path("/home/mikhail/nextcompany-operational/raw")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def worker_source_id(dataset_code: str) -> str:
    return DATASET_WORKER_SOURCE_IDS.get(dataset_code, dataset_code)


def dataset_code(source_id: str) -> str:
    return WORKER_DATASET_SOURCE_IDS.get(source_id, source_id)


def _incident_code(now: datetime) -> str:
    return f"INC-{now:%Y%m%d}-{uuid4().hex[:8].upper()}"


def record_action(
    session: Session,
    incident: SourceIncident,
    action_type: str,
    *,
    result: str,
    message: str | None = None,
    metadata: dict | None = None,
    now: datetime | None = None,
) -> SourceIncidentAction:
    now = now or utc_now()
    action = SourceIncidentAction(
        incident_id=incident.id,
        action_type=action_type,
        started_at=now,
        finished_at=now,
        result=result,
        safe_message=safe_text(message) if message else None,
        metadata_safe=safe_value(metadata or {}),
    )
    session.add(action)
    incident.last_action = action_type
    incident.last_action_result = result
    incident.updated_at = now
    return action


def ensure_policy(
    session: Session,
    *,
    source_id: str,
    dataset: str,
) -> SourceAutomationPolicy:
    policy = session.get(SourceAutomationPolicy, source_id)
    if policy is None:
        runtime = detect_agent_runtime()
        policy = SourceAutomationPolicy(
            source_id=source_id,
            dataset_code=dataset,
            auto_heal_enabled=True,
            auto_code_repair_enabled=runtime.available,
            remediation_level=3 if runtime.available else 2,
            max_attempts=3,
            cooldown_seconds=3600,
        )
        session.add(policy)
        session.flush()
    return policy


def ensure_default_policies(session: Session) -> None:
    for code in sorted(SCHEDULED_SOURCE_DATASET_CODES):
        ensure_policy(session, source_id=worker_source_id(code), dataset=code)


def _latest_worker_failure(session: Session, source_id: str) -> tuple[WorkerRun | None, WorkerJob | None]:
    row = session.execute(
        select(WorkerRun, WorkerJob)
        .join(WorkerJob, WorkerRun.job_id == WorkerJob.id)
        .where(WorkerJob.source_id == source_id)
        .order_by(WorkerRun.started_at.desc(), WorkerRun.attempt_no.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else (None, None)


def upsert_incident(
    session: Session,
    *,
    source_id: str,
    dataset: str,
    classification: Classification,
    error_code: str | None,
    message: str | None,
    run_id: UUID | None = None,
    job_id: UUID | None = None,
    now: datetime | None = None,
    notifier: NotificationAdapter = DEFAULT_NOTIFIER,
) -> tuple[SourceIncident, bool]:
    now = now or utc_now()
    safe_message = safe_text(message or classification.category)
    failure_fingerprint = fingerprint(
        source_id=source_id,
        category=classification.category,
        error_code=error_code,
        message=safe_message,
    )
    dedup_key = f"{source_id}:{failure_fingerprint}"
    incident = session.scalar(
        select(SourceIncident).where(
            SourceIncident.dedup_key == dedup_key,
            SourceIncident.status.not_in(TERMINAL_STATUSES),
        )
    )
    if incident is not None:
        incident.updated_at = now
        incident.run_id = run_id or incident.run_id
        incident.job_id = job_id or incident.job_id
        incident.safe_error_message = safe_message
        return incident, False

    policy = ensure_policy(session, source_id=source_id, dataset=dataset)
    incident = SourceIncident(
        incident_code=_incident_code(now),
        source_id=source_id,
        dataset_code=dataset,
        run_id=run_id,
        job_id=job_id,
        severity=classification.severity,
        category=classification.category,
        owner_domain=classification.owner_domain,
        status="WAITING_SOURCE" if classification.owner_domain == "SOURCE_OWNED" and classification.category == "SOURCE_SCHEMA_VIOLATION" else "OPEN",
        detected_at=now,
        updated_at=now,
        error_code=(error_code or "")[:120] or None,
        safe_error_message=safe_message,
        failure_fingerprint=failure_fingerprint,
        dedup_key=dedup_key,
        auto_heal_enabled=policy.auto_heal_enabled and not policy.paused and classification.remediation_level > 0,
        auto_code_repair_enabled=policy.auto_code_repair_enabled and not policy.paused,
        remediation_level=min(classification.remediation_level, policy.remediation_level),
        max_attempts=policy.max_attempts,
        next_attempt_at=now if classification.remediation_level > 0 else None,
        resolution_evidence={"baseline": {}},
    )
    session.add(incident)
    session.flush()
    record_action(session, incident, "DETECTED", result="SUCCESS", message=safe_message, now=now)
    record_action(
        session,
        incident,
        "CLASSIFIED",
        result="SUCCESS",
        metadata={"category": classification.category, "owner_domain": classification.owner_domain},
        now=now,
    )
    notifier.notify("incident_detected", {"incident": incident.incident_code, "source": source_id, "category": classification.category})
    return incident, True


def detect_dataset_incidents(
    session: Session,
    *,
    now: datetime | None = None,
    only_source: str | None = None,
    notifier: NotificationAdapter = DEFAULT_NOTIFIER,
) -> list[SourceIncident]:
    now = now or utc_now()
    ensure_default_policies(session)
    query = select(DataSet).where(DataSet.code.in_(SCHEDULED_SOURCE_DATASET_CODES))
    if only_source:
        query = query.where(DataSet.code == dataset_code(only_source))
    incidents: list[SourceIncident] = []
    for item in session.scalars(query):
        source_id = worker_source_id(item.code)
        status = effective_status(item, now=now)
        run, job = _latest_worker_failure(session, source_id)
        error_code = None
        message = item.last_error
        if run is not None and run.status in {"failed", "timed_out", "interrupted"}:
            errors = list(run.errors or [])
            if errors:
                error_code = str(errors[-1].get("kind") or "") or None
                message = str(errors[-1].get("message") or "") or message
        if status not in {"error", "unavailable", "stale", "source_blocked", "access_pending"} and not message:
            continue
        classification = classify_failure(
            source_id=source_id,
            error_code=error_code,
            message=message,
            operational_status=status,
        )
        incident, _ = upsert_incident(
            session,
            source_id=source_id,
            dataset=item.code,
            classification=classification,
            error_code=error_code,
            message=message or status,
            run_id=run.id if run else None,
            job_id=job.id if job else None,
            now=now,
            notifier=notifier,
        )
        incidents.append(incident)
    return incidents


def systemd_service_active(service: str = WORKER_SERVICE) -> bool:
    if service != WORKER_SERVICE:
        raise ValueError("service is not allowlisted")
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", WORKER_SERVICE],
        check=False,
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def restart_worker_service(service: str = WORKER_SERVICE) -> bool:
    if service != WORKER_SERVICE:
        raise ValueError("service is not allowlisted")
    subprocess.run(
        ["systemctl", "--user", "restart", WORKER_SERVICE],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return systemd_service_active(WORKER_SERVICE)


def detect_worker_incident(
    session: Session,
    *,
    now: datetime | None = None,
    active: bool | None = None,
    notifier: NotificationAdapter = DEFAULT_NOTIFIER,
) -> SourceIncident | None:
    now = now or utc_now()
    if active is None:
        active = systemd_service_active()
    if active:
        return None
    classification = Classification("WORKER_NOT_RUNNING", "OUR_INFRASTRUCTURE", "CRITICAL", 2)
    incident, _ = upsert_incident(
        session,
        source_id="__worker__",
        dataset="__worker__",
        classification=classification,
        error_code="service_inactive",
        message="nextcompany-source-worker.service is not active",
        now=now,
        notifier=notifier,
    )
    return incident


def detect_disk_pressure(
    session: Session,
    *,
    now: datetime | None = None,
    raw_root: Path = RAW_ROOT,
    minimum_free_bytes: int = 5 * 1024**3,
    notifier: NotificationAdapter = DEFAULT_NOTIFIER,
) -> SourceIncident | None:
    now = now or utc_now()
    try:
        usage = shutil.disk_usage(raw_root)
    except OSError:
        return None
    if usage.free >= minimum_free_bytes and usage.free / max(usage.total, 1) >= 0.05:
        return None
    classification = Classification("DISK_PRESSURE", "OUR_INFRASTRUCTURE", "CRITICAL", 0)
    incident, _ = upsert_incident(
        session,
        source_id="__storage__",
        dataset="__storage__",
        classification=classification,
        error_code="disk_pressure",
        message="RAW filesystem has crossed the fixed free-space safety threshold",
        now=now,
        notifier=notifier,
    )
    return incident


def _baseline(session: Session, incident: SourceIncident) -> dict:
    dataset = session.scalar(select(DataSet).where(DataSet.code == incident.dataset_code))
    publication = session.get(WorkerPublicationState, incident.source_id)
    return {
        "last_success_at": dataset.last_success_at.isoformat() if dataset and dataset.last_success_at else None,
        "publication_pointer": publication.active_pointer if publication else None,
        "publication_generation": publication.generation if publication else None,
        "facts": dataset.record_count if dataset else None,
    }


def verify_recovery(session: Session, incident: SourceIncident, *, now: datetime | None = None) -> tuple[bool, dict]:
    now = now or utc_now()
    if incident.source_id == "__worker__":
        active = systemd_service_active()
        return active, {"service_active": active}
    if incident.dataset_code.startswith("__"):
        return False, {"status": "manual verification required"}
    dataset = session.scalar(select(DataSet).where(DataSet.code == incident.dataset_code))
    publication = session.get(WorkerPublicationState, incident.source_id)
    baseline = dict((incident.resolution_evidence or {}).get("baseline") or {})
    status = effective_status(dataset, now=now) if dataset else "missing"
    last_success = dataset.last_success_at.isoformat() if dataset and dataset.last_success_at else None
    pointer = publication.active_pointer if publication else None
    facts = dataset.record_count if dataset else None
    preserved = (
        baseline.get("last_success_at") is None
        or (last_success is not None and last_success >= baseline["last_success_at"])
    )
    pointer_valid = baseline.get("publication_pointer") is None or pointer == baseline.get("publication_pointer") or bool(pointer)
    facts_intact = baseline.get("facts") is None or (facts is not None and facts >= 0)
    scheduler_ready = bool(dataset and dataset.next_expected_update_at)
    cleared = bool(dataset and not dataset.last_error and status not in {"error", "unavailable", "stale"})
    evidence = {
        "dataset_status": status,
        "last_success_at": last_success,
        "last_success_preserved": preserved,
        "publication_pointer_valid": pointer_valid,
        "facts_intact": facts_intact,
        "scheduler_has_next_run": scheduler_ready,
        "error_cleared": cleared,
    }
    return all((preserved, pointer_valid, facts_intact, scheduler_ready, cleared)), evidence


def _execute_playbook(session: Session, incident: SourceIncident, *, now: datetime) -> tuple[str, str]:
    if incident.category == "WORKER_NOT_RUNNING":
        return ("WORKER_RESTART", "SUCCESS" if restart_worker_service() else "FAILED")
    if incident.category == "LEASE_STUCK":
        recovered = recover_stale_runs(
            session,
            stale_after=timedelta(minutes=5),
            retry_policy=RetryPolicy(),
            now=now,
        )
        return "LEASE_RECOVERY", "SUCCESS" if recovered else "NOOP"
    if incident.category in {
        "TEMPORARY_NETWORK", "HTTP_5XX", "TIMEOUT", "RATE_LIMIT", "DNS_FAILURE",
        "DATABASE_LOCK",
    } and incident.job_id:
        job = session.get(WorkerJob, incident.job_id)
        if job is not None and job.status == "retry_scheduled" and job.next_attempt_at and job.next_attempt_at > now:
            return "RETRY_SCHEDULED", "PENDING"
        try:
            retry_job_now(session, job_id=incident.job_id, now=now)
        except (LookupError, ValueError):
            result = run_due_updates(due_codes=[incident.dataset_code])
            return "SOURCE_REDISCOVERY", "SUCCESS" if result.get(incident.dataset_code) == "success" else "FAILED"
        return "RETRY_EXECUTED", "SUCCESS"
    if incident.category in {
        "SOURCE_STALE", "SOURCE_UNAVAILABLE", "SOURCE_INVALID_ARTIFACT",
        "SOURCE_SCHEMA_VIOLATION", "CHECKSUM_MISMATCH",
    }:
        result = run_due_updates(due_codes=[incident.dataset_code])
        return "SOURCE_REDISCOVERY", "SUCCESS" if result.get(incident.dataset_code) == "success" else "FAILED"
    if incident.category == "DATABASE_UNAVAILABLE":
        # Reaching this playbook through a live transaction is itself the
        # canonical health check.  DB/container restart remains disabled until
        # a separately configured fixed service identity exists.
        session.scalar(select(1))
        return "DATABASE_HEALTH_CHECK", "SUCCESS"
    return "OBSERVED", "NOOP"


def process_incident(
    session: Session,
    incident: SourceIncident,
    *,
    now: datetime | None = None,
    force: bool = False,
    notifier: NotificationAdapter = DEFAULT_NOTIFIER,
) -> SourceIncident:
    now = now or utc_now()
    policy = ensure_policy(session, source_id=incident.source_id, dataset=incident.dataset_code)
    if policy.paused or not policy.auto_heal_enabled or not incident.auto_heal_enabled:
        return incident
    if not force and incident.next_attempt_at and incident.next_attempt_at > now:
        return incident
    if incident.status not in ACTIONABLE_STATUSES:
        return incident
    source_owned_wait = (
        incident.owner_domain == "SOURCE_OWNED"
        and incident.category in SOURCE_WAIT_CATEGORIES
    )
    evidence = dict(incident.resolution_evidence or {})
    if evidence.get("engineering_requested_at"):
        request_engineering_repair(session, incident, now=now)
        evidence = dict(incident.resolution_evidence or {})
        evidence.pop("engineering_requested_at", None)
        incident.resolution_evidence = safe_value(evidence)
        return incident
    if not source_owned_wait and incident.attempt_count >= incident.max_attempts:
        incident.status = "AUTO_REPAIR_EXHAUSTED"
        record_action(session, incident, "EXHAUSTED", result="FAILED", message="automatic remediation attempt limit reached", now=now)
        notifier.notify("repair_exhausted", {"incident": incident.incident_code, "source": incident.source_id})
        return incident

    evidence = dict(incident.resolution_evidence or {})
    if not evidence.get("baseline"):
        evidence["baseline"] = _baseline(session, incident)
        incident.resolution_evidence = safe_value(evidence)
    incident.status = "AUTO_HEAL_RUNNING"
    if not source_owned_wait:
        incident.attempt_count += 1
    action_type = "OBSERVED"
    result = "NOOP"
    try:
        action_type, result = _execute_playbook(session, incident, now=now)
    except Exception as error:
        result = "FAILED"
        record_action(session, incident, action_type, result=result, message=str(error), now=now)
    else:
        action_result = (
            "SOURCE_STILL_INVALID"
            if source_owned_wait and result != "SUCCESS"
            else result
        )
        record_action(session, incident, action_type, result=action_result, now=now)

    recovered, verification = verify_recovery(session, incident, now=now)
    evidence = dict(incident.resolution_evidence or {})
    evidence["latest_verification"] = safe_value(verification)
    incident.resolution_evidence = evidence
    recheck_result = (
        "SUCCESS"
        if recovered
        else "SOURCE_STILL_INVALID"
        if source_owned_wait
        else "PENDING"
    )
    record_action(
        session,
        incident,
        "SOURCE_RECHECK",
        result=recheck_result,
        metadata=verification,
        now=now,
    )
    if recovered:
        incident.status = "RESOLVED"
        incident.resolved_at = now
        incident.next_attempt_at = None
        incident.cooldown_until = None
        incident.resolution = "Recovery verification passed"
        record_action(session, incident, "RECOVERED", result="SUCCESS", metadata=verification, now=now)
        notifier.notify("auto_repair_recovered", {"incident": incident.incident_code, "source": incident.source_id})
    else:
        if not source_owned_wait and incident.attempt_count >= incident.max_attempts:
            incident.status = "AUTO_REPAIR_EXHAUSTED"
            incident.next_attempt_at = None
            incident.cooldown_until = None
            record_action(session, incident, "EXHAUSTED", result="FAILED", message="automatic remediation attempt limit reached", now=now)
            notifier.notify("repair_exhausted", {"incident": incident.incident_code, "source": incident.source_id})
            return incident
        delay = (
            timedelta(seconds=policy.cooldown_seconds)
            if source_owned_wait
            else retry_delay(
                incident.attempt_count,
                base_seconds=policy.cooldown_seconds,
                maximum_seconds=86400,
            )
        )
        scheduled_at = now + delay
        if action_type == "RETRY_SCHEDULED" and incident.job_id:
            job = session.get(WorkerJob, incident.job_id)
            if job is not None and job.next_attempt_at:
                scheduled_at = job.next_attempt_at
        incident.cooldown_until = scheduled_at
        incident.next_attempt_at = scheduled_at
        incident.status = "WAITING_SOURCE" if source_owned_wait else "RETRY_SCHEDULED"
        record_action(
            session,
            incident,
            "SOURCE_RECHECK_SCHEDULED" if source_owned_wait else "RETRY_SCHEDULED",
            result="PENDING",
            metadata={"next_attempt_at": incident.next_attempt_at.isoformat()},
            now=now,
        )
    return incident


def normalize_source_owned_waits(
    session: Session,
    *,
    now: datetime | None = None,
    only_source: str | None = None,
) -> list[SourceIncident]:
    """Repair legacy exhaustion state for persistent official-source failures."""

    now = now or utc_now()
    query = select(SourceIncident).where(
        SourceIncident.owner_domain == "SOURCE_OWNED",
        SourceIncident.category.in_(SOURCE_WAIT_CATEGORIES),
        SourceIncident.status == "AUTO_REPAIR_EXHAUSTED",
    )
    if only_source:
        query = query.where(SourceIncident.source_id == only_source)
    rows = list(session.scalars(query.with_for_update(skip_locked=True)))
    for incident in rows:
        incident.status = "WAITING_SOURCE"
        incident.next_attempt_at = now
        incident.cooldown_until = None
        incident.resolution = None
        record_action(
            session,
            incident,
            "SOURCE_RECHECK_SCHEDULED",
            result="PENDING",
            message="official source checks continue without circuit-breaker exhaustion",
            metadata={"next_attempt_at": now.isoformat()},
            now=now,
        )
    return rows


def process_due_incidents(
    session: Session,
    *,
    now: datetime | None = None,
    only_source: str | None = None,
    notifier: NotificationAdapter = DEFAULT_NOTIFIER,
) -> list[SourceIncident]:
    now = now or utc_now()
    query = select(SourceIncident).where(
        SourceIncident.status.in_(ACTIONABLE_STATUSES),
        or_(SourceIncident.next_attempt_at.is_(None), SourceIncident.next_attempt_at <= now),
    ).order_by(SourceIncident.detected_at)
    if only_source:
        query = query.where(SourceIncident.source_id == only_source)
    incidents = list(session.scalars(query.with_for_update(skip_locked=True)))
    for incident in incidents:
        process_incident(session, incident, now=now, notifier=notifier)
    return incidents


def verify_active_incidents(
    session: Session,
    *,
    now: datetime | None = None,
    only_source: str | None = None,
    notifier: NotificationAdapter = DEFAULT_NOTIFIER,
) -> list[SourceIncident]:
    """Resolve externally recovered incidents without waiting for cooldown."""

    now = now or utc_now()
    query = select(SourceIncident).where(
        SourceIncident.status.not_in(TERMINAL_STATUSES)
    ).order_by(SourceIncident.detected_at)
    if only_source:
        query = query.where(SourceIncident.source_id == only_source)
    resolved: list[SourceIncident] = []
    for incident in session.scalars(query.with_for_update(skip_locked=True)):
        recovered, evidence = verify_recovery(session, incident, now=now)
        if not recovered:
            continue
        state = dict(incident.resolution_evidence or {})
        state["latest_verification"] = safe_value(evidence)
        incident.resolution_evidence = state
        incident.status = "RESOLVED"
        incident.resolved_at = now
        incident.next_attempt_at = None
        incident.cooldown_until = None
        incident.resolution = "Recovery verification passed"
        record_action(session, incident, "SOURCE_RECHECK", result="SUCCESS", metadata=evidence, now=now)
        record_action(session, incident, "RECOVERED", result="SUCCESS", metadata=evidence, now=now)
        notifier.notify("auto_repair_recovered", {"incident": incident.incident_code, "source": incident.source_id})
        resolved.append(incident)
    return resolved


def run_controller_once(*, now: datetime | None = None, only_source: str | None = None) -> dict[str, int]:
    now = now or utc_now()
    with SessionLocal() as session:
        try:
            detected = detect_dataset_incidents(session, now=now, only_source=only_source)
            if only_source in {None, "__worker__"}:
                worker = detect_worker_incident(session, now=now)
                if worker:
                    detected.append(worker)
            if only_source is None:
                disk = detect_disk_pressure(session, now=now)
                if disk:
                    detected.append(disk)
            session.commit()
        except IntegrityError:
            session.rollback()
            detected = detect_dataset_incidents(session, now=now, only_source=only_source)
            session.commit()
        normalized = normalize_source_owned_waits(session, now=now, only_source=only_source)
        session.commit()
        resolved = verify_active_incidents(session, now=now, only_source=only_source)
        session.commit()
        processed = process_due_incidents(session, now=now, only_source=only_source)
        session.commit()
        return {
            "detected": len(detected),
            "normalized": len(normalized),
            "recovered": len(resolved),
            "processed": len(processed),
        }


def controller_loop(*, poll_seconds: int = 60) -> None:
    if poll_seconds < 10:
        raise ValueError("poll_seconds must be at least 10")
    while True:
        try:
            result = run_controller_once()
            logger.info("incident_controller_poll", extra=result)
        except Exception:
            # DB outages cannot be persisted in the unavailable DB; structured
            # logs remain the notification boundary until the next successful poll.
            logger.exception("incident_controller_poll_failed")
        time.sleep(poll_seconds)


def request_engineering_repair(
    session: Session,
    incident: SourceIncident,
    *,
    incident_root: Path | None = None,
    repository: Path | None = None,
    adapter: EngineeringAdapter | None = None,
    now: datetime | None = None,
) -> SourceIncident:
    now = now or utc_now()
    if incident.owner_domain != "OUR_CODE":
        raise ValueError("engineering repair is allowed only for OUR_CODE incidents")
    manifest = None
    if incident.run_id:
        manifest = session.scalar(
            select(WorkerRawManifest)
            .where(WorkerRawManifest.run_id == incident.run_id)
            .order_by(WorkerRawManifest.created_at.desc())
            .limit(1)
        )
    artifact_identity = None
    if manifest is not None:
        raw_name = Path(str(manifest.artifact_reference).split("?", 1)[0]).name
        artifact_identity = raw_name or "immutable WorkerRawManifest artifact"
    package = create_repair_package(
        incident=incident,
        root=incident_root or Path(os.getenv("INCIDENT_ROOT", str(Path("/home/mikhail/nextcompany-operational/incidents")))),
        repository=repository or Path(os.getenv("INCIDENT_REPOSITORY", "/home/mikhail/nextcompany-runtime/current")),
        artifact_identity=artifact_identity,
        artifact_checksum=manifest.checksum if manifest is not None else None,
        validation_summary=(manifest.manifest if manifest is not None else None),
    )
    incident.repair_branch = branch_name(incident.incident_code, incident.source_id)
    record_action(session, incident, "REPAIR_PACKAGE_CREATED", result="SUCCESS", metadata={"package": package.name}, now=now)
    adapter = adapter or EngineeringAdapter()
    if not adapter.runtime.available:
        incident.status = "AGENT_UNAVAILABLE"
        incident.repair_ci_status = "NOT_RUN"
        record_action(session, incident, "AGENT_STARTED", result="AGENT_UNAVAILABLE", now=now)
        return incident
    try:
        adapter.start(incident=incident, package=package)
    except Exception as error:
        incident.status = "AWAITING_ENGINEERING_REVIEW"
        record_action(session, incident, "AGENT_STARTED", result="FAILED", message=str(error), now=now)
    return incident


def update_policy(
    session: Session,
    *,
    source_id: str,
    auto_heal_enabled: bool,
    auto_code_repair_enabled: bool,
    max_attempts: int,
    cooldown_seconds: int,
) -> SourceAutomationPolicy:
    dataset = dataset_code(source_id)
    if dataset not in SCHEDULED_SOURCE_DATASET_CODES:
        raise ValueError("unsupported source policy")
    if max_attempts not in MAX_ATTEMPT_VALUES or cooldown_seconds not in COOLDOWN_VALUES:
        raise ValueError("policy value is not allowlisted")
    runtime = detect_agent_runtime()
    if auto_code_repair_enabled and not runtime.available:
        raise ValueError("AGENT_UNAVAILABLE")
    policy = ensure_policy(session, source_id=source_id, dataset=dataset)
    policy.auto_heal_enabled = auto_heal_enabled
    policy.auto_code_repair_enabled = auto_code_repair_enabled
    policy.remediation_level = 3 if auto_code_repair_enabled else (2 if auto_heal_enabled else 0)
    policy.max_attempts = max_attempts
    policy.cooldown_seconds = cooldown_seconds
    policy.paused = not auto_heal_enabled
    return policy
