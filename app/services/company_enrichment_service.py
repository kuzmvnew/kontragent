"""Canonical, restartable company enrichment orchestration.

The service is called by the existing Worker Foundation supervisor.  It turns
accepted Master replay signals (and Master rows which predate those signals)
into bounded work and advances Risk/Summary only after the frozen operational
source denominator has semantic, current outcomes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from statistics import median
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.contracts.risk_v3 import UnsupportedSubjectOutcome
from app.database.postgres import SessionLocal
from app.models.company import Company
from app.models.cbr_warning_list import CbrWarningListEntry
from app.models.company_enrichment import CompanyEnrichmentRun, CompanySourceCoverage
from app.models.fns_sme_support import FnsSmeSupportEntry
from app.models.headcount import CompanyHeadcount
from app.models.msp import CompanyMspProfile
from app.models.registry_master import MasterReplaySignal
from app.models.revenue_expense import CompanyRevenueExpenseSnapshot
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from app.models.roskomnadzor import RoskomnadzorCompanyFact
from app.models.source import DataSet
from app.models.tax_offence import CompanyTaxOffence
from app.models.tax_payment import CompanyTaxPaymentSnapshot
from app.models.tax_regime import CompanyTaxRegimeSnapshot
from app.models.worker import (
    WorkerHandlerRegistration,
    WorkerJob,
    WorkerPublicationState,
    WorkerRun,
)
from app.services.data_readiness_service import effective_status, safe_error_message
from app.services.risk_v3_persistence_service import (
    calculate_company_risk_v3_from_persisted,
    get_or_create_summary_v3,
)
from app.worker.execution import JobCreation, create_job


WORKFLOW_VERSION = "company-enrichment-v1"
DATASET_WORKER_SOURCE_IDS = {"fns_tax_debt": "S02"}
LEGAL_ONLY_DATASET_CODES = frozenset(
    {
        "fns_egrul",
        "fns_revenue_expenses",
        "fns_tax_offence",
        "fns_tax_paid",
        "fns_tax_debt",
        "fns_headcount",
        "girbo_accounting",
        "nostroy_sro_members_on_demand",
        "nopriz_sro_members_on_demand",
        "prime_corporate_disclosure",
        "rkn_personal_data_operators",
        "rkn_communications_licenses",
        "rkn_broadcast_licenses",
        "rkn_registered_media",
    }
)
IP_ONLY_DATASET_CODES = frozenset({"fns_egrip", "fns_npd"})
ACTIVE_EXECUTION_STATUSES = frozenset(
    {"pending", "queued", "running", "retry_scheduled"}
)
SUCCESSFUL_COVERAGE_STATUSES = frozenset({"FOUND", "NOT_FOUND", "NOT_APPLICABLE"})
TRANSIENT_COVERAGE_STATUSES = frozenset(
    {"SOURCE_UNAVAILABLE", "TIMEOUT", "PARSING_ERROR", "STALE_DATA", "ACCESS_REQUIRED"}
)
LOCAL_SNAPSHOT_SOURCES = frozenset(
    {
        "cbr_warning_list",
        "rkn_communications_licenses",
        "rkn_broadcast_licenses",
        "rkn_registered_media",
    }
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EnrichmentCreation:
    run: CompanyEnrichmentRun
    created: bool


@dataclass(frozen=True)
class ReplayConsumption:
    signals_seen: int
    signals_scheduled: int
    runs_created: int
    jobs_created: int


@dataclass(frozen=True)
class SourcePlan:
    dataset_id: int
    source_id: str
    worker_source_id: str
    mode: str
    handler_version: str
    publication_generation: int | None
    source_data_date: date | None
    replay_pointer: str | None
    replay_checksum: str | None
    snapshot: dict[str, Any]


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _worker_source_id(dataset_code: str) -> str:
    return DATASET_WORKER_SOURCE_IDS.get(dataset_code, dataset_code)


def _company_scope(company: Company) -> str:
    value = str(company.entity_type or "").strip().lower()
    if value in {"legal", "legal_entity", "organization"} or len(company.inn) == 10:
        return "legal"
    if value in {"individual_entrepreneur", "ip", "entrepreneur"} or len(company.inn) == 12:
        return "ip"
    return "unknown"


def _is_applicable(company: Company, dataset_code: str) -> bool:
    scope = _company_scope(company)
    if dataset_code in LEGAL_ONLY_DATASET_CODES:
        return scope == "legal"
    if dataset_code in IP_ONLY_DATASET_CODES:
        return scope == "ip"
    return True


def _latest_handler(
    session: Session, worker_source_id: str
) -> WorkerHandlerRegistration | None:
    return session.scalar(
        select(WorkerHandlerRegistration)
        .where(
            WorkerHandlerRegistration.source_id == worker_source_id,
            WorkerHandlerRegistration.approved.is_(True),
            WorkerHandlerRegistration.enabled.is_(True),
            WorkerHandlerRegistration.live_mode.is_(False),
        )
        .order_by(
            WorkerHandlerRegistration.registered_at.desc(),
            WorkerHandlerRegistration.handler_version.desc(),
        )
        .limit(1)
    )


def _producer_job(
    session: Session, state: WorkerPublicationState | None
) -> WorkerJob | None:
    if state is None or state.published_by_run_id is None:
        return None
    run = session.get(WorkerRun, state.published_by_run_id)
    return session.get(WorkerJob, run.job_id) if run is not None else None


def _source_plan(
    session: Session,
    company: Company,
    dataset: DataSet,
    *,
    now: datetime,
) -> SourcePlan | None:
    if not dataset.enabled or not _is_applicable(company, dataset.code):
        return None
    if dataset.auto_update_status != AutoUpdateStatus.CONFIGURED:
        return None
    if dataset.last_success_at is None:
        return None
    if effective_status(dataset, now=now) != OperationalStatus.CURRENT:
        return None
    if (dataset.coverage or {}).get("operational_accepted") is not True:
        return None

    worker_source_id = _worker_source_id(dataset.code)
    registration = _latest_handler(session, worker_source_id)
    if registration is None:
        return None
    registration_metadata = dict(registration.metadata_json or {})
    point_source = (
        dataset.dataset_kind == "on_demand_api"
        or dataset.update_mode in {"api", "on_demand"}
        or registration_metadata.get("mode")
        == "bounded_daily_master_exact_inn_sweep"
    )
    mode = (
        "point_check"
        if point_source
        else "local_snapshot_lookup"
        if dataset.code in LOCAL_SNAPSHOT_SOURCES
        else "local_bulk_replay"
    )
    state = session.get(WorkerPublicationState, worker_source_id)
    producer = _producer_job(session, state)
    handler_version = (
        producer.handler_version
        if mode == "local_bulk_replay" and producer is not None
        else registration.handler_version
    )
    validation = dict(state.validation_metadata or {}) if state else {}
    validation_payload = dict(validation.get("validation") or {})
    source_data_date = dataset.last_data_date
    if source_data_date is None and dataset.source_as_of is not None:
        source_data_date = dataset.source_as_of.date()
    pointer = state.active_pointer if state else None
    checksum = str(validation.get("checksum") or "") or None
    snapshot = {
        "dataset_id": dataset.id,
        "source_id": dataset.code,
        "worker_source_id": worker_source_id,
        "mode": mode,
        "handler_version": handler_version,
        "handler_mode": registration_metadata.get("mode"),
        "operational_status": OperationalStatus.CURRENT.value,
        "operational_accepted": True,
        "source_data_date": _iso(source_data_date),
        "last_success_at": _iso(dataset.last_success_at),
        "publication_generation": state.generation if state else None,
        "release_identity": validation_payload.get("release_identity"),
        "producer_job_id": str(producer.id) if producer else None,
        "local_snapshot_available": bool(pointer and checksum),
        "frozen_at": now.isoformat(),
    }
    return SourcePlan(
        dataset_id=dataset.id,
        source_id=dataset.code,
        worker_source_id=worker_source_id,
        mode=mode,
        handler_version=handler_version,
        publication_generation=state.generation if state else None,
        source_data_date=source_data_date,
        replay_pointer=pointer,
        replay_checksum=checksum,
        snapshot=snapshot,
    )


def freeze_operational_sources(
    session: Session,
    company: Company,
    *,
    source_ids: Sequence[str] | None = None,
    now: datetime | None = None,
) -> tuple[SourcePlan, ...]:
    """Freeze the applicable sources that are operational at plan time."""

    now = now or utc_now()
    statement = select(DataSet).order_by(DataSet.code)
    if source_ids is not None:
        requested = tuple(dict.fromkeys(source_ids))
        if not requested:
            return ()
        statement = statement.where(DataSet.code.in_(requested))
    plans = (
        _source_plan(session, company, dataset, now=now)
        for dataset in session.scalars(statement)
    )
    return tuple(plan for plan in plans if plan is not None)


def create_enrichment_run(
    session: Session,
    *,
    company_id: int,
    trigger: str,
    idempotency_key: str,
    source_ids: Sequence[str] | None = None,
    source_signal_ids: Mapping[str, Sequence[UUID]] | None = None,
    max_restarts: int = 2,
    now: datetime | None = None,
) -> EnrichmentCreation:
    """Create one run and its immutable source denominator idempotently."""

    now = now or utc_now()
    if not trigger.strip() or not idempotency_key.strip():
        raise ValueError("trigger and idempotency_key are required")
    if max_restarts < 0:
        raise ValueError("max_restarts cannot be negative")
    existing = session.scalar(
        select(CompanyEnrichmentRun).where(
            CompanyEnrichmentRun.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if existing.company_id != company_id or existing.trigger != trigger:
            raise ValueError("enrichment idempotency key belongs to another request")
        return EnrichmentCreation(existing, False)

    company = session.get(Company, company_id)
    if company is None:
        raise ValueError("Company not found")
    plans = freeze_operational_sources(
        session, company, source_ids=source_ids, now=now
    )
    if not plans:
        raise ValueError("company has no applicable operational sources")
    signal_map = {
        source_id: tuple(dict.fromkeys(values))
        for source_id, values in (source_signal_ids or {}).items()
    }
    run = CompanyEnrichmentRun(
        company_id=company.id,
        trigger=trigger,
        idempotency_key=idempotency_key,
        workflow_version=WORKFLOW_VERSION,
        status="pending",
        stage="planning",
        applicable_sources=[plan.snapshot for plan in plans],
        source_count=len(plans),
        completed_source_count=0,
        failed_source_count=0,
        max_restarts=max_restarts,
        public_ready=False,
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(run)
    session.flush()
    for plan in plans:
        session.add(
            CompanySourceCoverage(
                enrichment_run_id=run.id,
                company_id=company.id,
                dataset_id=plan.dataset_id,
                source_id=plan.source_id,
                worker_source_id=plan.worker_source_id,
                mode=plan.mode,
                status="PENDING",
                execution_status="pending",
                source_snapshot=plan.snapshot,
                handler_version=plan.handler_version,
                publication_generation=plan.publication_generation,
                source_data_date=plan.source_data_date,
                replay_pointer=plan.replay_pointer,
                replay_checksum=plan.replay_checksum,
                master_replay_signal_ids=[
                    str(value) for value in signal_map.get(plan.source_id, ())
                ],
                max_attempts=4 if plan.mode == "point_check" else 3,
                created_at=now,
                updated_at=now,
            )
        )
    session.flush()
    return EnrichmentCreation(run, True)


def _uuid_values(values: Sequence[str]) -> tuple[UUID, ...]:
    return tuple(UUID(str(value)) for value in values)


def _digest(values: Sequence[str]) -> str:
    return sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()[:20]


def _coverage_error(
    coverage: CompanySourceCoverage, error: object, *, now: datetime
) -> None:
    coverage.status = "SOURCE_UNAVAILABLE"
    coverage.execution_status = "failed"
    coverage.last_error = safe_error_message(error)
    coverage.finished_at = now
    coverage.updated_at = now


def _complete_replay_signals(
    session: Session, coverage: CompanySourceCoverage, *, now: datetime
) -> None:
    signal_ids = _uuid_values(coverage.master_replay_signal_ids)
    if not signal_ids:
        return
    session.execute(
        update(MasterReplaySignal)
        .where(
            MasterReplaySignal.id.in_(signal_ids),
            MasterReplaySignal.target_source_id == coverage.source_id,
            MasterReplaySignal.status.in_(("pending", "scheduled")),
        )
        .values(status="complete", completed_at=now, last_error=None)
    )


def _frozen_snapshot_is_current(
    session: Session, coverage: CompanySourceCoverage
) -> bool:
    state = session.get(WorkerPublicationState, coverage.worker_source_id)
    if state is None:
        return False
    validation = dict(state.validation_metadata or {})
    return bool(
        state.generation == coverage.publication_generation
        and state.active_pointer
        and state.active_pointer == coverage.replay_pointer
        and str(validation.get("checksum") or "") == str(coverage.replay_checksum or "")
    )


def _coverage_fact_count(session: Session, coverage: CompanySourceCoverage) -> int:
    """Count facts from the accepted frozen snapshot using exact identity."""

    if coverage.source_id == "fns_tax_offence":
        statement = select(func.count()).select_from(CompanyTaxOffence).where(
            CompanyTaxOffence.company_id == coverage.company_id,
            CompanyTaxOffence.dataset_id == coverage.dataset_id,
        )
    elif coverage.source_id == "fns_revenue_expenses":
        statement = select(func.count()).select_from(CompanyRevenueExpenseSnapshot).where(
            CompanyRevenueExpenseSnapshot.company_id == coverage.company_id,
            CompanyRevenueExpenseSnapshot.dataset_id == coverage.dataset_id,
        )
    elif coverage.source_id == "fns_tax_paid":
        statement = select(func.count()).select_from(CompanyTaxPaymentSnapshot).where(
            CompanyTaxPaymentSnapshot.company_id == coverage.company_id,
            CompanyTaxPaymentSnapshot.dataset_id == coverage.dataset_id,
        )
    elif coverage.source_id == "fns_headcount":
        statement = select(func.count()).select_from(CompanyHeadcount).where(
            CompanyHeadcount.company_id == coverage.company_id,
            CompanyHeadcount.dataset_id == coverage.dataset_id,
        )
    elif coverage.source_id == "fns_msp":
        statement = select(func.count()).select_from(CompanyMspProfile).where(
            CompanyMspProfile.company_id == coverage.company_id,
            CompanyMspProfile.dataset_id == coverage.dataset_id,
        )
    elif coverage.source_id == "fns_tax_regime":
        statement = select(func.count()).select_from(CompanyTaxRegimeSnapshot).where(
            CompanyTaxRegimeSnapshot.company_id == coverage.company_id,
            CompanyTaxRegimeSnapshot.data_date == coverage.source_data_date,
        )
    elif coverage.source_id == "fns_sme_support":
        inn = session.scalar(select(Company.inn).where(Company.id == coverage.company_id))
        statement = select(func.count()).select_from(FnsSmeSupportEntry).where(
            FnsSmeSupportEntry.dataset_id == coverage.dataset_id,
            FnsSmeSupportEntry.recipient_inn == inn,
        )
    elif coverage.source_id == "cbr_warning_list":
        inn = session.scalar(select(Company.inn).where(Company.id == coverage.company_id))
        statement = select(func.count()).select_from(CbrWarningListEntry).where(
            CbrWarningListEntry.dataset_id == coverage.dataset_id,
            CbrWarningListEntry.inn == inn,
        )
    elif coverage.source_id in {
        "rkn_communications_licenses",
        "rkn_broadcast_licenses",
        "rkn_registered_media",
    }:
        inn = session.scalar(select(Company.inn).where(Company.id == coverage.company_id))
        statement = select(func.count()).select_from(RoskomnadzorCompanyFact).where(
            RoskomnadzorCompanyFact.dataset_id == coverage.dataset_id,
            RoskomnadzorCompanyFact.inn == inn,
        )
    else:
        run = _latest_worker_run(session, coverage.worker_job_id) if coverage.worker_job_id else None
        return int((run.records_published or run.records_written or 0) if run else 0)
    return int(session.scalar(statement) or 0)


def _resolve_successful_coverage(
    session: Session, coverage: CompanySourceCoverage, *, now: datetime
) -> None:
    if (
        coverage.mode != "point_check"
        and not _frozen_snapshot_is_current(session, coverage)
    ):
        coverage.status = "STALE_DATA"
        coverage.last_error = "Frozen accepted source generation is no longer current"
        coverage.checked_at = now
        coverage.finished_at = now
        return
    coverage.fact_count = _coverage_fact_count(session, coverage)
    coverage.status = "FOUND" if coverage.fact_count else "NOT_FOUND"
    coverage.last_error = None
    coverage.checked_at = now
    coverage.finished_at = now
    _complete_replay_signals(session, coverage, now=now)


def _resolve_local_snapshot_coverage(
    session: Session, coverage: CompanySourceCoverage, *, now: datetime
) -> None:
    coverage.execution_status = "succeeded"
    _resolve_successful_coverage(session, coverage, now=now)
    coverage.updated_at = now


def _enqueue_bulk_group(
    session: Session,
    rows: Sequence[CompanySourceCoverage],
    *,
    now: datetime,
) -> JobCreation:
    first = rows[0]
    state = session.get(WorkerPublicationState, first.worker_source_id)
    if state is None or not state.active_pointer:
        raise ValueError("accepted local publication is unavailable")
    if state.generation != first.publication_generation:
        raise ValueError("frozen source generation changed")
    validation = dict(state.validation_metadata or {})
    checksum = str(validation.get("checksum") or "")
    if state.active_pointer != first.replay_pointer or checksum != first.replay_checksum:
        raise ValueError("frozen local replay pointer changed")
    producer = _producer_job(session, state)
    if producer is None or producer.handler_version != first.handler_version:
        raise ValueError("accepted publication producer is unavailable")
    if any(
        row.publication_generation != first.publication_generation
        or row.replay_pointer != first.replay_pointer
        or row.replay_checksum != first.replay_checksum
        or row.handler_version != first.handler_version
        for row in rows
    ):
        raise ValueError("bulk replay group has mixed frozen generations")

    signal_ids = tuple(
        dict.fromkeys(
            signal_id
            for row in rows
            for signal_id in _uuid_values(row.master_replay_signal_ids)
        )
    )
    run_ids = tuple(sorted(str(row.enrichment_run_id) for row in rows))
    restart_generation = _digest(
        [
            f"{row.enrichment_run_id}:"
            f"{session.get(CompanyEnrichmentRun, row.enrichment_run_id).restart_count}"
            for row in rows
        ]
    )
    company_ids = tuple(sorted({row.company_id for row in rows}))
    identity_values = [str(value) for value in signal_ids] or list(run_ids)
    metadata = dict(producer.schedule_metadata or {})
    metadata.pop("master_replay_signal_ids", None)
    metadata.pop("master_replay_target_source_id", None)
    metadata.update(
        {
            "check_only": True,
            "replay_snapshot": True,
            "replay_pointer": state.active_pointer,
            "replay_checksum": checksum,
            "company_enrichment_run_ids": list(run_ids),
            "company_ids": list(company_ids),
            "download_allowed": False,
        }
    )
    return create_job(
        session,
        source_id=first.worker_source_id,
        job_type="company_enrichment_local_replay",
        handler_version=first.handler_version,
        idempotency_key=(
            f"company-enrichment:bulk:{first.source_id}:"
            f"{first.publication_generation}:{_digest(identity_values)}:"
            f"restart:{restart_generation}"
        ),
        schedule_metadata=metadata,
        max_attempts=first.max_attempts,
        timeout_seconds=producer.timeout_seconds,
        now=now,
        master_replay_signal_ids=signal_ids,
        master_replay_target_source_id=first.source_id,
    )


def _latest_point_job(session: Session, source_id: str) -> WorkerJob | None:
    jobs = tuple(
        session.scalars(
            select(WorkerJob)
            .where(WorkerJob.source_id == source_id)
            .order_by(WorkerJob.created_at.desc(), WorkerJob.id.desc())
            .limit(50)
        )
    )
    return next(
        (
            job
            for job in jobs
            if str((job.schedule_metadata or {}).get("raw_root") or "").strip()
        ),
        None,
    )


def _enqueue_point_coverage(
    session: Session,
    coverage: CompanySourceCoverage,
    *,
    restart_count: int,
    now: datetime,
) -> JobCreation:
    handler = session.get(
        WorkerHandlerRegistration,
        (coverage.worker_source_id, coverage.handler_version),
    )
    if (
        handler is None
        or not handler.approved
        or not handler.enabled
        or handler.live_mode
        or (handler.metadata_json or {}).get("mode")
        != "bounded_daily_master_exact_inn_sweep"
    ):
        raise ValueError("bounded point-source handler contract is unavailable")
    company = session.get(Company, coverage.company_id)
    if company is None:
        raise ValueError("Company not found")
    previous = _latest_point_job(session, coverage.worker_source_id)
    if previous is None:
        raise ValueError("point-source RAW root is not configured")
    metadata = dict(previous.schedule_metadata or {})
    metadata.pop("master_replay_signal_ids", None)
    metadata.pop("master_replay_target_source_id", None)
    metadata.update(
        {
            "sweep_id": f"company-enrichment:{coverage.enrichment_run_id}",
            "cohort_size": 1,
            "inn": company.inn,
            "company_id": company.id,
            "request_date": now.date().isoformat(),
            "matching_method": "inn_exact",
            "scheduled_index": 0,
            "company_enrichment_run_id": str(coverage.enrichment_run_id),
            "bounded_point_check": True,
        }
    )
    signal_ids = _uuid_values(coverage.master_replay_signal_ids)
    return create_job(
        session,
        source_id=coverage.worker_source_id,
        job_type="company_enrichment_point_check",
        handler_version=coverage.handler_version,
        idempotency_key=(
            f"company-enrichment:point:{coverage.enrichment_run_id}:"
            f"{coverage.source_id}:restart:{restart_count}"
        ),
        schedule_metadata=metadata,
        max_attempts=coverage.max_attempts,
        timeout_seconds=min(previous.timeout_seconds, 300),
        now=now,
        master_replay_signal_ids=signal_ids,
        master_replay_target_source_id=coverage.source_id,
    )


def enqueue_enrichment_work(
    session: Session,
    run_ids: Sequence[UUID],
    *,
    now: datetime | None = None,
) -> int:
    """Attach pending coverage rows to executable, bounded Worker jobs."""

    now = now or utc_now()
    requested = tuple(dict.fromkeys(run_ids))
    if not requested:
        return 0
    runs = {
        run.id: run
        for run in session.scalars(
            select(CompanyEnrichmentRun)
            .where(CompanyEnrichmentRun.id.in_(requested))
            .with_for_update()
        )
    }
    pending = tuple(
        session.scalars(
            select(CompanySourceCoverage)
            .where(
                CompanySourceCoverage.enrichment_run_id.in_(requested),
                CompanySourceCoverage.execution_status == "pending",
            )
            .order_by(CompanySourceCoverage.source_id, CompanySourceCoverage.id)
            .with_for_update(skip_locked=True)
        )
    )
    jobs_created = 0
    bulk_groups: dict[tuple[Any, ...], list[CompanySourceCoverage]] = defaultdict(list)
    point_rows: list[CompanySourceCoverage] = []
    for row in pending:
        if row.mode == "local_snapshot_lookup":
            try:
                _resolve_local_snapshot_coverage(session, row, now=now)
            except Exception as error:
                _coverage_error(row, error, now=now)
        elif row.mode == "local_bulk_replay":
            bulk_groups[
                (
                    row.source_id,
                    row.worker_source_id,
                    row.handler_version,
                    row.publication_generation,
                    row.replay_pointer,
                    row.replay_checksum,
                )
            ].append(row)
        else:
            point_rows.append(row)

    for rows in bulk_groups.values():
        try:
            creation = _enqueue_bulk_group(session, rows, now=now)
        except Exception as error:
            for row in rows:
                _coverage_error(row, error, now=now)
            continue
        jobs_created += int(creation.created)
        for row in rows:
            row.worker_job_id = creation.job.id
            row.execution_status = creation.job.status
            row.status = "RUNNING"
            row.updated_at = now

    for row in point_rows:
        run = runs[row.enrichment_run_id]
        try:
            creation = _enqueue_point_coverage(
                session, row, restart_count=run.restart_count, now=now
            )
        except Exception as error:
            _coverage_error(row, error, now=now)
            continue
        jobs_created += int(creation.created)
        row.worker_job_id = creation.job.id
        row.execution_status = creation.job.status
        row.status = "RUNNING"
        row.updated_at = now

    for run in runs.values():
        _refresh_run_state(session, run, now=now, allow_projection=False)
    return jobs_created


def consume_master_replay_signals(
    session: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> ReplayConsumption:
    """Consume a backlog slice into idempotent per-company enrichment runs."""

    now = now or utc_now()
    if limit <= 0:
        raise ValueError("limit must be positive")
    candidate_companies = tuple(
        session.scalars(
            select(MasterReplaySignal.company_id)
            .join(DataSet, DataSet.code == MasterReplaySignal.target_source_id)
            .where(
                MasterReplaySignal.status == "pending",
                DataSet.enabled.is_(True),
                DataSet.auto_update_status == AutoUpdateStatus.CONFIGURED,
                DataSet.operational_status == OperationalStatus.CURRENT,
                DataSet.last_success_at.is_not(None),
                DataSet.coverage["operational_accepted"].as_boolean().is_(True),
            )
            .group_by(MasterReplaySignal.company_id)
            .order_by(func.min(MasterReplaySignal.created_at), MasterReplaySignal.company_id)
            .limit(limit)
        )
    )
    signals = tuple(
        session.scalars(
            select(MasterReplaySignal)
            .where(
                MasterReplaySignal.status == "pending",
                MasterReplaySignal.company_id.in_(candidate_companies),
            )
            .order_by(MasterReplaySignal.created_at, MasterReplaySignal.id)
            .with_for_update(skip_locked=True)
        )
    ) if candidate_companies else ()
    grouped: dict[int, list[MasterReplaySignal]] = defaultdict(list)
    for signal in signals:
        grouped[signal.company_id].append(signal)

    created_run_ids: list[UUID] = []
    scheduled_signal_ids: set[UUID] = set()
    runs_created = 0
    for company_id, company_signals in grouped.items():
        company = session.get(Company, company_id)
        if company is None:
            continue
        by_source: dict[str, list[UUID]] = defaultdict(list)
        for signal in company_signals:
            by_source[signal.target_source_id].append(signal.id)
        plans = freeze_operational_sources(session, company, now=now)
        eligible_sources = {plan.source_id for plan in plans}
        if not eligible_sources:
            continue
        source_signal_ids = {
            source_id: tuple(by_source.get(source_id, ()))
            for source_id in sorted(eligible_sources)
        }
        eligible_signal_ids = tuple(
            signal_id
            for source_id in sorted(source_signal_ids)
            for signal_id in source_signal_ids[source_id]
        )
        trigger_ids = eligible_signal_ids or tuple(signal.id for signal in company_signals)
        key = f"master-replay:{company_id}:{_digest([str(value) for value in trigger_ids])}"
        creation = create_enrichment_run(
            session,
            company_id=company_id,
            trigger="master_replay",
            idempotency_key=key,
            source_ids=tuple(sorted(eligible_sources)),
            source_signal_ids=source_signal_ids,
            now=now,
        )
        created_run_ids.append(creation.run.id)
        runs_created += int(creation.created)
        scheduled_signal_ids.update(eligible_signal_ids)

    jobs_created = enqueue_enrichment_work(session, created_run_ids, now=now)
    return ReplayConsumption(
        signals_seen=len(signals),
        signals_scheduled=len(scheduled_signal_ids),
        runs_created=runs_created,
        jobs_created=jobs_created,
    )


def _latest_worker_run(session: Session, job_id: UUID) -> WorkerRun | None:
    return session.scalar(
        select(WorkerRun)
        .where(WorkerRun.job_id == job_id)
        .order_by(WorkerRun.attempt_no.desc(), WorkerRun.started_at.desc())
        .limit(1)
    )


def _sync_coverage_from_job(
    session: Session, coverage: CompanySourceCoverage, *, now: datetime
) -> None:
    if coverage.worker_job_id is None:
        return
    job = session.get(WorkerJob, coverage.worker_job_id)
    if job is None:
        _coverage_error(coverage, "Worker job is missing", now=now)
        return
    run = _latest_worker_run(session, job.id)
    if run is not None:
        coverage.worker_run_id = run.id
        coverage.attempt_count = run.attempt_no
        coverage.started_at = coverage.started_at or run.started_at
        if run.errors:
            coverage.last_error = safe_error_message(
                (run.errors[-1] or {}).get("message") or "worker execution failed"
            )
    mapped = {
        "queued": "queued",
        "running": "running",
        "retry_scheduled": "retry_scheduled",
        "succeeded": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
    }
    coverage.execution_status = mapped[job.status]
    coverage.updated_at = now
    if coverage.execution_status in {"queued", "running", "retry_scheduled"}:
        coverage.status = "RUNNING"
    elif coverage.execution_status == "succeeded":
        _resolve_successful_coverage(session, coverage, now=now)
    elif coverage.execution_status in {"failed", "cancelled"}:
        error_kind = str((run.errors[-1] or {}).get("kind") or "") if run and run.errors else ""
        if "timeout" in error_kind:
            coverage.status = "TIMEOUT"
        elif error_kind in {"invalid_data", "schema_mismatch", "parser"}:
            coverage.status = "PARSING_ERROR"
        else:
            coverage.status = "SOURCE_UNAVAILABLE"
    if coverage.execution_status in {"succeeded", "failed", "cancelled"}:
        coverage.finished_at = (
            run.finished_at if run is not None and run.finished_at is not None else now
        )


def _refresh_run_state(
    session: Session,
    run: CompanyEnrichmentRun,
    *,
    now: datetime,
    allow_projection: bool,
) -> CompanyEnrichmentRun:
    rows = tuple(
        session.scalars(
            select(CompanySourceCoverage)
            .where(CompanySourceCoverage.enrichment_run_id == run.id)
            .order_by(CompanySourceCoverage.source_id)
            .with_for_update()
        )
    )
    for row in rows:
        _sync_coverage_from_job(session, row, now=now)
    succeeded = sum(row.status in SUCCESSFUL_COVERAGE_STATUSES for row in rows)
    failed = sum(
        row.status in TRANSIENT_COVERAGE_STATUSES
        and row.execution_status in {"failed", "cancelled", "succeeded"}
        for row in rows
    )
    active = [
        row for row in rows if row.execution_status in ACTIVE_EXECUTION_STATUSES
    ]
    run.completed_source_count = succeeded
    run.failed_source_count = failed
    run.updated_at = now
    run.stage = "source_enrichment"
    if not rows:
        run.status = "failed"
        run.stage = "failed"
        run.last_error_code = "empty_source_plan"
        run.last_error = "No applicable operational sources were frozen"
        run.finished_at = now
        return run
    if active:
        run.status = (
            "retry_scheduled"
            if any(row.execution_status == "retry_scheduled" for row in active)
            else "waiting_sources"
        )
        return run
    if failed:
        run.status = "failed"
        run.stage = "failed"
        run.last_error_code = "source_enrichment_failed"
        run.last_error = f"{failed} of {len(rows)} frozen source checks failed"
        run.finished_at = now
        return run
    if succeeded != run.source_count:
        run.status = "failed"
        run.stage = "failed"
        run.last_error_code = "coverage_count_mismatch"
        run.last_error = "Frozen source denominator does not match persisted coverage"
        run.finished_at = now
        return run
    if not allow_projection:
        run.status = "running"
        run.stage = "risk"
        return run

    try:
        with session.begin_nested():
            run.status = "running"
            run.stage = "risk"
            assessment, _risk_reused = calculate_company_risk_v3_from_persisted(
                session, run.company_id, calculated_at=now
            )
            if isinstance(assessment, UnsupportedSubjectOutcome):
                run.status = "succeeded"
                run.stage = "complete"
                run.public_ready = False
                run.finished_at = now
                run.last_error_code = None
                run.last_error = None
                return run
            session.flush()
            risk_row = session.scalar(
                select(CompanyRiskAssessmentV3).where(
                    CompanyRiskAssessmentV3.assessment_id == assessment.assessment_id
                )
            )
            if risk_row is None:
                raise RuntimeError("Risk v3 was not persisted")
            run.risk_assessment_id = risk_row.assessment_id
            run.stage = "summary"
            summary, _summary_reused = get_or_create_summary_v3(
                session, risk_row.assessment_id, generated_at=now
            )
            session.flush()
            summary_row = session.scalar(
                select(CompanySummaryV3).where(
                    CompanySummaryV3.summary_id == summary.summary_id,
                    CompanySummaryV3.risk_assessment_id == risk_row.assessment_id,
                )
            )
            if summary_row is None:
                raise RuntimeError("Summary v3 was not persisted after Risk v3")
            run.summary_id = summary_row.summary_id
            run.status = "succeeded"
            run.stage = "complete"
            run.public_ready = True
            run.finished_at = now
            run.last_error_code = None
            run.last_error = None
    except Exception as error:
        run.status = "failed"
        run.stage = "failed"
        run.public_ready = False
        run.finished_at = now
        run.last_error_code = "risk_summary_projection_failed"
        run.last_error = safe_error_message(error)
    return run


def reconcile_enrichment_run(
    session: Session,
    run_id: UUID,
    *,
    now: datetime | None = None,
) -> CompanyEnrichmentRun:
    """Refresh Worker statuses and enforce the source→Risk→Summary gate."""

    now = now or utc_now()
    run = session.scalar(
        select(CompanyEnrichmentRun)
        .where(CompanyEnrichmentRun.id == run_id)
        .with_for_update()
    )
    if run is None:
        raise LookupError(f"enrichment run not found: {run_id}")
    if run.status in {"succeeded", "cancelled"}:
        return run
    return _refresh_run_state(session, run, now=now, allow_projection=True)


def restart_enrichment_run(
    session: Session,
    run_id: UUID,
    *,
    now: datetime | None = None,
) -> CompanyEnrichmentRun:
    """Restart only failed expectations; successful source work is preserved."""

    now = now or utc_now()
    run = session.scalar(
        select(CompanyEnrichmentRun)
        .where(CompanyEnrichmentRun.id == run_id)
        .with_for_update()
    )
    if run is None:
        raise LookupError(f"enrichment run not found: {run_id}")
    if run.status != "failed":
        raise ValueError("only a failed enrichment run can be restarted")
    if run.restart_count >= run.max_restarts:
        raise ValueError("enrichment restart limit exhausted")
    rows = tuple(
        session.scalars(
            select(CompanySourceCoverage)
            .where(CompanySourceCoverage.enrichment_run_id == run.id)
            .with_for_update()
        )
    )
    run.restart_count += 1
    run.status = "pending"
    run.stage = "source_enrichment"
    run.public_ready = False
    run.finished_at = None
    run.last_error_code = None
    run.last_error = None
    run.updated_at = now
    for row in rows:
        if not (
            row.status in TRANSIENT_COVERAGE_STATUSES
            or row.execution_status in {"failed", "cancelled"}
        ):
            continue
        signal_ids = _uuid_values(row.master_replay_signal_ids)
        if signal_ids:
            session.execute(
                update(MasterReplaySignal)
                .where(
                    MasterReplaySignal.id.in_(signal_ids),
                    MasterReplaySignal.status == "failed",
                )
                .values(
                    status="pending",
                    scheduled_at=None,
                    completed_at=None,
                    last_error=None,
                )
            )
        row.status = "PENDING"
        row.execution_status = "pending"
        row.worker_job_id = None
        row.worker_run_id = None
        row.attempt_count = 0
        row.last_error = None
        row.started_at = None
        row.finished_at = None
        row.updated_at = now
    enqueue_enrichment_work(session, (run.id,), now=now)
    return run


def get_company_public_readiness(
    session: Session, company_id: int
) -> dict[str, Any]:
    """Canonical additive public-readiness projection; performs no enrichment."""

    run = session.scalar(
        select(CompanyEnrichmentRun)
        .where(CompanyEnrichmentRun.company_id == company_id)
        .order_by(
            CompanyEnrichmentRun.created_at.desc(), CompanyEnrichmentRun.id.desc()
        )
        .limit(1)
    )
    if run is None:
        return {
            "status": "NOT_STARTED",
            "ready": False,
            "company_id": company_id,
            "expected_source_count": 0,
            "completed_source_count": 0,
            "failed_source_count": 0,
            "pending_source_count": 0,
            "risk_assessment_id": None,
            "summary_id": None,
        }
    pending = max(
        0,
        run.source_count - run.completed_source_count - run.failed_source_count,
    )
    ready = bool(
        run.status == "succeeded"
        and run.public_ready
        and run.risk_assessment_id
        and run.summary_id
    )
    status = (
        "READY"
        if ready
        else "UNSUPPORTED"
        if run.status == "succeeded"
        else "ERROR"
        if run.status == "failed"
        else "RETRY_SCHEDULED"
        if run.status == "retry_scheduled"
        else "ENRICHING"
    )
    return {
        "status": status,
        "ready": ready,
        "company_id": company_id,
        "enrichment_run_id": str(run.id),
        "expected_source_count": run.source_count,
        "completed_source_count": run.completed_source_count,
        "failed_source_count": run.failed_source_count,
        "pending_source_count": pending,
        "risk_assessment_id": run.risk_assessment_id,
        "summary_id": run.summary_id,
        "updated_at": run.updated_at,
    }


def _semantic_coverage(
    statuses: Sequence[str], *, frozen_expected: int, run_status: str
) -> tuple[int, int, bool, float]:
    """Summarize one frozen denominator without turning errors into negatives."""

    normalized = [str(status).upper() for status in statuses]
    # SUCCEEDED is a bounded compatibility bridge for rows written by v1.
    resolved = sum(
        status in {"FOUND", "NOT_FOUND", "SUCCEEDED"} for status in normalized
    )
    not_applicable = sum(status == "NOT_APPLICABLE" for status in normalized)
    terminal = resolved + not_applicable
    expected = max(int(frozen_expected or 0), len(normalized))
    applicable_expected = max(0, expected - not_applicable)
    complete = terminal >= expected and (
        expected > 0 or str(run_status).lower() == "succeeded"
    )
    percent = (
        min(100.0, resolved * 100.0 / applicable_expected)
        if applicable_expected
        else 100.0 if complete else 0.0
    )
    return resolved, terminal, complete, percent


def canonical_enrichment_metrics(session: Session) -> dict[str, Any]:
    """Return canonical Master coverage metrics from each company's latest run.

    ``FOUND`` and ``NOT_FOUND`` resolve an applicable source expectation.
    ``NOT_APPLICABLE`` resolves the workflow expectation but is excluded from
    the applicable-source denominator.  Every other semantic status remains
    fail-closed.  ``succeeded`` is accepted temporarily for enrichment rows
    written before the semantic-status migration; it can be removed once no
    such rows remain in operational storage.
    """

    ranked = (
        select(
            CompanyEnrichmentRun.id.label("run_id"),
            CompanyEnrichmentRun.company_id.label("company_id"),
            CompanyEnrichmentRun.status.label("status"),
            CompanyEnrichmentRun.public_ready.label("public_ready"),
            CompanyEnrichmentRun.source_count.label("source_count"),
            CompanyEnrichmentRun.risk_assessment_id.label("risk_assessment_id"),
            CompanyEnrichmentRun.summary_id.label("summary_id"),
            func.row_number()
            .over(
                partition_by=CompanyEnrichmentRun.company_id,
                order_by=(
                    CompanyEnrichmentRun.created_at.desc(),
                    CompanyEnrichmentRun.id.desc(),
                ),
            )
            .label("position"),
        )
        .subquery()
    )
    latest = (
        select(
            ranked.c.run_id,
            ranked.c.company_id,
            ranked.c.status,
            ranked.c.public_ready,
            ranked.c.source_count,
            ranked.c.risk_assessment_id,
            ranked.c.summary_id,
        )
        .where(ranked.c.position == 1)
        .subquery()
    )
    company_total = int(
        session.scalar(select(func.count()).select_from(Company)) or 0
    )
    latest_rows = session.execute(select(latest)).mappings().all()
    source_rows = session.execute(
        select(
            CompanySourceCoverage.enrichment_run_id,
            CompanySourceCoverage.status,
        )
        .join(latest, latest.c.run_id == CompanySourceCoverage.enrichment_run_id)
    ).all()

    coverage_by_run: dict[UUID, list[str]] = defaultdict(list)
    for run_id, status in source_rows:
        coverage_by_run[run_id].append(str(status).upper())

    retry_statuses = {"RETRY_SCHEDULED"}
    failure_statuses = {
        "FAILED",
        "CANCELLED",
        "SOURCE_UNAVAILABLE",
        "TIMEOUT",
        "PARSING_ERROR",
        "STALE_DATA",
        "ACCESS_REQUIRED",
    }
    pending_statuses = {"PENDING", "QUEUED", "RUNNING"}

    companies_complete = 0
    companies_failed = 0
    companies_in_progress = 0
    risk_ready = 0
    summary_ready = 0
    public_ready = 0
    covered_at_least = {1: 0, 3: 0, 5: 0, 10: 0}
    coverage_percentages = [0.0] * max(0, company_total - len(latest_rows))
    expected = completed = retrying = failed = pending = 0

    for run in latest_rows:
        statuses = coverage_by_run.get(run["run_id"], [])
        frozen_expected = max(int(run["source_count"] or 0), len(statuses))
        semantic_resolved, terminal, is_complete, coverage_percent = (
            _semantic_coverage(
                statuses,
                frozen_expected=frozen_expected,
                run_status=str(run["status"]),
            )
        )
        coverage_percentages.append(coverage_percent)
        for threshold in covered_at_least:
            if semantic_resolved >= threshold:
                covered_at_least[threshold] += 1
        if is_complete:
            companies_complete += 1
        elif str(run["status"]).lower() in {
            "pending",
            "waiting_sources",
            "retry_scheduled",
            "running",
        }:
            companies_in_progress += 1
        if str(run["status"]).lower() == "failed":
            companies_failed += 1
        if run["risk_assessment_id"]:
            risk_ready += 1
        if run["summary_id"]:
            summary_ready += 1
        if bool(run["public_ready"]):
            public_ready += 1

        expected += frozen_expected
        completed += terminal
        retrying += sum(item in retry_statuses for item in statuses)
        failed += sum(item in failure_statuses for item in statuses)
        pending += sum(item in pending_statuses for item in statuses)

    companies_with_run = len(latest_rows)
    coverage_average = (
        sum(coverage_percentages) / company_total if company_total else 0.0
    )
    coverage_median = median(coverage_percentages) if coverage_percentages else 0.0
    return {
        "master_company_count": company_total,
        "companies_with_enrichment_run": companies_with_run,
        "companies_not_started": max(0, company_total - companies_with_run),
        "companies_complete": companies_complete,
        "companies_failed": companies_failed,
        "companies_in_progress": companies_in_progress,
        "risk_ready_companies": risk_ready,
        "summary_ready_companies": summary_ready,
        "public_ready_companies": public_ready,
        "coverage_at_least_1": covered_at_least[1],
        "coverage_at_least_3": covered_at_least[3],
        "coverage_at_least_5": covered_at_least[5],
        "coverage_at_least_10": covered_at_least[10],
        "coverage_100_percent": companies_complete,
        "average_coverage_percent": round(coverage_average, 4),
        "median_coverage_percent": round(float(coverage_median), 4),
        "source_expectation_count": expected,
        "source_expectations_succeeded": completed,
        "source_expectations_retrying": retrying,
        "source_expectations_failed": failed,
        "source_expectations_pending": pending,
        "source_completion_percent": (
            round(completed * 100 / expected, 4) if expected else None
        ),
    }


def seed_existing_master_enrichment(
    session: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Enroll Master rows which existed before durable replay signals.

    Official/original Master rows are intentionally ordered before provisional
    Firmoteka rows; current replay signals continue to provide the new-company
    fast path.  Each company is idempotent and freezes the full operational
    denominator at creation.
    """

    now = now or utc_now()
    if limit <= 0:
        return 0, 0
    existing = select(CompanyEnrichmentRun.company_id)
    company_ids = tuple(
        session.scalars(
            select(Company.id)
            .where(~Company.id.in_(existing))
            .order_by(
                Company.official_registry_verified.desc(),
                (Company.master_source == "firmoteka").asc(),
                Company.created_at,
                Company.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    run_ids: list[UUID] = []
    created = 0
    for company_id in company_ids:
        try:
            creation = create_enrichment_run(
                session,
                company_id=company_id,
                trigger="existing_master_bootstrap",
                idempotency_key=f"existing-master:{WORKFLOW_VERSION}:{company_id}",
                now=now,
            )
        except ValueError as error:
            if "no applicable operational sources" not in str(error):
                raise
            continue
        run_ids.append(creation.run.id)
        created += int(creation.created)
    return created, enqueue_enrichment_work(session, run_ids, now=now)


def run_company_enrichment_cycle(
    session: Session,
    *,
    signal_limit: int = 100,
    reconcile_limit: int = 100,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Standalone scheduler/worker callable; caller owns the transaction."""

    now = now or utc_now()
    consumption = consume_master_replay_signals(session, limit=signal_limit, now=now)
    bootstrap_runs, bootstrap_jobs = seed_existing_master_enrichment(
        session, limit=signal_limit, now=now
    )
    run_ids = tuple(
        session.scalars(
            select(CompanyEnrichmentRun.id)
            .where(
                CompanyEnrichmentRun.status.in_(
                    ("pending", "waiting_sources", "retry_scheduled", "running")
                )
            )
            .order_by(CompanyEnrichmentRun.updated_at, CompanyEnrichmentRun.id)
            .limit(reconcile_limit)
            .with_for_update(skip_locked=True)
        )
    )
    statuses: dict[str, int] = defaultdict(int)
    for run_id in run_ids:
        run = reconcile_enrichment_run(session, run_id, now=now)
        statuses[run.status] += 1
    return {
        "signals_seen": consumption.signals_seen,
        "signals_scheduled": consumption.signals_scheduled,
        "runs_created": consumption.runs_created,
        "jobs_created": consumption.jobs_created,
        "bootstrap_runs_created": bootstrap_runs,
        "bootstrap_jobs_created": bootstrap_jobs,
        "runs_reconciled": len(run_ids),
        "run_statuses": dict(sorted(statuses.items())),
    }


def run_company_enrichment_cycle_once(
    *,
    signal_limit: int = 100,
    reconcile_limit: int = 100,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Commit one bounded cycle for the persistent Worker supervisor."""

    with SessionLocal() as session:
        result = run_company_enrichment_cycle(
            session,
            signal_limit=signal_limit,
            reconcile_limit=reconcile_limit,
            now=now,
        )
        session.commit()
        return result
