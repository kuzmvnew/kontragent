"""Read models and canonical controls for the private source console."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from admin_app.catalog import DATA_ACCESS_URLS, OFFICIAL_PAGE_URLS, access_kind
from app.contracts.data_readiness import AutoUpdateStatus
from app.database.postgres import SessionLocal
from app.incidents.controller import (
    record_action,
    update_policy,
)
from app.incidents.engineering import detect_agent_runtime
from app.incidents.safe import safe_value
from app.models.admin import AdminActionAudit, SourceChangeSummary
from app.models.company import Company
from app.models.incident import (
    SourceAutomationPolicy,
    SourceIncident,
    SourceIncidentAction,
)
from app.models.source import DataSet, DataSource
from app.models.worker import (
    WorkerHandlerRegistration,
    WorkerJob,
    WorkerLease,
    WorkerPublicationState,
    WorkerRawManifest,
    WorkerRun,
)
from app.services.company_enrichment_service import canonical_enrichment_metrics
from app.services.data_readiness_scheduler import (
    HANDLERS,
    SCHEDULED_SOURCE_DATASET_CODES,
    configure_source_schedules,
    run_due_updates,
)
from app.services.data_readiness_service import effective_status, safe_error_message
from app.services.publication_service import (
    publication_dashboard,
)
from app.services.publication_service import (
    publication_history as public_sync_history,
)
from app.worker.execution import retry_job_now

DATASET_WORKER_SOURCE_IDS = {"fns_tax_debt": "S02"}
DATASET_CODES = tuple(sorted(SCHEDULED_SOURCE_DATASET_CODES))
SOURCE_IDS = tuple(
    DATASET_WORKER_SOURCE_IDS.get(code, code) for code in DATASET_CODES
)
TERMINAL_INCIDENT_STATUSES = {"RESOLVED", "CANCELLED"}
SECRET_KEY = re.compile(r"(?i)(password|secret|token|cookie|authorization|api[_-]?key|dsn|database_url)")
ACTION_LABELS = {
    "check-now": "Проверить сейчас",
    "activate": "Активировать расписание",
    "pause": "Приостановить расписание",
    "resume": "Возобновить расписание",
    "retry": "Повторить неудачную задачу",
}
INCIDENT_ACTION_LABELS = {
    "check-source-now": "Проверить источник сейчас",
    "run-auto-heal": "Запустить восстановление сейчас",
    "pause-auto-repair": "Приостановить автоматику",
    "resume-auto-repair": "Возобновить автоматику",
    "mark-source-owned": "Отметить как проблему официального источника",
    "request-engineering": "Запустить инженерное исправление",
    "prepare-repair-package": "Подготовить пакет для исправления",
    "cancel-pending": "Отменить ожидающее действие",
}
SOURCE_WAIT_CATEGORIES = {
    "SOURCE_UNAVAILABLE",
    "SOURCE_STALE",
    "SOURCE_INVALID_ARTIFACT",
    "SOURCE_SCHEMA_VIOLATION",
}
CANCELLABLE_INCIDENT_STATUSES = {
    "OPEN",
    "RETRY_SCHEDULED",
    "WAITING_SOURCE",
    "AGENT_UNAVAILABLE",
    "AWAITING_ENGINEERING_REVIEW",
    "REVIEW_REQUIRED",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dataset_code_for(source_id: str) -> str:
    for dataset_code, worker_source_id in DATASET_WORKER_SOURCE_IDS.items():
        if worker_source_id == source_id:
            return dataset_code
    return source_id


def worker_source_id_for(dataset_code: str) -> str:
    return DATASET_WORKER_SOURCE_IDS.get(dataset_code, dataset_code)


def safe_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    safe_query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not SECRET_KEY.search(key) and not key.casefold().startswith("x-amz-")
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(safe_query), "")
    )


def safe_identity(value: str | None) -> str | None:
    if not value:
        return None
    clean = str(value).split("?", 1)[0].rstrip("/")
    name = Path(urlsplit(clean).path).name
    scheme = urlsplit(clean).scheme or "local"
    return f"{scheme}://…/{name}" if name else f"{scheme}://…"


def redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(key)[:100]: ("[REDACTED]" if SECRET_KEY.search(str(key)) else redact(item, depth=depth + 1))
            for key, item in list(value.items())[:80]
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, depth=depth + 1) for item in list(value)[:80]]
    if isinstance(value, str):
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.query:
            value = parsed._replace(query="", fragment="").geturl()
        return safe_error_message(value, limit=1000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def redact_manifest(value: Any) -> Any:
    """Expose manifest metadata while omitting unknown payload-like fields."""

    if not isinstance(value, dict):
        return "[OMITTED — manifest is not metadata]"
    safe_names = re.compile(
        r"(?i)(checksum|sha|size|length|count|record|source|data|date|member|file|name|format|schema|identity|etag|modified|url|version)"
    )
    return {
        str(key)[:100]: (
            redact(item) if safe_names.search(str(key)) else "[OMITTED]"
        )
        for key, item in list(value.items())[:80]
    }


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _source_inventory_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count update processes, runnable handlers, and grouped source families."""

    return {
        "data_processes": len(rows),
        "connected": sum(bool(row["connected"]) for row in rows),
        "source_families": len(
            {
                (str(row["source_group"]), str(row["fact_family"]))
                for row in rows
            }
        ),
    }


def _master_counts(session) -> dict[str, Any]:
    total, legal, individual = session.execute(
        select(
            func.count(Company.id),
            func.count(Company.id).filter(
                or_(Company.entity_type == "legal", func.length(Company.inn) == 10)
            ),
            func.count(Company.id).filter(
                or_(
                    Company.entity_type == "individual_entrepreneur",
                    func.length(Company.inn) == 12,
                )
            ),
        )
    ).one()
    today = utc_now().date()
    authority = session.execute(
        select(
            func.count(Company.id).filter(Company.official_registry_verified.is_(True)),
            func.count(Company.id).filter(
                Company.master_source == "firmoteka",
                Company.official_registry_verified.is_(False),
            ),
            func.count(Company.id).filter(Company.official_registry_verified.is_(False)),
            func.count(Company.id).filter(func.date(Company.created_at) == today),
            func.count(Company.id).filter(func.date(Company.updated_at) == today),
        )
    ).one()
    return {
        "total": int(total or 0),
        "legal": int(legal or 0),
        "ip": int(individual or 0),
        "official_verified": int(authority[0] or 0),
        "firmoteka_provisional": int(authority[1] or 0),
        "pending_official_verification": int(authority[2] or 0),
        "new_today": int(authority[3] or 0),
        "changed_today": int(authority[4] or 0),
    }


def _latest_by_source(rows, source_getter):
    result = {}
    for row in rows:
        source_id = source_getter(row)
        if source_id not in result:
            result[source_id] = row
    return result


def _connected_sources(session) -> set[str]:
    registered = set(
        session.scalars(
            select(WorkerHandlerRegistration.source_id).where(
                WorkerHandlerRegistration.approved.is_(True),
                WorkerHandlerRegistration.enabled.is_(True),
                WorkerHandlerRegistration.live_mode.is_(False),
                WorkerHandlerRegistration.source_id.in_(SOURCE_IDS),
            )
        )
    )
    return {
        dataset_code
        for dataset_code in DATASET_CODES
        if dataset_code in HANDLERS
        and worker_source_id_for(dataset_code) in registered
    }


def _incident_row(incident: SourceIncident) -> dict[str, Any]:
    return {
        "id": str(incident.id),
        "incident_code": incident.incident_code,
        "source_id": incident.source_id,
        "dataset_code": incident.dataset_code,
        "run_id": str(incident.run_id) if incident.run_id else None,
        "job_id": str(incident.job_id) if incident.job_id else None,
        "severity": incident.severity,
        "category": incident.category,
        "owner_domain": incident.owner_domain,
        "status": incident.status,
        "detected_at": incident.detected_at,
        "updated_at": incident.updated_at,
        "resolved_at": incident.resolved_at,
        "error_code": incident.error_code,
        "safe_error_message": safe_error_message(incident.safe_error_message) if incident.safe_error_message else None,
        "failure_fingerprint": incident.failure_fingerprint,
        "auto_heal_enabled": incident.auto_heal_enabled,
        "auto_code_repair_enabled": incident.auto_code_repair_enabled,
        "remediation_level": incident.remediation_level,
        "attempt_count": incident.attempt_count,
        "max_attempts": incident.max_attempts,
        "next_attempt_at": incident.next_attempt_at,
        "cooldown_until": incident.cooldown_until,
        "last_action": incident.last_action,
        "last_action_result": incident.last_action_result,
        "repair_branch": incident.repair_branch,
        "repair_pr_number": incident.repair_pr_number,
        "repair_commit_sha": incident.repair_commit_sha,
        "repair_ci_status": incident.repair_ci_status,
        "resolution": incident.resolution,
        "resolution_evidence": safe_value(incident.resolution_evidence),
    }


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def available_incident_actions(
    incident: SourceIncident | dict[str, Any],
    automation_policy: SourceAutomationPolicy | None,
    agent_runtime: Any,
) -> tuple[str, ...]:
    """Return the only owner actions the server will accept for this incident."""

    status = str(_value(incident, "status", ""))
    owner = str(_value(incident, "owner_domain", "UNKNOWN"))
    category = str(_value(incident, "category", "UNKNOWN"))
    if status in TERMINAL_INCIDENT_STATUSES:
        return ()

    paused = bool(automation_policy and automation_policy.paused)
    enabled = bool(automation_policy and automation_policy.auto_heal_enabled and not paused)
    actions: list[str] = []

    if owner == "SOURCE_OWNED" and category in SOURCE_WAIT_CATEGORIES:
        if enabled:
            actions.append("check-source-now")
        actions.append("resume-auto-repair" if paused else "pause-auto-repair")
        return tuple(actions)

    if owner == "OUR_CODE":
        actions.append(
            "request-engineering" if bool(getattr(agent_runtime, "available", False))
            else "prepare-repair-package"
        )
    elif owner == "OUR_INFRASTRUCTURE" and status != "AUTO_HEAL_RUNNING" and enabled:
        actions.append("run-auto-heal")
    elif owner == "UNKNOWN":
        if enabled:
            actions.append("run-auto-heal")
        actions.append("mark-source-owned")

    if automation_policy is not None:
        actions.append("resume-auto-repair" if paused else "pause-auto-repair")
    if status in CANCELLABLE_INCIDENT_STATUSES and owner not in {"SOURCE_OWNED", "ACCESS_REQUIRED"}:
        actions.append("cancel-pending")
    return tuple(actions)


def _auto_repair_status(
    policy: SourceAutomationPolicy | None,
    incident: SourceIncident | None,
) -> str:
    if incident is not None:
        if (
            incident.owner_domain == "SOURCE_OWNED"
            and incident.category in SOURCE_WAIT_CATEGORIES
            and incident.status == "AUTO_REPAIR_EXHAUSTED"
        ):
            return "WAITING SOURCE"
        return {
            "AUTO_HEAL_RUNNING": "RUNNING",
            "WAITING_SOURCE": "WAITING SOURCE",
            "AWAITING_ENGINEERING_REVIEW": "REVIEW REQUIRED",
            "REVIEW_REQUIRED": "REVIEW REQUIRED",
            "AGENT_UNAVAILABLE": "AGENT UNAVAILABLE",
            "AUTO_REPAIR_EXHAUSTED": "EXHAUSTED",
        }.get(incident.status, "ON")
    if policy is None or policy.paused or not policy.auto_heal_enabled:
        return "OFF"
    return "ON"


def _missing_reason(row: dict[str, Any], field: str) -> str:
    stage = row.get("stage")
    if stage == "ACCESS REQUIRED":
        return "Нет данных: для источника ещё не настроен доступ."
    if stage in {"FIRST RUN", "CHECK PENDING"}:
        return "Нет данных: первый рабочий цикл ещё не завершён."
    if stage in {"ERROR", "SOURCE BLOCKED"} and not row.get("last_successful_run"):
        return "Нет данных: последний запуск закончился ошибкой до первой успешной публикации."
    if not row.get("last_successful_run"):
        return "Нет данных: источник ещё не завершал успешную обработку."
    if field == "retry_at":
        return "Нет данных: повтор не запланирован."
    if field == "next_scheduled_check":
        return "Нет данных: следующая автоматическая проверка не запланирована."
    if field == "last_check":
        return "Нет данных: источник ещё не проверялся."
    if field == "last_publication":
        return "Нет данных: источник ещё не публиковал факты."
    if field == "official_actual_until":
        return "Нет данных: официальный источник не сообщает срок актуальности."
    if field == "source_data_date":
        return "Нет данных: официальный источник не сообщает дату сведений."
    if field in {"new_facts", "changed_facts", "removed_facts", "replayed_facts", "quarantined_records"}:
        return "Нет данных: этот WorkerRun не сохранял метрику изменения публикации."
    if field in {"matched_companies", "master_coverage"}:
        return "Нет данных: обработчик этого источника пока не публикует метрику сопоставления с Master."
    if field == "publication_generation":
        return "Нет данных: для источника ещё нет атомарной версии публикации."
    return "Нет данных: обработчик источника пока не публикует эту метрику."


def _incident_counts(session, *, now: datetime) -> dict[str, int]:
    rows = list(session.scalars(select(SourceIncident)))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "open": sum(row.status not in TERMINAL_INCIDENT_STATUSES for row in rows),
        "running": sum(row.status == "AUTO_HEAL_RUNNING" for row in rows),
        "waiting_source": sum(row.status == "WAITING_SOURCE" for row in rows),
        "review_required": sum(row.status in {"AWAITING_ENGINEERING_REVIEW", "REVIEW_REQUIRED"} for row in rows),
        "recovered_today": sum(row.status == "RESOLVED" and row.resolved_at is not None and row.resolved_at >= start for row in rows),
        "exhausted": sum(row.status == "AUTO_REPAIR_EXHAUSTED" for row in rows),
    }


def _stage(
    dataset: DataSet,
    *,
    implemented: bool = True,
    connected: bool,
    freshness: str,
    latest_job: WorkerJob | None,
    latest_run: WorkerRun | None,
    now: datetime,
) -> str:
    if not implemented:
        return "PLANNED"
    if latest_run is not None and latest_run.status == "running":
        return "UPDATING"
    if freshness == "source_blocked":
        return "SOURCE BLOCKED"
    if freshness == "access_pending":
        return "ACCESS REQUIRED"
    if freshness in {"error", "unavailable"}:
        return "ERROR"
    if freshness == "stale":
        return "STALE"
    if not connected:
        return "NOT CONFIGURED"
    if not dataset.enabled or dataset.auto_update_status != AutoUpdateStatus.CONFIGURED:
        return "DISABLED"
    if dataset.last_success_at is None:
        if latest_job is not None and latest_job.status in {"queued", "retry_scheduled", "running"}:
            return "CHECK PENDING"
        return "FIRST RUN"
    if (dataset.coverage or {}).get("operational_accepted") is False:
        return "FIRST RUN"
    if (
        dataset.next_expected_update_at is not None
        and dataset.next_expected_update_at <= now
        and (latest_job is None or latest_job.status not in {"queued", "retry_scheduled", "running"})
    ):
        return "CHECK PENDING"
    return "OPERATIONAL"


CATALOG_STATUS_FILTERS = {
    "operational": {"OPERATIONAL"},
    "updating": {"UPDATING"},
    "first-run": {"FIRST RUN", "CHECK PENDING"},
    "access-required": {"ACCESS REQUIRED"},
    "source-blocked": {"SOURCE BLOCKED"},
    "error": {"ERROR"},
    "stale": {"STALE"},
    "disabled": {"DISABLED", "NOT CONFIGURED"},
    "unconnected": {"PLANNED"},
    "planned": {"PLANNED"},
}


def filter_catalog_rows(
    rows: list[dict[str, Any]], *, query: str = "", status: str = "", family: str = ""
) -> list[dict[str, Any]]:
    """Apply deterministic owner filters without changing global KPI counts."""

    query_folded = query.strip().casefold()
    family_folded = family.strip().casefold()
    stages = CATALOG_STATUS_FILTERS.get(status.strip().lower())
    result = []
    for row in rows:
        haystack = " ".join(
            str(row.get(key) or "")
            for key in ("source_name", "source_id", "dataset_code", "source_group", "fact_family")
        ).casefold()
        if query_folded and query_folded not in haystack:
            continue
        if family_folded and str(row.get("fact_family") or "").casefold() != family_folded:
            continue
        if stages is not None and row.get("stage") not in stages:
            continue
        result.append(row)
    return result


def merge_catalog_metadata(
    metadata_rows: list[dict[str, Any]], operational_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge future catalog imports without ever promoting metadata to runtime.

    Operational state always wins. A confirmed metadata-only record is honest:
    it is planned, disconnected and cannot be enabled. Current production calls
    build both sides from the operational registry; this boundary lets the
    canonical workbook be added later without changing templates or controls.
    """

    operational = {str(row["dataset_code"]): row for row in operational_rows}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for metadata in metadata_rows:
        code = str(metadata["dataset_code"])
        runtime = operational.get(code)
        if runtime is None:
            merged.append({
                **metadata, "source_id": code, "implemented": False,
                "connected": False, "enabled": False, "stage": "PLANNED",
            })
        else:
            merged.append({**metadata, **runtime})
        seen.add(code)
    merged.extend(row for code, row in operational.items() if code not in seen)
    return merged


def _source_rows(session, *, now: datetime, master: dict[str, int]) -> list[dict[str, Any]]:
    datasets = session.execute(
        select(DataSet, DataSource)
        .join(DataSource, DataSet.source_id == DataSource.id)
        .order_by(DataSet.priority, DataSet.name)
    ).all()
    connected = _connected_sources(session)
    jobs = session.scalars(
        select(WorkerJob)
        .where(WorkerJob.source_id.in_(SOURCE_IDS))
        .order_by(WorkerJob.created_at.desc(), WorkerJob.id.desc())
    ).all()
    latest_jobs = _latest_by_source(jobs, lambda item: item.source_id)
    runs = session.execute(
        select(WorkerRun, WorkerJob)
        .join(WorkerJob, WorkerRun.job_id == WorkerJob.id)
        .where(WorkerJob.source_id.in_(SOURCE_IDS))
        .order_by(WorkerRun.started_at.desc(), WorkerRun.attempt_no.desc())
    ).all()
    latest_runs = _latest_by_source(runs, lambda item: item[1].source_id)
    publications = {
        item.source_id: item
        for item in session.scalars(
            select(WorkerPublicationState).where(WorkerPublicationState.source_id.in_(SOURCE_IDS))
        )
    }
    changes = session.scalars(
        select(SourceChangeSummary)
        .where(SourceChangeSummary.source_id.in_(SOURCE_IDS))
        .order_by(SourceChangeSummary.created_at.desc(), SourceChangeSummary.id.desc())
    ).all()
    latest_changes = _latest_by_source(changes, lambda item: item.source_id)
    active_leases = {
        lease.source_id
        for lease in session.scalars(
            select(WorkerLease).where(
                WorkerLease.source_id.in_(SOURCE_IDS), WorkerLease.expires_at > now
            )
        )
    }
    policies = {
        item.source_id: item
        for item in session.scalars(select(SourceAutomationPolicy))
    }
    active_incidents = _latest_by_source(
        session.scalars(
            select(SourceIncident)
            .where(SourceIncident.status.not_in(("RESOLVED", "CANCELLED")))
            .order_by(SourceIncident.detected_at.desc())
        ).all(),
        lambda item: item.source_id,
    )

    rows: list[dict[str, Any]] = []
    for dataset, source in datasets:
        worker_source_id = worker_source_id_for(dataset.code)
        job = latest_jobs.get(worker_source_id)
        run_pair = latest_runs.get(worker_source_id)
        run = run_pair[0] if run_pair else None
        publication = publications.get(worker_source_id)
        change = latest_changes.get(worker_source_id)
        coverage = dict(dataset.coverage or {})
        policy = policies.get(worker_source_id)
        incident = active_incidents.get(worker_source_id)
        freshness = effective_status(dataset, now=now)
        implemented = dataset.code in HANDLERS
        stage = _stage(
            dataset,
            implemented=implemented,
            connected=dataset.code in connected,
            freshness=freshness,
            latest_job=job,
            latest_run=run,
            now=now,
        )
        matched_companies = _int(coverage.get("matched_companies"))
        if matched_companies is None:
            matched_companies = _int(coverage.get("risk_summary_candidate_companies"))
        if dataset.code == "fns_tax_debt" and matched_companies is None:
            matched_companies = _int(coverage.get("projected_facts"))
        applicable = master["total"]
        if dataset.code in {
            "fns_revenue_expenses",
            "fns_tax_offence",
            "fns_tax_paid",
            "fns_tax_debt",
            "fns_headcount",
            "fns_egrul",
            "girbo_accounting",
            "nostroy_sro_members_on_demand",
            "nopriz_sro_members_on_demand",
            "prime_corporate_disclosure",
            "rkn_personal_data_operators",
        }:
            applicable = master["legal"]
        if dataset.code in {"fns_egrip", "fns_npd"}:
            applicable = master["ip"]
        if dataset.code == "fns_tax_debt" and _int(coverage.get("cohort_size")) is not None:
            applicable = int(coverage["cohort_size"])
        row = {
                "source_name": dataset.name,
                "source_group": source.name,
                "source_id": worker_source_id,
                "dataset_code": dataset.code,
                "official_page_url": safe_url(
                    OFFICIAL_PAGE_URLS.get(source.code) or source.website_url
                ),
                "data_access_url": safe_url(
                    DATA_ACCESS_URLS.get(dataset.code) or dataset.source_url
                ),
                # Compatibility for ADMIN-UX-03 templates/tests. New views use
                # the two explicit fields above.
                "official_url": safe_url(
                    OFFICIAL_PAGE_URLS.get(source.code) or source.website_url
                ),
                "access_kind": access_kind(
                    update_mode=dataset.update_mode, data_format=dataset.data_format
                ),
                "fact_family": dataset.domain,
                "description": dataset.description,
                "implemented": implemented,
                "connected": dataset.code in connected,
                "stage": stage,
                "enabled": bool(dataset.enabled),
                "freshness": freshness.upper().replace("_", " "),
                "source_data_date": dataset.last_data_date or (dataset.source_as_of.date() if dataset.source_as_of else None),
                "source_as_of": dataset.source_as_of,
                "official_actual_until": dataset.official_actual_until,
                "last_check": dataset.checked_at or dataset.last_attempt_at,
                "last_successful_run": dataset.last_success_at,
                "last_publication": dataset.published_at,
                "next_scheduled_check": dataset.next_expected_update_at,
                "retry_at": dataset.next_retry_at or (job.next_attempt_at if job and job.status == "retry_scheduled" else None),
                "matched_companies": matched_companies,
                "applicable_companies": applicable,
                "master_coverage": (matched_companies / applicable * 100 if matched_companies is not None and applicable else None),
                "current_fact_count": dataset.record_count,
                "new_facts": change.new_facts if change else None,
                "changed_facts": change.changed_facts if change else None,
                "removed_facts": change.removed_or_expired_facts if change else None,
                "replayed_facts": change.replayed_facts if change else None,
                "quarantined_records": change.quarantined_records if change else None,
                "publication_generation": publication.generation if publication else None,
                "last_error": safe_error_message(dataset.last_error) if dataset.last_error else None,
                "worker_status": run.status.upper() if run else "NO RUN",
                "worker_stage": run.current_stage if run else None,
                "latest_run_id": str(run.id) if run else None,
                "latest_job_id": str(job.id) if job else None,
                "retryable": bool(job and job.status == "retry_scheduled" and run and run.retryable),
                "active_lease": worker_source_id in active_leases,
                "frequency": dataset.refresh_schedule or dataset.freshness_policy,
                "access_channel": f"{dataset.update_mode} / {dataset.data_format}",
                "raw_identity": None,
                "normalized_identity": safe_identity(publication.active_pointer) if publication else None,
                "active_checksum": redact((publication.validation_metadata or {}).get("checksum")) if publication else None,
                "auto_repair": _auto_repair_status(policy, incident),
                "open_incident_id": str(incident.id) if incident else None,
                "open_incident_code": incident.incident_code if incident else None,
                "progress": redact(coverage),
            }
        row["missing_reasons"] = {
            field: _missing_reason(row, field)
            for field in (
                "source_data_date", "official_actual_until", "last_check",
                "last_successful_run", "last_publication", "next_scheduled_check",
                "retry_at",
                "matched_companies", "master_coverage", "current_fact_count",
                "new_facts", "changed_facts", "removed_facts", "replayed_facts",
                "quarantined_records", "publication_generation",
            )
        }
        rows.append(row)
    return rows


def console_snapshot(*, now: datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    with SessionLocal() as session:
        master = _master_counts(session)
        rows = _source_rows(session, now=now, master=master)
        inventory = _source_inventory_counts(rows)
        queue = int(session.scalar(select(func.count()).select_from(WorkerJob).where(WorkerJob.status == "queued")) or 0)
        retries = int(session.scalar(select(func.count()).select_from(WorkerJob).where(WorkerJob.status == "retry_scheduled")) or 0)
        leases = int(session.scalar(select(func.count()).select_from(WorkerLease).where(WorkerLease.expires_at > now)) or 0)
        stages = Counter(row["stage"] for row in rows)
        incident_counts = _incident_counts(session, now=now)
        latest_run = session.execute(
            select(WorkerRun, WorkerJob)
            .join(WorkerJob, WorkerRun.job_id == WorkerJob.id)
            .order_by(WorkerRun.started_at.desc())
            .limit(1)
        ).first()
        return {
            "generated_at": now,
            "sources": rows,
            "source_count": len(rows),
            **inventory,
            "summary": {
                "operational": stages["OPERATIONAL"],
                "first_run": stages["FIRST RUN"] + stages["CHECK PENDING"],
                "stale": stages["STALE"],
                "errors": stages["ERROR"],
                "blocked": stages["SOURCE BLOCKED"] + stages["ACCESS REQUIRED"],
                "disabled": stages["DISABLED"] + stages["NOT CONFIGURED"],
                "planned": stages["PLANNED"],
                "queue": queue,
                "retry_scheduled": retries,
                "active_leases": leases,
            },
            "master": master,
            "enrichment": canonical_enrichment_metrics(session),
            "incidents": incident_counts,
            "latest_run": {
                "id": str(latest_run[0].id),
                "source_id": latest_run[1].source_id,
                "status": latest_run[0].status,
                "started_at": latest_run[0].started_at,
            } if latest_run else None,
        }


def public_publication_snapshot() -> dict[str, Any]:
    with SessionLocal() as session:
        return publication_dashboard(session)


def public_publication_history(*, limit: int = 100) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        return public_sync_history(session, limit=limit)


def source_detail(source_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or utc_now()
    with SessionLocal() as session:
        master = _master_counts(session)
        source = next((item for item in _source_rows(session, now=now, master=master) if item["source_id"] == source_id), None)
        if source is None:
            return None
        run_rows = session.execute(
            select(WorkerRun, WorkerJob, SourceChangeSummary)
            .join(WorkerJob, WorkerRun.job_id == WorkerJob.id)
            .outerjoin(SourceChangeSummary, SourceChangeSummary.run_id == WorkerRun.id)
            .where(WorkerJob.source_id == source_id)
            .order_by(WorkerRun.started_at.desc(), WorkerRun.attempt_no.desc())
            .limit(50)
        ).all()
        manifest = session.execute(
            select(WorkerRawManifest)
            .join(WorkerRun, WorkerRawManifest.run_id == WorkerRun.id)
            .join(WorkerJob, WorkerRun.job_id == WorkerJob.id)
            .where(WorkerJob.source_id == source_id)
            .order_by(WorkerRawManifest.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        source["raw_identity"] = safe_identity(manifest.artifact_reference) if manifest else None
        matched = source["matched_companies"]
        applicable = source["applicable_companies"]
        source["not_found"] = max(applicable - matched, 0) if matched is not None else None
        source["not_applicable"] = max(master["total"] - applicable, 0)
        dataset = session.scalar(
            select(DataSet).where(DataSet.code == dataset_code_for(source_id))
        )
        source["conflicts"] = _int((dataset.coverage or {}).get("conflicts")) if dataset else None
        source["runs"] = [_run_row(run, job, change) for run, job, change in run_rows]
        policy = session.get(SourceAutomationPolicy, source_id)
        incident = session.scalar(
            select(SourceIncident)
            .where(
                SourceIncident.source_id == source_id,
                SourceIncident.status.not_in(("RESOLVED", "CANCELLED")),
            )
            .order_by(SourceIncident.detected_at.desc())
            .limit(1)
        )
        runtime = detect_agent_runtime()
        source["automation"] = {
            "auto_heal": bool(policy and policy.auto_heal_enabled and not policy.paused),
            "auto_code_repair": (
                "ON" if policy and policy.auto_code_repair_enabled and runtime.available
                else "AGENT UNAVAILABLE" if not runtime.available else "OFF"
            ),
            "incident": _incident_row(incident) if incident else None,
        }
        return source


def _error(run: WorkerRun) -> tuple[str | None, str | None]:
    errors = list(run.errors or [])
    if not errors:
        return None, None
    latest = errors[-1]
    return str(latest.get("kind") or "")[:120] or None, safe_error_message(latest.get("message") or "") or None


def _run_row(run: WorkerRun, job: WorkerJob, change: SourceChangeSummary | None) -> dict[str, Any]:
    error_code, error_message = _error(run)
    return {
        "run_id": str(run.id),
        "job_id": str(job.id),
        "type": job.job_type,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_ms": run.duration_ms,
        "records_seen": run.records_seen,
        "matched": change.matched_companies if change else None,
        "published": run.records_published,
        "new_facts": change.new_facts if change else None,
        "changed_facts": change.changed_facts if change else None,
        "removed_facts": change.removed_or_expired_facts if change else None,
        "quarantine": change.quarantined_records if change else None,
        "error_code": error_code,
        "error_message": error_message,
        "retry_time": job.next_attempt_at if job.status == "retry_scheduled" else None,
        "legacy": change is None,
        "current_stage": run.current_stage,
        "worker_id": run.worker_id,
    }


def run_detail(run_id: UUID) -> dict[str, Any] | None:
    with SessionLocal() as session:
        row = session.execute(
            select(WorkerRun, WorkerJob, SourceChangeSummary)
            .join(WorkerJob, WorkerRun.job_id == WorkerJob.id)
            .outerjoin(SourceChangeSummary, SourceChangeSummary.run_id == WorkerRun.id)
            .where(WorkerRun.id == run_id)
        ).first()
        if row is None or row[1].source_id not in SOURCE_IDS:
            return None
        result = _run_row(*row)
        result["schedule_metadata"] = redact(row[1].schedule_metadata)
        result["checksum_metadata"] = redact(row[0].checksum_metadata)
        result["errors"] = redact(row[0].errors)
        result["manifests"] = [
            {
                "checksum_algorithm": manifest.checksum_algorithm,
                "checksum": manifest.checksum,
                "identity": safe_identity(manifest.artifact_reference),
                "created_at": manifest.created_at,
                "manifest": redact_manifest(manifest.manifest),
            }
            for manifest in session.scalars(
                select(WorkerRawManifest)
                .where(WorkerRawManifest.run_id == run_id)
                .order_by(WorkerRawManifest.created_at)
            )
        ]
        return result


def change_history(source_id: str) -> list[dict[str, Any]] | None:
    with SessionLocal() as session:
        dataset_code = dataset_code_for(source_id)
        if session.scalar(select(DataSet.id).where(DataSet.code == dataset_code)) is None:
            return None
        rows = session.execute(
            select(WorkerRun, WorkerJob, SourceChangeSummary)
            .join(WorkerJob, WorkerRun.job_id == WorkerJob.id)
            .outerjoin(SourceChangeSummary, SourceChangeSummary.run_id == WorkerRun.id)
            .where(WorkerJob.source_id == source_id, WorkerRun.status == "succeeded")
            .order_by(WorkerRun.started_at.desc())
            .limit(100)
        ).all()
        return [_run_row(*row) | {
            "source_data_date": row[2].source_data_date if row[2] else None,
            "previous_source_data_date": row[2].previous_source_data_date if row[2] else None,
            "unchanged_facts": row[2].unchanged_facts if row[2] else None,
            "replayed_facts": row[2].replayed_facts if row[2] else None,
            "source_records": row[2].source_records if row[2] else None,
        } for row in rows]


def action_state(source_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        dataset = session.scalar(
            select(DataSet).where(DataSet.code == dataset_code_for(source_id))
        )
        job = session.scalar(
            select(WorkerJob).where(WorkerJob.source_id == source_id).order_by(WorkerJob.created_at.desc()).limit(1)
        )
        connected = bool(dataset and dataset.code in _connected_sources(session))
        return {
            "connected": connected,
            "enabled": bool(dataset.enabled) if dataset else None,
            "auto_update_status": dataset.auto_update_status if dataset else None,
            "next_expected_update_at": dataset.next_expected_update_at.isoformat() if dataset and dataset.next_expected_update_at else None,
            "latest_job_id": str(job.id) if job else None,
            "latest_job_status": job.status if job else None,
            "latest_job_next_attempt_at": job.next_attempt_at.isoformat() if job and job.next_attempt_at else None,
        }


def audit_action(
    *,
    action: str,
    source_id: str | None,
    job_id: UUID | None,
    previous_state: dict[str, Any],
    new_state: dict[str, Any],
    result: str,
    detail: str | None = None,
) -> None:
    with SessionLocal() as session:
        session.add(
            AdminActionAudit(
                action=action,
                source_id=source_id,
                job_id=job_id,
                previous_state=redact(previous_state),
                new_state=redact(new_state),
                result=result,
                actor="local_owner",
                detail=safe_error_message(detail) if detail else None,
            )
        )
        session.commit()


def perform_source_action(source_id: str, action: str) -> dict[str, Any]:
    if action not in ACTION_LABELS:
        raise ValueError("unsupported action")
    before = action_state(source_id)
    if before["enabled"] is None:
        raise LookupError("source not found")
    job_id: UUID | None = None
    try:
        if action in {"activate", "resume", "pause", "check-now"} and before.get("connected") is False:
            raise ValueError("source has no runnable registered handler")
        if action in {"activate", "resume"}:
            configure_source_schedules(
                enabled=True, dataset_codes=[dataset_code_for(source_id)]
            )
        elif action == "pause":
            configure_source_schedules(
                enabled=False, dataset_codes=[dataset_code_for(source_id)]
            )
        elif action == "check-now":
            if before["auto_update_status"] != AutoUpdateStatus.CONFIGURED or not before["enabled"]:
                raise ValueError("source schedule is paused or disabled")
            dataset_code = dataset_code_for(source_id)
            result = run_due_updates(due_codes=[dataset_code])
            if result.get(dataset_code) != "success":
                raise RuntimeError(
                    f"scheduler returned {result.get(dataset_code, 'unknown')}"
                )
        elif action == "retry":
            if not before["latest_job_id"]:
                raise ValueError("source has no worker job")
            job_id = UUID(before["latest_job_id"])
            with SessionLocal() as session:
                retry_job_now(session, job_id=job_id)
                session.commit()
        after = action_state(source_id)
        audit_action(
            action=action,
            source_id=source_id,
            job_id=job_id,
            previous_state=before,
            new_state=after,
            result="success",
        )
        return after
    except Exception as error:
        audit_action(
            action=action,
            source_id=source_id,
            job_id=job_id,
            previous_state=before,
            new_state=action_state(source_id),
            result="failed",
            detail=str(error),
        )
        raise


def audit_rows(*, limit: int = 100) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        return [
            {
                "action_id": str(row.id),
                "timestamp": row.timestamp,
                "action": row.action,
                "source_id": row.source_id,
                "job_id": str(row.job_id) if row.job_id else None,
                "previous_state": redact(row.previous_state),
                "new_state": redact(row.new_state),
                "result": row.result,
                "actor": row.actor,
                "detail": safe_error_message(row.detail) if row.detail else None,
            }
            for row in session.scalars(
                select(AdminActionAudit).order_by(AdminActionAudit.timestamp.desc()).limit(limit)
            )
        ]


def incident_rows(*, limit: int = 200) -> dict[str, Any]:
    now = utc_now()
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(SourceIncident)
                .order_by(SourceIncident.detected_at.desc())
                .limit(limit)
            )
        )
        return {
            "rows": [_incident_row(row) for row in rows],
            "summary": _incident_counts(session, now=now),
            "agent": detect_agent_runtime(),
            "notification_status": "NOTIFICATION CHANNEL NOT CONFIGURED",
        }


def _incident_problem(incident: SourceIncident) -> str:
    if incident.category == "SOURCE_SCHEMA_VIOLATION":
        return "Официальный файл не проходит обязательную проверку схемы."
    if incident.category == "SOURCE_STALE":
        return "Официальный источник не обновил данные в ожидаемый срок."
    if incident.category == "SOURCE_ACCESS_REQUIRED":
        return "Для получения официальных данных требуется настроить доступ."
    if incident.category == "LEASE_STUCK":
        return "Задача worker перестала обновлять служебную блокировку."
    if incident.category == "WORKER_NOT_RUNNING":
        return "Фоновый процесс обработки источников остановлен."
    return safe_error_message(incident.safe_error_message or incident.category)


def _incident_system_action(incident: SourceIncident) -> str:
    if incident.status == "RESOLVED":
        return "Работа восстановлена и проверена."
    if incident.owner_domain == "SOURCE_OWNED":
        return "Ждёт исправленный официальный файл и проверяет источник автоматически."
    if incident.owner_domain == "ACCESS_REQUIRED":
        return "Сохраняет инцидент до настройки требуемого доступа."
    if incident.owner_domain == "OUR_CODE":
        return "Сохраняет диагностику и готовит безопасный пакет для исправления кода."
    return "Выполняет разрешённый сценарий восстановления и проверяет результат."


def _incident_owner_action(incident: SourceIncident, runtime: Any) -> str:
    if incident.status == "RESOLVED":
        return "Ничего делать не требуется."
    if incident.owner_domain == "SOURCE_OWNED":
        return "Ничего делать не требуется. При необходимости можно запросить внеочередную проверку."
    if incident.owner_domain == "ACCESS_REQUIRED":
        return "Нужно настроить доступ к официальному источнику."
    if incident.owner_domain == "OUR_CODE" and not runtime.available:
        return "Coding-agent на HOME WORKER не настроен; диагностический пакет можно подготовить вручную."
    return "Следите за результатом автоматического восстановления."


def incident_detail(incident_id: UUID) -> dict[str, Any] | None:
    with SessionLocal() as session:
        incident = session.get(SourceIncident, incident_id)
        if incident is None:
            return None
        result = _incident_row(incident)
        action_models = list(session.scalars(
            select(SourceIncidentAction)
            .where(SourceIncidentAction.incident_id == incident.id)
            .order_by(SourceIncidentAction.started_at, SourceIncidentAction.id)
        ))
        result["actions"] = [
            {
                "id": str(action.id),
                "action_type": action.action_type,
                "started_at": action.started_at,
                "finished_at": action.finished_at,
                "result": action.result,
                "safe_message": safe_error_message(action.safe_message) if action.safe_message else None,
                "metadata_safe": safe_value(action.metadata_safe),
            }
            for action in action_models
        ]
        policy = session.get(SourceAutomationPolicy, incident.source_id)
        runtime = detect_agent_runtime()
        result["available_actions"] = available_incident_actions(incident, policy, runtime)
        result["automation_paused"] = bool(policy and policy.paused)
        source_checks = [action for action in action_models if action.action_type == "SOURCE_RECHECK"]
        last_check = source_checks[-1] if source_checks else None
        result["source_check_count"] = len(source_checks)
        result["last_source_check"] = last_check.started_at if last_check else None
        result["last_source_check_result"] = last_check.result if last_check else None
        result["problem_summary"] = _incident_problem(incident)
        result["system_action_summary"] = _incident_system_action(incident)
        result["owner_action_summary"] = _incident_owner_action(incident, runtime)
        result["agent_available"] = runtime.available
        return result


def automation_rows() -> dict[str, Any]:
    runtime = detect_agent_runtime()
    with SessionLocal() as session:
        policies = list(
            session.scalars(
                select(SourceAutomationPolicy).order_by(SourceAutomationPolicy.dataset_code)
            )
        )
        active = _latest_by_source(
            session.scalars(
                select(SourceIncident)
                .where(SourceIncident.status.not_in(TERMINAL_INCIDENT_STATUSES))
                .order_by(SourceIncident.detected_at.desc())
            ).all(),
            lambda item: item.source_id,
        )
        return {
            "agent": runtime,
            "notification_status": "NOTIFICATION CHANNEL NOT CONFIGURED",
            "max_attempt_values": tuple(range(1, 11)),
            "cooldown_values": (60, 300, 900, 3600, 21600, 86400),
            "rows": [
                {
                    "source_id": policy.source_id,
                    "dataset_code": policy.dataset_code,
                    "auto_heal_enabled": policy.auto_heal_enabled,
                    "auto_code_repair_enabled": policy.auto_code_repair_enabled,
                    "remediation_level": policy.remediation_level,
                    "max_attempts": policy.max_attempts,
                    "cooldown_seconds": policy.cooldown_seconds,
                    "paused": policy.paused,
                    "circuit_breaker": (
                        "НЕ ПРИМЕНЯЕТСЯ" if active.get(policy.source_id) and active[policy.source_id].owner_domain == "SOURCE_OWNED"
                        else "EXHAUSTED" if active.get(policy.source_id) and active[policy.source_id].status == "AUTO_REPAIR_EXHAUSTED"
                        else f"{active[policy.source_id].attempt_count}/{active[policy.source_id].max_attempts}" if active.get(policy.source_id)
                        else "CLOSED"
                    ),
                    "current_incident": _incident_row(active[policy.source_id]) if active.get(policy.source_id) else None,
                }
                for policy in policies
            ],
        }


def perform_incident_action(incident_id: UUID, action: str) -> dict[str, Any]:
    if action not in INCIDENT_ACTION_LABELS:
        raise ValueError("unsupported incident action")
    before: dict[str, Any] = {}
    source_id: str | None = None
    job_id: UUID | None = None
    try:
        with SessionLocal() as session:
            incident = session.get(SourceIncident, incident_id, with_for_update=True)
            if incident is None:
                raise LookupError("incident not found")
            source_id = incident.source_id
            job_id = incident.job_id
            before = _incident_row(incident)
            policy = session.get(SourceAutomationPolicy, incident.source_id)
            runtime = detect_agent_runtime()
            eligible = available_incident_actions(incident, policy, runtime)
            if action not in eligible:
                raise ValueError("action is not applicable to this incident")
            if action == "check-source-now":
                incident.status = "WAITING_SOURCE"
                incident.auto_heal_enabled = True
                incident.next_attempt_at = utc_now()
                incident.cooldown_until = None
                record_action(
                    session,
                    incident,
                    "MANUAL_SOURCE_RECHECK_REQUESTED",
                    result="QUEUED",
                    metadata={"expected_within_seconds": 10},
                )
            elif action == "run-auto-heal":
                incident.status = "OPEN"
                incident.next_attempt_at = utc_now()
                record_action(session, incident, "AUTO_HEAL_REQUESTED", result="QUEUED")
            elif action == "pause-auto-repair":
                if policy is None:
                    raise ValueError("automation policy is missing")
                policy.paused = True
                incident.auto_heal_enabled = False
                incident.auto_code_repair_enabled = False
                record_action(session, incident, "AUTO_REPAIR_PAUSED", result="SUCCESS")
            elif action == "resume-auto-repair":
                if policy is None:
                    raise ValueError("automation policy is missing")
                policy.paused = False
                policy.auto_heal_enabled = True
                incident.auto_heal_enabled = True
                incident.auto_code_repair_enabled = policy.auto_code_repair_enabled
                if incident.status == "CANCELLED":
                    incident.status = "OPEN"
                incident.next_attempt_at = utc_now()
                record_action(session, incident, "AUTO_REPAIR_RESUMED", result="SUCCESS")
            elif action == "mark-source-owned":
                incident.owner_domain = "SOURCE_OWNED"
                incident.status = "WAITING_SOURCE"
                incident.next_attempt_at = utc_now()
                record_action(session, incident, "CLASSIFIED", result="SUCCESS", metadata={"owner_domain": "SOURCE_OWNED"})
            elif action in {"request-engineering", "prepare-repair-package"}:
                if incident.owner_domain != "OUR_CODE":
                    raise ValueError("engineering repair is allowed only for OUR_CODE incidents")
                evidence = dict(incident.resolution_evidence or {})
                evidence["engineering_requested_at"] = utc_now().isoformat()
                incident.resolution_evidence = safe_value(evidence)
                incident.status = "OPEN"
                incident.next_attempt_at = utc_now()
                record_action(
                    session,
                    incident,
                    "ENGINEERING_REPAIR_REQUESTED",
                    result="QUEUED",
                    metadata={"package_only": action == "prepare-repair-package"},
                )
            elif action == "cancel-pending":
                incident.status = "CANCELLED"
                incident.next_attempt_at = None
                record_action(session, incident, "CANCELLED", result="SUCCESS")
            session.commit()
            after = _incident_row(incident)
        audit_action(
            action=f"incident:{action}", source_id=source_id, job_id=job_id,
            previous_state=before, new_state=after, result="success",
        )
        return after
    except Exception as error:
        audit_action(
            action=f"incident:{action}", source_id=source_id, job_id=job_id,
            previous_state=before, new_state={}, result="failed", detail=str(error),
        )
        raise


def perform_policy_update(
    *,
    source_id: str,
    auto_heal_enabled: bool,
    auto_code_repair_enabled: bool,
    max_attempts: int,
    cooldown_seconds: int,
) -> dict[str, Any]:
    before: dict[str, Any] = {}
    try:
        with SessionLocal() as session:
            existing = session.get(SourceAutomationPolicy, source_id)
            if existing:
                before = {
                    "auto_heal_enabled": existing.auto_heal_enabled,
                    "auto_code_repair_enabled": existing.auto_code_repair_enabled,
                    "max_attempts": existing.max_attempts,
                    "cooldown_seconds": existing.cooldown_seconds,
                    "paused": existing.paused,
                }
            policy = update_policy(
                session,
                source_id=source_id,
                auto_heal_enabled=auto_heal_enabled,
                auto_code_repair_enabled=auto_code_repair_enabled,
                max_attempts=max_attempts,
                cooldown_seconds=cooldown_seconds,
            )
            session.commit()
            after = {
                "auto_heal_enabled": policy.auto_heal_enabled,
                "auto_code_repair_enabled": policy.auto_code_repair_enabled,
                "max_attempts": policy.max_attempts,
                "cooldown_seconds": policy.cooldown_seconds,
                "paused": policy.paused,
            }
        audit_action(
            action="automation-policy-update", source_id=source_id, job_id=None,
            previous_state=before, new_state=after, result="success",
        )
        return after
    except Exception as error:
        audit_action(
            action="automation-policy-update", source_id=source_id, job_id=None,
            previous_state=before, new_state={}, result="failed", detail=str(error),
        )
        raise


def postgres_health() -> dict[str, Any]:
    try:
        with SessionLocal() as session:
            value = session.scalar(select(1))
        return {"available": value == 1, "status": "HEALTHY" if value == 1 else "UNAVAILABLE"}
    except SQLAlchemyError as error:
        return {"available": False, "status": "UNAVAILABLE", "error": type(error).__name__}
