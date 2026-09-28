"""Controlled state repair for the DATA-SCALE post-migration checkpoint.

No function in this module executes a source handler or publishes a card.
Callers own transaction boundaries so recovery can commit bounded batches.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, aliased

from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichmentRun, CompanySourceCoverage
from app.models.registry_master import MasterReplaySignal
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from app.models.source import DataSet
from app.models.worker import WorkerJob, WorkerLease, WorkerRun
from app.services.replay_readiness_service import (
    ACTIONABLE_REPLAY_STATUSES,
    operational_replay_predicates,
    signal_is_resolved_by_run_clause,
    unresolved_replay_exists_clause,
)
from app.services.source_applicability_service import (
    SourceApplicability,
    source_applicability_expression,
)
from app.worker.errors import WorkerTimeoutError
from app.worker.execution import RetryPolicy, recover_stale_runs


ACTIVE_ENRICHMENT_STATUSES = (
    "pending",
    "waiting_sources",
    "retry_scheduled",
    "running",
)
TERMINAL_COVERAGE_STATUSES = ("FOUND", "NOT_FOUND", "NOT_APPLICABLE")
FAILED_COVERAGE_STATUSES = (
    "SOURCE_UNAVAILABLE",
    "TIMEOUT",
    "PARSING_ERROR",
    "STALE_DATA",
    "ACCESS_REQUIRED",
)
ACTIVE_COVERAGE_EXECUTION_STATUSES = (
    "pending",
    "queued",
    "running",
    "retry_scheduled",
)
RecoveryAction = Literal[
    "NO_CHANGE",
    "RECONCILE_TO_COMPLETE",
    "INVALIDATE_AND_PARK",
    "BLOCKED_APPLICABILITY",
    "BLOCKED_FAILED_REPLAY",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, (UUID, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class StaleWorkerPlanItem:
    run_id: UUID
    job_id: UUID
    source_id: str
    lease_owner: str | None
    heartbeat_at: datetime
    age_seconds: int
    attempt_no: int
    max_attempts: int
    retry_eligible: bool
    recoverable: bool
    expected_run_status: str
    expected_job_status: str

    def as_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class EnrichmentRecoveryPlanItem:
    run_id: UUID
    company_id: int
    status: str
    stage: str
    public_ready: bool
    source_count: int
    coverage_terminal_count: int
    coverage_active_count: int
    coverage_failed_count: int
    applicability_blocker_count: int
    pending_replay_blockers: int
    failed_replay_blockers: int
    risk_assessment_id: str | None
    summary_id: str | None
    current_conclusion_refs: bool
    action: RecoveryAction
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class EnrichmentRecoveryBatch:
    items: tuple[EnrichmentRecoveryPlanItem, ...]
    scanned: int
    changed: int
    last_company_id: int | None
    reasons: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [item.as_dict() for item in self.items],
            "scanned": self.scanned,
            "changed": self.changed,
            "last_company_id": self.last_company_id,
            "reasons": dict(self.reasons),
        }


def plan_stale_worker_recovery(
    session: Session,
    *,
    stale_after: timedelta,
    retry_policy: RetryPolicy,
    now: datetime | None = None,
    limit: int = 100,
) -> tuple[StaleWorkerPlanItem, ...]:
    """Describe canonical timeout recovery without mutating worker state."""

    observed_at = now or utc_now()
    if stale_after <= timedelta(0) or limit <= 0:
        raise ValueError("stale_after and limit must be positive")
    rows = tuple(
        session.execute(
            select(WorkerRun, WorkerJob, WorkerLease)
            .join(WorkerJob, WorkerJob.id == WorkerRun.job_id)
            .outerjoin(WorkerLease, WorkerLease.source_id == WorkerJob.source_id)
            .where(
                WorkerRun.status == "running",
                WorkerRun.heartbeat_at <= observed_at - stale_after,
            )
            .order_by(WorkerRun.heartbeat_at, WorkerRun.id)
            .limit(limit)
        )
    )
    result: list[StaleWorkerPlanItem] = []
    timeout = WorkerTimeoutError("stale worker run recovered")
    for run, job, lease in rows:
        retry_eligible = retry_policy.allows(
            timeout,
            attempt_no=run.attempt_no,
            max_attempts=job.max_attempts,
        )
        deadline_exceeded = (
            run.started_at + timedelta(seconds=job.timeout_seconds) <= observed_at
        )
        lease_expired = lease is None or lease.expires_at <= observed_at
        recoverable = deadline_exceeded or lease_expired
        result.append(
            StaleWorkerPlanItem(
                run_id=run.id,
                job_id=job.id,
                source_id=job.source_id,
                lease_owner=lease.owner_worker_id if lease else None,
                heartbeat_at=run.heartbeat_at,
                age_seconds=max(
                    0, int((observed_at - run.heartbeat_at).total_seconds())
                ),
                attempt_no=run.attempt_no,
                max_attempts=job.max_attempts,
                retry_eligible=retry_eligible,
                recoverable=recoverable,
                expected_run_status="timed_out" if recoverable else "running",
                expected_job_status=(
                    "retry_scheduled"
                    if recoverable and retry_eligible
                    else "failed" if recoverable else job.status
                ),
            )
        )
    return tuple(result)


def apply_stale_worker_recovery(
    session: Session,
    *,
    stale_after: timedelta,
    retry_policy: RetryPolicy,
    now: datetime | None = None,
) -> tuple[UUID, ...]:
    """Delegate mutation to the canonical Worker Foundation lifecycle."""

    return recover_stale_runs(
        session,
        stale_after=stale_after,
        retry_policy=retry_policy,
        now=now,
    )


def _latest_candidate_runs(
    session: Session,
    *,
    after_company_id: int,
    limit: int,
    lock: bool,
) -> tuple[CompanyEnrichmentRun, ...]:
    ranked = (
        select(
            CompanyEnrichmentRun.id.label("run_id"),
            CompanyEnrichmentRun.company_id.label("company_id"),
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
    ids = tuple(
        session.scalars(
            select(ranked.c.run_id)
            .join(CompanyEnrichmentRun, CompanyEnrichmentRun.id == ranked.c.run_id)
            .where(
                ranked.c.position == 1,
                ranked.c.company_id > after_company_id,
                (
                    CompanyEnrichmentRun.status.in_(ACTIVE_ENRICHMENT_STATUSES)
                    | CompanyEnrichmentRun.public_ready.is_(True)
                ),
            )
            .order_by(ranked.c.company_id, ranked.c.run_id)
            .limit(limit)
        )
    )
    if not ids:
        return ()
    order = case(
        *((CompanyEnrichmentRun.id == value, index) for index, value in enumerate(ids)),
        else_=len(ids),
    )
    statement = (
        select(CompanyEnrichmentRun)
        .where(CompanyEnrichmentRun.id.in_(ids))
        .order_by(order)
    )
    if lock:
        statement = statement.with_for_update(skip_locked=True)
    return tuple(session.scalars(statement))


def _coverage_counts(
    session: Session, run_ids: tuple[UUID, ...]
) -> dict[UUID, Counter[str]]:
    result: dict[UUID, Counter[str]] = defaultdict(Counter)
    if not run_ids:
        return result
    rows = session.execute(
        select(
            CompanySourceCoverage.enrichment_run_id,
            CompanySourceCoverage.status,
            CompanySourceCoverage.execution_status,
            func.count(CompanySourceCoverage.id),
        )
        .where(CompanySourceCoverage.enrichment_run_id.in_(run_ids))
        .group_by(
            CompanySourceCoverage.enrichment_run_id,
            CompanySourceCoverage.status,
            CompanySourceCoverage.execution_status,
        )
    )
    for run_id, status, execution_status, count in rows:
        amount = int(count)
        result[run_id]["terminal"] += amount * int(
            status in TERMINAL_COVERAGE_STATUSES
            and execution_status == "succeeded"
        )
        result[run_id]["active"] += amount * int(
            execution_status in ACTIVE_COVERAGE_EXECUTION_STATUSES
        )
        result[run_id]["failed"] += amount * int(
            status in FAILED_COVERAGE_STATUSES
            or execution_status in ("failed", "cancelled")
        )
        result[run_id]["applicability"] += amount * int(
            status == "APPLICABILITY_UNKNOWN"
        )
    return result


def _replay_counts(
    session: Session, company_ids: tuple[int, ...]
) -> dict[int, Counter[str]]:
    result: dict[int, Counter[str]] = defaultdict(Counter)
    if not company_ids:
        return result
    latest = aliased(CompanyEnrichmentRun)
    latest_run_id = (
        select(latest.id)
        .where(latest.company_id == MasterReplaySignal.company_id)
        .order_by(latest.created_at.desc(), latest.id.desc())
        .limit(1)
        .correlate(MasterReplaySignal)
        .scalar_subquery()
    )
    decision = source_applicability_expression(
        DataSet.applicability, Company.entity_type, Company.inn
    )
    rows = session.execute(
        select(
            MasterReplaySignal.company_id,
            MasterReplaySignal.status,
            func.count(MasterReplaySignal.id),
        )
        .join(DataSet, DataSet.code == MasterReplaySignal.target_source_id)
        .join(Company, Company.id == MasterReplaySignal.company_id)
        .where(
            MasterReplaySignal.company_id.in_(company_ids),
            MasterReplaySignal.status.in_(("pending", "scheduled", "failed")),
            *operational_replay_predicates(DataSet, now=utc_now()),
            decision.in_(
                (
                    SourceApplicability.APPLICABLE.value,
                    SourceApplicability.UNKNOWN.value,
                )
            ),
            ~signal_is_resolved_by_run_clause(
                MasterReplaySignal, latest_run_id
            ),
        )
        .group_by(MasterReplaySignal.company_id, MasterReplaySignal.status)
    )
    for company_id, status, count in rows:
        result[int(company_id)][str(status)] = int(count)
    return result


def _current_conclusion_refs(
    session: Session, company_ids: tuple[int, ...]
) -> dict[int, tuple[str | None, str | None, str | None]]:
    if not company_ids:
        return {}
    risks = (
        select(
            CompanyRiskAssessmentV3.company_id,
            CompanyRiskAssessmentV3.assessment_id,
            func.row_number()
            .over(
                partition_by=CompanyRiskAssessmentV3.company_id,
                order_by=(
                    CompanyRiskAssessmentV3.calculated_at.desc(),
                    CompanyRiskAssessmentV3.id.desc(),
                ),
            )
            .label("position"),
        )
        .where(CompanyRiskAssessmentV3.company_id.in_(company_ids))
        .subquery()
    )
    summaries = (
        select(
            CompanySummaryV3.company_id,
            CompanySummaryV3.summary_id,
            CompanySummaryV3.risk_assessment_id,
            func.row_number()
            .over(
                partition_by=CompanySummaryV3.company_id,
                order_by=(
                    CompanySummaryV3.generated_at.desc(),
                    CompanySummaryV3.id.desc(),
                ),
            )
            .label("position"),
        )
        .where(CompanySummaryV3.company_id.in_(company_ids))
        .subquery()
    )
    risk_map = {
        int(company_id): assessment_id
        for company_id, assessment_id in session.execute(
            select(risks.c.company_id, risks.c.assessment_id).where(
                risks.c.position == 1
            )
        )
    }
    summary_map = {
        int(company_id): (summary_id, risk_id)
        for company_id, summary_id, risk_id in session.execute(
            select(
                summaries.c.company_id,
                summaries.c.summary_id,
                summaries.c.risk_assessment_id,
            ).where(summaries.c.position == 1)
        )
    }
    return {
        company_id: (
            risk_map.get(company_id),
            summary_map.get(company_id, (None, None))[0],
            summary_map.get(company_id, (None, None))[1],
        )
        for company_id in company_ids
    }


def plan_enrichment_recovery_batch(
    session: Session,
    *,
    after_company_id: int = 0,
    limit: int = 100,
    lock: bool = False,
) -> EnrichmentRecoveryBatch:
    """Plan latest-run repair in stable bounded company order."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    runs = _latest_candidate_runs(
        session,
        after_company_id=after_company_id,
        limit=limit,
        lock=lock,
    )
    run_ids = tuple(run.id for run in runs)
    company_ids = tuple(run.company_id for run in runs)
    coverage = _coverage_counts(session, run_ids)
    replay = _replay_counts(session, company_ids)
    refs = _current_conclusion_refs(session, company_ids)
    items: list[EnrichmentRecoveryPlanItem] = []
    for run in runs:
        counts = coverage[run.id]
        replay_counts = replay[run.company_id]
        pending = replay_counts["pending"] + replay_counts["scheduled"]
        failed = replay_counts["failed"]
        risk_ref, summary_ref, summary_risk_ref = refs.get(
            run.company_id, (None, None, None)
        )
        current_refs = bool(
            run.risk_assessment_id
            and run.summary_id
            and run.risk_assessment_id == risk_ref == summary_risk_ref
            and run.summary_id == summary_ref
        )
        if failed:
            action: RecoveryAction = "BLOCKED_FAILED_REPLAY"
            reason = "failed replay lacks exact successful terminal coverage"
        elif counts["applicability"]:
            action = "BLOCKED_APPLICABILITY"
            reason = "source applicability remains unknown"
        elif pending:
            action = "INVALIDATE_AND_PARK"
            reason = "pending or scheduled replay remains unresolved"
        elif (
            counts["terminal"] == run.source_count
            and counts["active"] == 0
            and counts["failed"] == 0
            and current_refs
        ):
            if (
                run.status == "succeeded"
                and run.stage == "complete"
                and run.public_ready
            ):
                action = "NO_CHANGE"
                reason = "current terminal conclusion remains safe"
            else:
                action = "RECONCILE_TO_COMPLETE"
                reason = "terminal denominator and current Risk/Summary are proven"
        else:
            action = "INVALIDATE_AND_PARK"
            reason = "no safe current conclusion can be proven"
        items.append(
            EnrichmentRecoveryPlanItem(
                run_id=run.id,
                company_id=run.company_id,
                status=run.status,
                stage=run.stage,
                public_ready=run.public_ready,
                source_count=run.source_count,
                coverage_terminal_count=counts["terminal"],
                coverage_active_count=counts["active"],
                coverage_failed_count=counts["failed"],
                applicability_blocker_count=counts["applicability"],
                pending_replay_blockers=pending,
                failed_replay_blockers=failed,
                risk_assessment_id=run.risk_assessment_id,
                summary_id=run.summary_id,
                current_conclusion_refs=current_refs,
                action=action,
                reason=reason,
            )
        )
    return EnrichmentRecoveryBatch(
        items=tuple(items),
        scanned=len(items),
        changed=0,
        last_company_id=max(company_ids) if company_ids else None,
        reasons=dict(Counter(item.action for item in items)),
    )


def apply_enrichment_recovery_batch(
    session: Session,
    *,
    after_company_id: int = 0,
    limit: int = 100,
    now: datetime | None = None,
) -> EnrichmentRecoveryBatch:
    """Apply state-only reconciliation; never enqueue or execute source work."""

    observed_at = now or utc_now()
    plan = plan_enrichment_recovery_batch(
        session,
        after_company_id=after_company_id,
        limit=limit,
        lock=True,
    )
    rows = {
        run.id: run
        for run in session.scalars(
            select(CompanyEnrichmentRun)
            .where(CompanyEnrichmentRun.id.in_(item.run_id for item in plan.items))
            .with_for_update()
        )
    } if plan.items else {}
    changed = 0
    for item in plan.items:
        if item.action == "NO_CHANGE":
            continue
        run = rows.get(item.run_id)
        if run is None:
            continue
        if item.action == "RECONCILE_TO_COMPLETE":
            run.status = "succeeded"
            run.stage = "complete"
            run.public_ready = True
            run.finished_at = run.finished_at or observed_at
            run.last_error_code = None
            run.last_error = None
        else:
            run.status = "cancelled"
            run.stage = "failed"
            run.public_ready = False
            run.risk_assessment_id = None
            run.summary_id = None
            run.finished_at = observed_at
            if item.action == "BLOCKED_FAILED_REPLAY":
                run.last_error_code = "failed_replay_blocker"
            elif item.action == "BLOCKED_APPLICABILITY":
                run.last_error_code = "applicability_unknown"
            else:
                run.last_error_code = "post_migration_recovery_parked"
            run.last_error = item.reason
        run.updated_at = observed_at
        changed += 1
    session.flush()
    return EnrichmentRecoveryBatch(
        items=plan.items,
        scanned=plan.scanned,
        changed=changed,
        last_company_id=plan.last_company_id,
        reasons=plan.reasons,
    )


def reconcile_not_applicable_replay_batch(
    session: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, int]:
    """Represent exact NOT_APPLICABLE replay UUIDs without creating jobs."""

    observed_at = now or utc_now()
    if limit <= 0:
        raise ValueError("limit must be positive")
    latest = aliased(CompanyEnrichmentRun)
    latest_run_id = (
        select(latest.id)
        .where(latest.company_id == MasterReplaySignal.company_id)
        .order_by(latest.created_at.desc(), latest.id.desc())
        .limit(1)
        .correlate(MasterReplaySignal)
        .scalar_subquery()
    )
    decision = source_applicability_expression(
        DataSet.applicability, Company.entity_type, Company.inn
    )
    signals = tuple(
        session.scalars(
            select(MasterReplaySignal)
            .join(DataSet, DataSet.code == MasterReplaySignal.target_source_id)
            .join(Company, Company.id == MasterReplaySignal.company_id)
            .where(
                MasterReplaySignal.status.in_(("pending", "scheduled", "failed")),
                latest_run_id.is_not(None),
                decision == SourceApplicability.NOT_APPLICABLE.value,
                ~signal_is_resolved_by_run_clause(
                    MasterReplaySignal, latest_run_id
                ),
            )
            .order_by(MasterReplaySignal.created_at, MasterReplaySignal.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    if not signals:
        return {"signals_reconciled": 0, "signals_terminalized": 0, "coverage_created": 0}
    company_ids = tuple(dict.fromkeys(signal.company_id for signal in signals))
    latest_rows = tuple(
        session.scalars(
            select(CompanyEnrichmentRun)
            .where(CompanyEnrichmentRun.company_id.in_(company_ids))
            .order_by(
                CompanyEnrichmentRun.company_id,
                CompanyEnrichmentRun.created_at.desc(),
                CompanyEnrichmentRun.id.desc(),
            )
        )
    )
    runs: dict[int, CompanyEnrichmentRun] = {}
    for run in latest_rows:
        runs.setdefault(run.company_id, run)
    dataset_by_code = {
        row.code: row
        for row in session.scalars(
            select(DataSet).where(
                DataSet.code.in_(signal.target_source_id for signal in signals)
            )
        )
    }
    grouped: dict[tuple[UUID, str], list[MasterReplaySignal]] = defaultdict(list)
    for signal in signals:
        run = runs.get(signal.company_id)
        if run is not None:
            grouped[(run.id, signal.target_source_id)].append(signal)
    coverage_created = terminalized = reconciled = 0
    touched_run_ids: set[UUID] = set()
    for (run_id, source_id), source_signals in grouped.items():
        run = session.get(CompanyEnrichmentRun, run_id)
        dataset = dataset_by_code[source_id]
        coverage = session.scalar(
            select(CompanySourceCoverage)
            .where(
                CompanySourceCoverage.enrichment_run_id == run_id,
                CompanySourceCoverage.source_id == source_id,
            )
            .with_for_update()
        )
        if coverage is None:
            coverage = CompanySourceCoverage(
                enrichment_run_id=run_id,
                company_id=run.company_id,
                dataset_id=dataset.id,
                source_id=source_id,
                worker_source_id=source_id,
                mode="point_check",
                status="NOT_APPLICABLE",
                execution_status="succeeded",
                source_snapshot={},
                handler_version="post-migration-applicability-v1",
                master_replay_signal_ids=[],
                attempt_count=0,
                max_attempts=1,
                fact_count=0,
                created_at=observed_at,
                updated_at=observed_at,
            )
            session.add(coverage)
            coverage_created += 1
        existing_ids = list(coverage.master_replay_signal_ids or ())
        coverage.master_replay_signal_ids = list(
            dict.fromkeys(
                existing_ids + [str(signal.id) for signal in source_signals]
            )
        )
        coverage.dataset_id = dataset.id
        coverage.status = "NOT_APPLICABLE"
        coverage.execution_status = "succeeded"
        coverage.worker_job_id = None
        coverage.worker_run_id = None
        coverage.checked_at = observed_at
        coverage.finished_at = observed_at
        coverage.last_error = None
        coverage.source_snapshot = {
            **dict(coverage.source_snapshot or {}),
            "applicability": SourceApplicability.NOT_APPLICABLE.value,
            "applicability_policy": dataset.applicability,
            "recovery": "post_migration_applicability_v1",
            "failed_signal_ids_preserved": [
                str(signal.id)
                for signal in source_signals
                if signal.status == "failed"
            ],
        }
        coverage.updated_at = observed_at
        touched_run_ids.add(run_id)
        pending_ids = [
            signal.id
            for signal in source_signals
            if signal.status in ACTIONABLE_REPLAY_STATUSES
        ]
        if pending_ids:
            session.execute(
                update(MasterReplaySignal)
                .where(
                    MasterReplaySignal.id.in_(pending_ids),
                    MasterReplaySignal.status.in_(ACTIONABLE_REPLAY_STATUSES),
                )
                .values(status="complete", completed_at=observed_at)
            )
        terminalized += len(pending_ids)
        reconciled += len(source_signals)
    session.flush()
    for run_id in sorted(touched_run_ids, key=str):
        run = session.get(CompanyEnrichmentRun, run_id)
        coverage_rows = tuple(
            session.scalars(
                select(CompanySourceCoverage)
                .where(CompanySourceCoverage.enrichment_run_id == run_id)
                .order_by(CompanySourceCoverage.source_id)
            )
        )
        run.source_count = len(coverage_rows)
        run.completed_source_count = sum(
            row.status in TERMINAL_COVERAGE_STATUSES for row in coverage_rows
        )
        run.failed_source_count = sum(
            row.status in FAILED_COVERAGE_STATUSES for row in coverage_rows
        )
        run.applicable_sources = [
            dict(row.source_snapshot or {}) for row in coverage_rows
        ]
        run.updated_at = observed_at
    session.flush()
    return {
        "signals_reconciled": reconciled,
        "signals_terminalized": terminalized,
        "coverage_created": coverage_created,
    }


def recovery_invariants(session: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Read the required post-recovery zero invariants."""

    observed_at = now or utc_now()
    ranked = (
        select(
            CompanyEnrichmentRun.id.label("run_id"),
            CompanyEnrichmentRun.company_id.label("company_id"),
            CompanyEnrichmentRun.status.label("status"),
            CompanyEnrichmentRun.stage.label("stage"),
            CompanyEnrichmentRun.public_ready.label("public_ready"),
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
    latest = select(ranked).where(ranked.c.position == 1).subquery()

    def latest_count(*predicates) -> int:
        return int(
            session.scalar(select(func.count()).select_from(latest).where(*predicates))
            or 0
        )

    return {
        "public_ready_not_succeeded": latest_count(
            latest.c.public_ready.is_(True), latest.c.status != "succeeded"
        ),
        "public_ready_not_complete": latest_count(
            latest.c.public_ready.is_(True), latest.c.stage != "complete"
        ),
        "public_ready_with_unresolved_replay": latest_count(
            latest.c.public_ready.is_(True),
            unresolved_replay_exists_clause(
                latest.c.company_id,
                latest.c.run_id,
                now=observed_at,
                statuses=ACTIONABLE_REPLAY_STATUSES,
            ),
        ),
        "public_ready_with_failed_replay": latest_count(
            latest.c.public_ready.is_(True),
            unresolved_replay_exists_clause(
                latest.c.company_id,
                latest.c.run_id,
                now=observed_at,
                statuses=("failed",),
            ),
        ),
        "active_enrichment_runs": int(
            session.scalar(
                select(func.count(CompanyEnrichmentRun.id)).where(
                    CompanyEnrichmentRun.status.in_(ACTIVE_ENRICHMENT_STATUSES)
                )
            )
            or 0
        ),
        "running_worker_jobs": int(
            session.scalar(
                select(func.count(WorkerJob.id)).where(WorkerJob.status == "running")
            )
            or 0
        ),
        "running_worker_runs": int(
            session.scalar(
                select(func.count(WorkerRun.id)).where(WorkerRun.status == "running")
            )
            or 0
        ),
    }


def replay_blocker_counts(
    session: Session, *, now: datetime | None = None
) -> dict[str, int]:
    """Count exact unresolved signals without treating failed as actionable."""

    observed_at = now or utc_now()
    latest = aliased(CompanyEnrichmentRun)
    latest_run_id = (
        select(latest.id)
        .where(latest.company_id == MasterReplaySignal.company_id)
        .order_by(latest.created_at.desc(), latest.id.desc())
        .limit(1)
        .correlate(MasterReplaySignal)
        .scalar_subquery()
    )
    decision = source_applicability_expression(
        DataSet.applicability, Company.entity_type, Company.inn
    )
    rows = dict(
        session.execute(
            select(MasterReplaySignal.status, func.count(MasterReplaySignal.id))
            .join(DataSet, DataSet.code == MasterReplaySignal.target_source_id)
            .join(Company, Company.id == MasterReplaySignal.company_id)
            .where(
                MasterReplaySignal.status.in_(("pending", "scheduled", "failed")),
                *operational_replay_predicates(DataSet, now=observed_at),
                decision.in_(
                    (
                        SourceApplicability.APPLICABLE.value,
                        SourceApplicability.UNKNOWN.value,
                    )
                ),
                ~signal_is_resolved_by_run_clause(
                    MasterReplaySignal, latest_run_id
                ),
            )
            .group_by(MasterReplaySignal.status)
        ).all()
    )
    pending = int(rows.get("pending", 0))
    scheduled = int(rows.get("scheduled", 0))
    failed = int(rows.get("failed", 0))
    return {
        "pending": pending,
        "scheduled": scheduled,
        "actionable": pending + scheduled,
        "failed_blocking": failed,
        "readiness_blocking": pending + scheduled + failed,
    }


def public_ready_count(session: Session) -> int:
    latest = (
        select(
            CompanyEnrichmentRun.public_ready.label("public_ready"),
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
    return int(
        session.scalar(
            select(func.count()).select_from(latest).where(
                latest.c.position == 1, latest.c.public_ready.is_(True)
            )
        ) or 0
    )


def evidence_counts(session: Session) -> dict[str, int]:
    """Counts used to prove that recovery preserves historical evidence."""

    from app.models.company_fact import CompanyPublicFact
    from app.models.firmoteka import (
        FirmotekaCompanySnapshot,
        FirmotekaCrawlItem,
        FirmotekaRawArtifact,
    )
    from app.models.registry_master import CompanyRegistryChange

    models = {
        "companies": Company,
        "company_public_facts": CompanyPublicFact,
        "company_registry_changes": CompanyRegistryChange,
        "company_source_coverage": CompanySourceCoverage,
        "master_replay_signals": MasterReplaySignal,
        "risk_rows": CompanyRiskAssessmentV3,
        "summary_rows": CompanySummaryV3,
        "firmoteka_crawl_items": FirmotekaCrawlItem,
        "firmoteka_raw_artifacts": FirmotekaRawArtifact,
        "firmoteka_company_snapshots": FirmotekaCompanySnapshot,
    }
    return {
        name: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for name, model in models.items()
    }
