"""Transactional job, lease, execution, retry and publication foundation."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import multiprocessing
from threading import Event, Thread
import time
from typing import Any
from uuid import UUID

from sqlalchemy import Sequence, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.worker import (
    WorkerHandlerRegistration,
    WorkerJob,
    WorkerLease,
    WorkerPublicationState,
    WorkerRawManifest,
    WorkerRun,
)
from app.models.registry_master import MasterReplaySignal
from app.worker.contracts import (
    ExecutionCounters,
    HandlerContext,
    HandlerResult,
    StagingResult,
)
from app.worker.errors import (
    AccessRequiredError,
    HandlerNotRegisteredError,
    IdempotencyConflictError,
    InvalidDataError,
    LegalBlockError,
    LeaseConflictError,
    LeaseLostError,
    PublicationValidationError,
    SchemaMismatchError,
    TemporaryInfrastructureError,
    WorkerFoundationError,
    WorkerNetworkError,
    WorkerTimeoutError,
)
from app.worker.registry import HandlerRegistry, RegisteredHandler


WORKER_FENCING_SEQUENCE = Sequence("worker_lease_fencing_token_seq")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class JobCreation:
    job: WorkerJob
    created: bool


@dataclass(frozen=True)
class ClaimedExecution:
    job_id: UUID
    run_id: UUID
    source_id: str
    worker_id: str
    handler_version: str
    fencing_token: int
    attempt_no: int
    timeout_seconds: int
    schedule_metadata: dict[str, Any]
    handler: RegisteredHandler


@dataclass(frozen=True)
class RunObservation:
    run_id: UUID
    status: str
    current_stage: str
    heartbeat_at: datetime
    duration_ms: int | None
    stale: bool
    errors: tuple[dict[str, Any], ...]
    counters: ExecutionCounters


class RetryPolicy:
    """Retry only the three transient failure classes approved for V1."""

    _retryable_kinds = {
        "timeout",
        "network_failure",
        "temporary_infrastructure",
    }

    def __init__(self, *, base_delay_seconds: int = 30, max_delay_seconds: int = 900):
        if base_delay_seconds <= 0 or max_delay_seconds < base_delay_seconds:
            raise ValueError("invalid retry delay bounds")
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds

    def allows(self, error: WorkerFoundationError, *, attempt_no: int, max_attempts: int) -> bool:
        return (
            error.retryable
            and error.kind.value in self._retryable_kinds
            and attempt_no < max_attempts
        )

    def delay(self, attempt_no: int) -> timedelta:
        seconds = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** max(0, attempt_no - 1)),
        )
        return timedelta(seconds=seconds)


def create_job(
    session: Session,
    *,
    source_id: str,
    job_type: str,
    handler_version: str,
    idempotency_key: str,
    schedule_metadata: dict[str, Any] | None = None,
    max_attempts: int = 3,
    timeout_seconds: int = 300,
    now: datetime | None = None,
    master_replay_signal_ids: Sequence[UUID] | None = None,
    master_replay_target_source_id: str | None = None,
) -> JobCreation:
    """Create a durable job or return the equivalent idempotent request."""

    now = now or utc_now()
    required = {
        "source_id": source_id,
        "job_type": job_type,
        "handler_version": handler_version,
        "idempotency_key": idempotency_key,
    }
    missing = tuple(name for name, value in required.items() if not value.strip())
    if missing:
        raise ValueError(f"required worker job fields are empty: {', '.join(missing)}")
    if max_attempts <= 0 or timeout_seconds <= 0:
        raise ValueError("max_attempts and timeout_seconds must be positive")
    metadata = dict(schedule_metadata or {})
    replay_target_source_id = master_replay_target_source_id or source_id
    if master_replay_signal_ids is None:
        replay_ids = tuple(session.scalars(
            select(MasterReplaySignal.id)
            .where(
                MasterReplaySignal.target_source_id == replay_target_source_id,
                MasterReplaySignal.status == "pending",
            )
            .order_by(MasterReplaySignal.created_at, MasterReplaySignal.id)
        ))
    else:
        requested_replay_ids = tuple(dict.fromkeys(master_replay_signal_ids))
        replay_rows = tuple(session.scalars(
            select(MasterReplaySignal)
            .where(MasterReplaySignal.id.in_(requested_replay_ids))
            .order_by(MasterReplaySignal.created_at, MasterReplaySignal.id)
        )) if requested_replay_ids else ()
        found_ids = {row.id for row in replay_rows}
        if found_ids != set(requested_replay_ids):
            raise ValueError("master replay signal does not exist")
        if any(
            row.target_source_id != replay_target_source_id
            for row in replay_rows
        ):
            raise ValueError("master replay signal belongs to another source")
        if any(row.status not in {"pending", "scheduled"} for row in replay_rows):
            raise ValueError("master replay signal is already terminal")
        replay_ids = tuple(row.id for row in replay_rows)
    if replay_ids:
        replay_token = sha256(
            "\n".join(str(value) for value in replay_ids).encode("ascii")
        ).hexdigest()[:20]
        idempotency_key = f"{idempotency_key}:master-replay:{replay_token}"
        metadata["master_replay_signal_ids"] = [str(value) for value in replay_ids]
        metadata["master_replay_target_source_id"] = replay_target_source_id
    if len(idempotency_key) > 255:
        digest = sha256(idempotency_key.encode("utf-8")).hexdigest()
        idempotency_key = f"{source_id}:job:{digest}"

    existing = session.scalar(
        select(WorkerJob).where(WorkerJob.idempotency_key == idempotency_key)
    )
    if existing is not None:
        expected = (source_id, job_type, handler_version)
        actual = (existing.source_id, existing.job_type, existing.handler_version)
        if actual != expected:
            raise IdempotencyConflictError(
                "idempotency key already belongs to a different worker job"
            )
        return JobCreation(job=existing, created=False)

    job = WorkerJob(
        source_id=source_id,
        job_type=job_type,
        handler_version=handler_version,
        schedule_metadata=metadata,
        idempotency_key=idempotency_key,
        status="queued",
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError as error:
        existing = session.scalar(
            select(WorkerJob).where(WorkerJob.idempotency_key == idempotency_key)
        )
        if existing is None:
            raise
        expected = (source_id, job_type, handler_version)
        actual = (existing.source_id, existing.job_type, existing.handler_version)
        if actual != expected:
            raise IdempotencyConflictError(
                "idempotency key already belongs to a different worker job"
            ) from error
        return JobCreation(job=existing, created=False)
    if replay_ids:
        session.execute(
            update(MasterReplaySignal)
            .where(
                MasterReplaySignal.id.in_(replay_ids),
                MasterReplaySignal.target_source_id == replay_target_source_id,
                MasterReplaySignal.status == "pending",
            )
            .values(status="scheduled", scheduled_at=now, last_error=None)
        )
    return JobCreation(job=job, created=True)


def register_handler(
    session: Session,
    registry: HandlerRegistry,
    *,
    source_id: str,
    version: str,
    handler: Callable[[HandlerContext], HandlerResult],
    approved: bool,
    publisher: Callable[
        [Session, ClaimedExecution, HandlerResult], HandlerResult
    ] | None = None,
    live: bool = False,
    fixture: bool = False,
    metadata: dict[str, Any] | None = None,
) -> RegisteredHandler:
    """Register executable code and its durable approval record explicitly."""

    record = session.get(WorkerHandlerRegistration, (source_id, version))
    if record is not None and (
        not record.approved or not record.enabled or record.live_mode
    ):
        raise HandlerNotRegisteredError(
            f"durable handler approval is inactive: {source_id}@{version}"
        )
    registration = registry.register(
        source_id=source_id,
        version=version,
        handler=handler,
        publisher=publisher,
        approved=approved,
        live=live,
        fixture=fixture,
    )
    if record is None:
        record = WorkerHandlerRegistration(
            source_id=source_id,
            handler_version=version,
            approved=True,
            enabled=True,
            live_mode=False,
            metadata_json={"fixture": fixture, **(metadata or {})},
        )
        session.add(record)
    session.flush()
    return registration


def _acquire_lease(
    session: Session,
    *,
    source_id: str,
    worker_id: str,
    now: datetime,
    lease_ttl: timedelta,
) -> int:
    if lease_ttl <= timedelta(0):
        raise ValueError("lease_ttl must be positive")
    fencing_token = int(
        session.scalar(select(WORKER_FENCING_SEQUENCE.next_value()))
    )
    statement = (
        pg_insert(WorkerLease)
        .values(
            source_id=source_id,
            owner_worker_id=worker_id,
            fencing_token=fencing_token,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + lease_ttl,
        )
        .on_conflict_do_update(
            index_elements=[WorkerLease.source_id],
            set_={
                "owner_worker_id": worker_id,
                "fencing_token": fencing_token,
                "acquired_at": now,
                "heartbeat_at": now,
                "expires_at": now + lease_ttl,
            },
            where=WorkerLease.expires_at <= now,
        )
        .returning(WorkerLease.fencing_token)
    )
    claimed_token = session.scalar(statement)
    if claimed_token is None:
        raise LeaseConflictError(f"source lease is held: {source_id}")
    return int(claimed_token)


def claim_next_job(
    session: Session,
    registry: HandlerRegistry,
    *,
    worker_id: str,
    lease_ttl: timedelta,
    now: datetime | None = None,
) -> ClaimedExecution | None:
    """Claim one runnable job and source lease in the same DB transaction."""

    now = now or utc_now()
    job = session.scalar(
        select(WorkerJob)
        .where(
            WorkerJob.status.in_(("queued", "retry_scheduled")),
            or_(WorkerJob.next_attempt_at.is_(None), WorkerJob.next_attempt_at <= now),
        )
        .order_by(WorkerJob.created_at, WorkerJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return None

    handler = registry.resolve(job.source_id, job.handler_version)
    approval = session.get(
        WorkerHandlerRegistration, (job.source_id, job.handler_version)
    )
    if (
        approval is None
        or not approval.approved
        or not approval.enabled
        or approval.live_mode
    ):
        raise HandlerNotRegisteredError(
            f"durable handler approval is missing: "
            f"{job.source_id}@{job.handler_version}"
        )

    fencing_token = _acquire_lease(
        session,
        source_id=job.source_id,
        worker_id=worker_id,
        now=now,
        lease_ttl=lease_ttl,
    )
    last_attempt = session.scalar(
        select(func.max(WorkerRun.attempt_no)).where(WorkerRun.job_id == job.id)
    )
    attempt_no = int(last_attempt or 0) + 1
    run = WorkerRun(
        job_id=job.id,
        attempt_no=attempt_no,
        started_at=now,
        status="running",
        worker_id=worker_id,
        fencing_token=fencing_token,
        handler_version=job.handler_version,
        current_stage="claimed",
        errors=[],
        checksum_metadata={},
        heartbeat_at=now,
        records_seen=0,
        records_written=0,
        records_rejected=0,
        records_duplicated=0,
        records_published=0,
        retryable=False,
    )
    session.add(run)
    job.status = "running"
    job.updated_at = now
    session.flush()
    return ClaimedExecution(
        job_id=job.id,
        run_id=run.id,
        source_id=job.source_id,
        worker_id=worker_id,
        handler_version=job.handler_version,
        fencing_token=fencing_token,
        attempt_no=attempt_no,
        timeout_seconds=job.timeout_seconds,
        schedule_metadata=dict(job.schedule_metadata),
        handler=handler,
    )


def heartbeat_run(
    session: Session,
    claim: ClaimedExecution,
    *,
    lease_ttl: timedelta,
    stage: str | None = None,
    now: datetime | None = None,
) -> None:
    """Renew both lease and run heartbeat only when the fence is still valid."""

    now = now or utc_now()
    lease_result = session.execute(
        update(WorkerLease)
        .where(
            WorkerLease.source_id == claim.source_id,
            WorkerLease.owner_worker_id == claim.worker_id,
            WorkerLease.fencing_token == claim.fencing_token,
            WorkerLease.expires_at > now,
        )
        .values(heartbeat_at=now, expires_at=now + lease_ttl)
    )
    if lease_result.rowcount != 1:
        raise LeaseLostError(f"source lease was lost: {claim.source_id}")
    values: dict[str, Any] = {"heartbeat_at": now}
    if stage is not None:
        values["current_stage"] = stage
    run_result = session.execute(
        update(WorkerRun)
        .where(
            WorkerRun.id == claim.run_id,
            WorkerRun.worker_id == claim.worker_id,
            WorkerRun.fencing_token == claim.fencing_token,
            WorkerRun.status == "running",
        )
        .values(**values)
    )
    if run_result.rowcount != 1:
        raise LeaseLostError(f"worker run is no longer active: {claim.run_id}")


def update_run_counters(
    session: Session,
    claim: ClaimedExecution,
    counters: ExecutionCounters,
    *,
    lease_ttl: timedelta,
    now: datetime | None = None,
) -> None:
    """Persist in-flight progress under the current lease fencing token."""

    now = now or utc_now()
    heartbeat_run(
        session,
        claim,
        lease_ttl=lease_ttl,
        stage="handler",
        now=now,
    )
    result = session.execute(
        update(WorkerRun)
        .where(
            WorkerRun.id == claim.run_id,
            WorkerRun.status == "running",
            WorkerRun.fencing_token == claim.fencing_token,
        )
        .values(**counters.as_dict())
    )
    if result.rowcount != 1:
        raise LeaseLostError(f"worker counters rejected: {claim.run_id}")


def _assert_lease(
    session: Session, claim: ClaimedExecution, *, now: datetime
) -> WorkerLease:
    lease = session.scalar(
        select(WorkerLease)
        .where(WorkerLease.source_id == claim.source_id)
        .with_for_update()
    )
    if (
        lease is None
        or lease.owner_worker_id != claim.worker_id
        or lease.fencing_token != claim.fencing_token
        or lease.expires_at <= now
    ):
        raise LeaseLostError(f"invalid fencing token for source: {claim.source_id}")
    return lease


def _publish_staging(
    session: Session,
    claim: ClaimedExecution,
    staging: StagingResult,
    *,
    now: datetime,
) -> None:
    if not staging.validation.accepted:
        raise PublicationValidationError(
            "; ".join(staging.validation.errors)
        )
    state = session.scalar(
        select(WorkerPublicationState)
        .where(WorkerPublicationState.source_id == claim.source_id)
        .with_for_update()
    )
    validation_metadata = {
        "checksum": staging.checksum,
        "validation": staging.validation.metadata,
        "staging": staging.metadata,
    }
    if state is None:
        state = WorkerPublicationState(
            source_id=claim.source_id,
            active_pointer=staging.staging_pointer,
            rollback_pointer=None,
            generation=1,
            last_fencing_token=claim.fencing_token,
            published_by_run_id=claim.run_id,
            validation_metadata=validation_metadata,
            updated_at=now,
        )
        session.add(state)
        return
    if state.last_fencing_token > claim.fencing_token:
        raise LeaseLostError("publication rejected by a newer fencing token")
    state.rollback_pointer = state.active_pointer
    state.active_pointer = staging.staging_pointer
    state.generation += 1
    state.last_fencing_token = claim.fencing_token
    state.published_by_run_id = claim.run_id
    state.validation_metadata = validation_metadata
    state.updated_at = now


def complete_run_success(
    session: Session,
    claim: ClaimedExecution,
    result: HandlerResult,
    *,
    now: datetime | None = None,
) -> None:
    """Persist RAW metadata, publish the pointer, and finish atomically."""

    now = now or utc_now()
    _assert_lease(session, claim, now=now)
    run = session.get(WorkerRun, claim.run_id, with_for_update=True)
    job = session.get(WorkerJob, claim.job_id, with_for_update=True)
    if run is None or job is None or run.status != "running":
        raise LeaseLostError("claimed run is no longer active")
    for artifact in result.raw_artifacts:
        session.add(
            WorkerRawManifest(
                run_id=run.id,
                artifact_reference=artifact.artifact_reference,
                checksum_algorithm=artifact.checksum_algorithm,
                checksum=artifact.checksum,
                manifest=artifact.manifest,
                immutable=artifact.immutable,
            )
        )
    if result.staging_result is not None:
        run.current_stage = "publication"
        _publish_staging(session, claim, result.staging_result, now=now)
    run.status = "succeeded"
    run.current_stage = "complete"
    run.finished_at = now
    run.heartbeat_at = now
    run.duration_ms = max(0, int((now - run.started_at).total_seconds() * 1000))
    run.checksum_metadata = {
        **result.checksum_metadata,
        **(
            {"change_summary": result.change_summary.as_dict()}
            if result.change_summary is not None
            else {}
        ),
    }
    if result.counters is not None:
        for name, value in result.counters.as_dict().items():
            setattr(run, name, value)
    from app.services.source_change_service import persist_source_change_summary

    persist_source_change_summary(
        session,
        source_id=claim.source_id,
        run_id=run.id,
        result=result,
        created_at=now,
    )
    run.retryable = False
    job.status = "succeeded"
    job.next_attempt_at = None
    job.updated_at = now
    replay_ids = tuple(job.schedule_metadata.get("master_replay_signal_ids") or ())
    if replay_ids:
        replay_target_source_id = str(
            job.schedule_metadata.get("master_replay_target_source_id")
            or claim.source_id
        )
        session.execute(
            update(MasterReplaySignal)
            .where(
                MasterReplaySignal.id.in_(replay_ids),
                MasterReplaySignal.target_source_id == replay_target_source_id,
            )
            .values(status="complete", completed_at=now, last_error=None)
        )
    session.execute(
        delete(WorkerLease).where(
            WorkerLease.source_id == claim.source_id,
            WorkerLease.owner_worker_id == claim.worker_id,
            WorkerLease.fencing_token == claim.fencing_token,
        )
    )


def retry_job_now(
    session: Session,
    *,
    job_id: UUID,
    now: datetime | None = None,
) -> WorkerJob:
    """Make an already-approved automatic retry immediately claimable.

    This deliberately cannot resurrect terminal failures, successful or
    cancelled jobs, nor a source with an active lease.
    """

    now = now or utc_now()
    job = session.scalar(
        select(WorkerJob).where(WorkerJob.id == job_id).with_for_update()
    )
    if job is None:
        raise LookupError(f"worker job not found: {job_id}")
    if job.status != "retry_scheduled":
        raise ValueError("job is not retry-scheduled")
    latest_run = session.scalar(
        select(WorkerRun)
        .where(WorkerRun.job_id == job.id)
        .order_by(WorkerRun.attempt_no.desc(), WorkerRun.started_at.desc())
        .limit(1)
    )
    if latest_run is None or not latest_run.retryable:
        raise ValueError("latest worker run is not retryable")
    lease = session.get(WorkerLease, job.source_id)
    if lease is not None and lease.expires_at > now:
        raise LeaseConflictError(f"source lease is held: {job.source_id}")
    job.next_attempt_at = now
    job.updated_at = now
    return job


def complete_run_failure(
    session: Session,
    claim: ClaimedExecution,
    error: WorkerFoundationError,
    retry_policy: RetryPolicy,
    *,
    now: datetime | None = None,
) -> None:
    now = now or utc_now()
    run = session.get(WorkerRun, claim.run_id, with_for_update=True)
    job = session.get(WorkerJob, claim.job_id, with_for_update=True)
    if run is None or job is None or run.status != "running":
        return
    should_retry = retry_policy.allows(
        error, attempt_no=run.attempt_no, max_attempts=job.max_attempts
    )
    run.status = "timed_out" if isinstance(error, WorkerTimeoutError) else "failed"
    run.current_stage = "failed"
    run.finished_at = now
    run.heartbeat_at = now
    run.duration_ms = max(0, int((now - run.started_at).total_seconds() * 1000))
    run.retryable = should_retry
    run.errors = [
        *run.errors,
        {
            "kind": error.kind.value,
            "message": str(error),
            "at": now.isoformat(),
        },
    ]
    job.status = "retry_scheduled" if should_retry else "failed"
    job.next_attempt_at = (
        now + retry_policy.delay(run.attempt_no) if should_retry else None
    )
    job.updated_at = now
    replay_ids = tuple(job.schedule_metadata.get("master_replay_signal_ids") or ())
    if replay_ids and not should_retry:
        replay_target_source_id = str(
            job.schedule_metadata.get("master_replay_target_source_id")
            or claim.source_id
        )
        session.execute(
            update(MasterReplaySignal)
            .where(
                MasterReplaySignal.id.in_(replay_ids),
                MasterReplaySignal.target_source_id == replay_target_source_id,
            )
            .values(status="failed", completed_at=now, last_error=str(error)[:2000])
        )
    session.execute(
        delete(WorkerLease).where(
            WorkerLease.source_id == claim.source_id,
            WorkerLease.owner_worker_id == claim.worker_id,
            WorkerLease.fencing_token == claim.fencing_token,
        )
    )


def observe_run(
    session: Session,
    run_id: UUID,
    *,
    stale_after: timedelta,
    now: datetime | None = None,
) -> RunObservation:
    now = now or utc_now()
    run = session.get(WorkerRun, run_id)
    if run is None:
        raise LookupError(f"worker run not found: {run_id}")
    stale = run.status == "running" and run.heartbeat_at <= now - stale_after
    return RunObservation(
        run_id=run.id,
        status=run.status,
        current_stage=run.current_stage,
        heartbeat_at=run.heartbeat_at,
        duration_ms=run.duration_ms,
        stale=stale,
        errors=tuple(run.errors),
        counters=ExecutionCounters(
            records_seen=run.records_seen,
            records_written=run.records_written,
            records_rejected=run.records_rejected,
            records_duplicated=run.records_duplicated,
            records_published=run.records_published,
        ),
    )


def stale_run_ids(
    session: Session,
    *,
    stale_after: timedelta,
    now: datetime | None = None,
) -> tuple[UUID, ...]:
    now = now or utc_now()
    return tuple(
        session.scalars(
            select(WorkerRun.id).where(
                WorkerRun.status == "running",
                WorkerRun.heartbeat_at <= now - stale_after,
            )
        )
    )


def recover_stale_runs(
    session: Session,
    *,
    stale_after: timedelta,
    retry_policy: RetryPolicy,
    now: datetime | None = None,
) -> tuple[UUID, ...]:
    """Fence expired/stale runs and route timeout through the normal retry policy."""

    now = now or utc_now()
    runs = tuple(
        session.scalars(
            select(WorkerRun)
            .where(
                WorkerRun.status == "running",
                WorkerRun.heartbeat_at <= now - stale_after,
            )
            .with_for_update(skip_locked=True)
        )
    )
    recovered: list[UUID] = []
    for run in runs:
        job = session.get(WorkerJob, run.job_id)
        if job is None:
            continue
        lease = session.get(WorkerLease, job.source_id)
        deadline_exceeded = run.started_at + timedelta(seconds=job.timeout_seconds) <= now
        lease_expired = lease is None or lease.expires_at <= now
        if not (deadline_exceeded or lease_expired):
            continue
        claim = ClaimedExecution(
            job_id=job.id,
            run_id=run.id,
            source_id=job.source_id,
            worker_id=run.worker_id,
            handler_version=run.handler_version,
            fencing_token=run.fencing_token,
            attempt_no=run.attempt_no,
            timeout_seconds=job.timeout_seconds,
            schedule_metadata=dict(job.schedule_metadata),
            handler=RegisteredHandler(
                source_id=job.source_id,
                version=run.handler_version,
                handler=lambda _context: HandlerResult(),
                publisher=None,
                fixture=True,
            ),
        )
        complete_run_failure(
            session,
            claim,
            WorkerTimeoutError("stale worker run recovered"),
            retry_policy,
            now=now,
        )
        recovered.append(run.id)
    return tuple(recovered)


def rollback_publication_pointer(
    session: Session,
    *,
    source_id: str,
    expected_generation: int,
    now: datetime | None = None,
) -> WorkerPublicationState:
    """Swap active/rollback pointers with optimistic generation protection."""

    now = now or utc_now()
    state = session.scalar(
        select(WorkerPublicationState)
        .where(WorkerPublicationState.source_id == source_id)
        .with_for_update()
    )
    if state is None or state.rollback_pointer is None:
        raise LookupError(f"rollback pointer is unavailable: {source_id}")
    if state.generation != expected_generation:
        raise LeaseLostError("publication generation changed before rollback")
    state.active_pointer, state.rollback_pointer = (
        state.rollback_pointer,
        state.active_pointer,
    )
    state.generation += 1
    state.updated_at = now
    return state


def _normalize_error(error: Exception) -> WorkerFoundationError:
    if isinstance(error, WorkerFoundationError):
        return error
    if isinstance(error, TimeoutError):
        return WorkerTimeoutError(str(error))
    if isinstance(error, ConnectionError):
        return WorkerNetworkError(str(error))
    if isinstance(error, OperationalError):
        return TemporaryInfrastructureError(str(error))
    return WorkerFoundationError(str(error) or error.__class__.__name__)


def _error_from_envelope(kind: str, message: str) -> WorkerFoundationError:
    error_types: dict[str, type[WorkerFoundationError]] = {
        "access_required": AccessRequiredError,
        "timeout": WorkerTimeoutError,
        "network_failure": WorkerNetworkError,
        "temporary_infrastructure": TemporaryInfrastructureError,
        "invalid_data": InvalidDataError,
        "schema_mismatch": SchemaMismatchError,
        "legal_block": LegalBlockError,
        "handler_missing": HandlerNotRegisteredError,
        "handler_failure": WorkerFoundationError,
    }
    return error_types.get(kind, WorkerFoundationError)(message)


def _handler_child_main(
    claim: ClaimedExecution,
    deadline_at: datetime,
    connection: Any,
    shutdown_event: Any,
) -> None:
    """Execute one handler behind a process boundary and report over IPC."""

    context = HandlerContext(
        job_id=claim.job_id,
        run_id=claim.run_id,
        source_id=claim.source_id,
        worker_id=claim.worker_id,
        fencing_token=claim.fencing_token,
        deadline_at=deadline_at,
        schedule_metadata=dict(claim.schedule_metadata),
        heartbeat=lambda: connection.send(("heartbeat",)),
        report_counters=lambda counters: connection.send(("counters", counters)),
        shutdown_requested=shutdown_event.is_set,
    )
    try:
        connection.send(("started",))
        result = claim.handler.handler(context)
        if not isinstance(result, HandlerResult):
            raise InvalidDataError("handler must return HandlerResult")
        connection.send(("result", result))
    except BaseException as raw_error:
        error = (
            _normalize_error(raw_error)
            if isinstance(raw_error, Exception)
            else WorkerFoundationError(
                str(raw_error) or raw_error.__class__.__name__
            )
        )
        try:
            connection.send(("error", error.kind.value, str(error)))
        except Exception:
            pass
    finally:
        connection.close()


class WorkerExecutor:
    """Single-claim executor; deliberately not wired to a production scheduler."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        registry: HandlerRegistry,
        worker_id: str,
        lease_ttl: timedelta = timedelta(seconds=60),
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
        process_start_method: str = "spawn",
        child_cleanup_seconds: float = 1.0,
    ) -> None:
        if lease_ttl <= timedelta(0):
            raise ValueError("lease_ttl must be positive")
        if child_cleanup_seconds <= 0:
            raise ValueError("child_cleanup_seconds must be positive")
        if process_start_method not in multiprocessing.get_all_start_methods():
            raise ValueError(
                f"unsupported process start method: {process_start_method}"
            )
        self.session_factory = session_factory
        self.registry = registry
        self.worker_id = worker_id
        self.lease_ttl = lease_ttl
        self.retry_policy = retry_policy or RetryPolicy()
        self.clock = clock
        self.child_cleanup_seconds = child_cleanup_seconds
        self._process_context = multiprocessing.get_context(process_start_method)
        self._shutdown = self._process_context.Event()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown.is_set()

    def request_shutdown(self) -> None:
        """Stop accepting new jobs; an in-flight handler may finish gracefully."""

        self._shutdown.set()

    def _heartbeat(
        self,
        claim: ClaimedExecution,
        deadline_at: datetime,
        *,
        stage: str = "handler",
    ) -> None:
        now = self.clock()
        if now >= deadline_at:
            raise WorkerTimeoutError("worker execution deadline exceeded")
        with self.session_factory() as session:
            heartbeat_run(
                session,
                claim,
                lease_ttl=self.lease_ttl,
                stage=stage,
                now=now,
            )
            session.commit()

    def _start_publisher_heartbeat(
        self,
        claim: ClaimedExecution,
        deadline_at: datetime,
    ) -> tuple[Event, Thread, list[Exception]]:
        stop = Event()
        errors: list[Exception] = []
        heartbeat_period = max(
            0.001,
            min(1.0, self.lease_ttl.total_seconds() / 3),
        )

        def maintain_lease() -> None:
            while not stop.wait(heartbeat_period):
                try:
                    self._heartbeat(claim, deadline_at, stage="publication")
                except Exception as error:
                    errors.append(error)
                    stop.set()
                    return

        thread = Thread(
            target=maintain_lease,
            name=f"worker-publisher-heartbeat-{claim.run_id}",
            daemon=True,
        )
        thread.start()
        return stop, thread, errors

    def _report_counters(
        self,
        claim: ClaimedExecution,
        deadline_at: datetime,
        counters: ExecutionCounters,
    ) -> None:
        if not isinstance(counters, ExecutionCounters):
            raise InvalidDataError("handler progress must use ExecutionCounters")
        now = self.clock()
        if now >= deadline_at:
            raise WorkerTimeoutError("worker execution deadline exceeded")
        with self.session_factory() as session:
            update_run_counters(
                session,
                claim,
                counters,
                lease_ttl=self.lease_ttl,
                now=now,
            )
            session.commit()

    def _run_isolated(
        self,
        claim: ClaimedExecution,
        *,
        deadline_at: datetime,
    ) -> HandlerResult:
        parent_connection, child_connection = self._process_context.Pipe(
            duplex=False
        )
        process = self._process_context.Process(
            target=_handler_child_main,
            args=(claim, deadline_at, child_connection, self._shutdown),
            name=f"worker-handler-{claim.run_id}",
            daemon=True,
        )
        started = False
        outcome: HandlerResult | None = None
        timeout_at = time.monotonic() + claim.timeout_seconds
        heartbeat_period = max(
            0.001,
            min(1.0, self.lease_ttl.total_seconds() / 3),
        )
        next_heartbeat_at = time.monotonic() + heartbeat_period
        try:
            process.start()
            started = True
            child_connection.close()
            self._heartbeat(claim, deadline_at)

            while outcome is None:
                monotonic_now = time.monotonic()
                if monotonic_now >= timeout_at:
                    raise WorkerTimeoutError(
                        f"handler exceeded timeout_seconds={claim.timeout_seconds}"
                    )

                wait_seconds = min(
                    0.05,
                    timeout_at - monotonic_now,
                    max(0.0, next_heartbeat_at - monotonic_now),
                )
                if parent_connection.poll(wait_seconds):
                    try:
                        message = parent_connection.recv()
                    except EOFError:
                        message = None
                    if message is None:
                        if not process.is_alive():
                            raise WorkerFoundationError(
                                f"handler child exited with code {process.exitcode}"
                            )
                        continue
                    kind = message[0]
                    if kind == "started":
                        continue
                    if kind == "heartbeat":
                        self._heartbeat(claim, deadline_at)
                        next_heartbeat_at = time.monotonic() + heartbeat_period
                        continue
                    if kind == "counters":
                        self._report_counters(claim, deadline_at, message[1])
                        next_heartbeat_at = time.monotonic() + heartbeat_period
                        continue
                    if kind == "error":
                        raise _error_from_envelope(message[1], message[2])
                    if kind == "result":
                        outcome = message[1]
                        continue
                    raise WorkerFoundationError(
                        f"unknown handler child message: {kind}"
                    )

                monotonic_now = time.monotonic()
                if monotonic_now >= next_heartbeat_at:
                    self._heartbeat(claim, deadline_at)
                    next_heartbeat_at = monotonic_now + heartbeat_period
                if not process.is_alive() and not parent_connection.poll():
                    raise WorkerFoundationError(
                        f"handler child exited with code {process.exitcode}"
                    )
        finally:
            parent_connection.close()
            if not started:
                child_connection.close()
            if started:
                process.join(timeout=0.1)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=self.child_cleanup_seconds)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=self.child_cleanup_seconds)
                process.close()

        if outcome is None:
            raise WorkerFoundationError("handler child returned no result")
        return outcome

    def run_once(self) -> UUID | None:
        if self.shutdown_requested:
            return None
        with self.session_factory() as session:
            claim = claim_next_job(
                session,
                self.registry,
                worker_id=self.worker_id,
                lease_ttl=self.lease_ttl,
                now=self.clock(),
            )
            if claim is None:
                session.rollback()
                return None
            session.commit()

        deadline_at = self.clock() + timedelta(seconds=claim.timeout_seconds)
        try:
            result = self._run_isolated(claim, deadline_at=deadline_at)
            with self.session_factory() as session:
                heartbeat_stop: Event | None = None
                heartbeat_thread: Thread | None = None
                heartbeat_errors: list[Exception] = []
                try:
                    if claim.handler.publisher is not None:
                        (
                            heartbeat_stop,
                            heartbeat_thread,
                            heartbeat_errors,
                        ) = self._start_publisher_heartbeat(claim, deadline_at)
                        result = claim.handler.publisher(session, claim, result)
                        if not isinstance(result, HandlerResult):
                            raise InvalidDataError(
                                "publisher must return HandlerResult"
                            )
                        heartbeat_stop.set()
                        heartbeat_thread.join()
                        if heartbeat_errors:
                            raise heartbeat_errors[0]
                    completed_at = self.clock()
                    if completed_at >= deadline_at:
                        raise WorkerTimeoutError(
                            "worker execution deadline exceeded"
                        )
                    complete_run_success(
                        session,
                        claim,
                        result,
                        now=completed_at,
                    )
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                finally:
                    if heartbeat_stop is not None:
                        heartbeat_stop.set()
                    if heartbeat_thread is not None:
                        heartbeat_thread.join()
            return claim.run_id
        except Exception as raw_error:
            error = _normalize_error(raw_error)
            with self.session_factory() as session:
                complete_run_failure(
                    session,
                    claim,
                    error,
                    self.retry_policy,
                    now=self.clock(),
                )
                session.commit()
            raise error from raw_error
