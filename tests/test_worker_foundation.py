from datetime import date, datetime, timedelta, timezone
import multiprocessing
from threading import Thread
import time
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.models.worker import (
    WorkerJob,
    WorkerLease,
    WorkerPublicationState,
    WorkerRawManifest,
    WorkerRun,
)
from app.worker.contracts import (
    ExecutionCounters,
    HandlerResult,
    RawArtifactReference,
    SourceChangeSummary,
    StagingResult,
    ValidationResult,
)
from app.worker.errors import (
    HandlerNotRegisteredError,
    InvalidDataError,
    LegalBlockError,
    LeaseConflictError,
    LeaseLostError,
    LiveHandlerProhibitedError,
    PublicationValidationError,
    SchemaMismatchError,
    TemporaryInfrastructureError,
    WorkerNetworkError,
    WorkerTimeoutError,
)
from app.worker.execution import (
    RetryPolicy,
    WorkerExecutor,
    claim_next_job,
    complete_run_failure,
    complete_run_success,
    create_job,
    heartbeat_run,
    observe_run,
    recover_stale_runs,
    register_handler,
)
from app.worker.registry import HandlerRegistry


NOW = datetime(2026, 9, 23, 8, tzinfo=timezone.utc)


def _empty_handler(_context):
    return HandlerResult()


def _success_handler(context):
    context.heartbeat()
    return HandlerResult(
        raw_artifacts=(
            RawArtifactReference(
                artifact_reference="fixture://artifact/one",
                checksum="a" * 64,
                manifest={"records": 2},
            ),
        ),
        checksum_metadata={
            "input_sha256": "a" * 64,
            "fencing_token": context.fencing_token,
        },
    )


def _change_summary_handler(_context):
    return HandlerResult(
        checksum_metadata={"release": "fixture-v1"},
        change_summary=SourceChangeSummary(
            matched_companies=2,
            new_facts=3,
            changed_facts=1,
            removed_or_expired_facts=0,
            unchanged_facts=4,
            replayed_facts=0,
            quarantined_records=0,
            source_records=8,
            source_data_date=date(2026, 9, 25),
            previous_source_data_date=None,
            unavailable_reasons={
                "previous_source_data_date": "first accepted publication"
            },
        ),
    )


def _invalid_handler(_context):
    raise InvalidDataError("fixture row is invalid")


def _network_handler(_context):
    raise WorkerNetworkError("fixture network unavailable")


def _counter_handler(context):
    context.report_counters(
        ExecutionCounters(
            records_seen=10,
            records_written=4,
            records_rejected=1,
            records_duplicated=2,
            records_published=0,
        )
    )
    return HandlerResult(
        counters=ExecutionCounters(
            records_seen=10,
            records_written=7,
            records_rejected=1,
            records_duplicated=2,
            records_published=7,
        )
    )


def _counter_then_fail_handler(context):
    context.report_counters(
        ExecutionCounters(
            records_seen=8,
            records_written=3,
            records_rejected=2,
            records_duplicated=1,
            records_published=0,
        )
    )
    raise InvalidDataError("failure after durable progress")


def _publication_handler(context):
    return HandlerResult(
        staging_result=StagingResult(
            staging_pointer=f"staging://snapshot/{context.job_id}",
            checksum="b" * 64,
            validation=ValidationResult(
                accepted=True,
                metadata={"schema": "fixture-v1"},
            ),
        )
    )


def _slow_publication_publisher(_session, _claim, result):
    time.sleep(1.2)
    return result


def _slow_mutating_publisher(session, claim, result):
    time.sleep(0.8)
    session.add(
        WorkerPublicationState(
            source_id=claim.source_id,
            active_pointer="staging://must-roll-back",
            rollback_pointer=None,
            generation=1,
            last_fencing_token=claim.fencing_token,
            published_by_run_id=claim.run_id,
            validation_metadata={"uncommitted": True},
        )
    )
    session.flush()
    return result


def _accepted_publication_handler(_context):
    return HandlerResult(
        staging_result=StagingResult(
            staging_pointer="staging://accepted",
            validation=ValidationResult(accepted=True),
        )
    )


def _rejected_publication_handler(_context):
    return HandlerResult(
        staging_result=StagingResult(
            staging_pointer="staging://rejected",
            validation=ValidationResult(
                accepted=False,
                errors=("schema mismatch",),
            ),
        )
    )


def _stuck_handler(_context):
    while True:
        time.sleep(10)


def _graceful_handler(context):
    while not context.shutdown_requested():
        time.sleep(0.01)
    return HandlerResult(checksum_metadata={"shutdown_observed": True})


@pytest.fixture
def worker_db():
    connection = engine.connect()
    transaction = connection.begin()
    assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()


def _identity(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _register_fixture(
    factory,
    registry,
    source_id,
    handler,
    *,
    version="fixture-v1",
    publisher=None,
):
    with factory() as session:
        register_handler(
            session,
            registry,
            source_id=source_id,
            version=version,
            handler=handler,
            publisher=publisher,
            approved=True,
            fixture=True,
            metadata={"task": "DEV-008"},
        )
        session.commit()


def _create(factory, source_id, *, version="fixture-v1", **changes):
    with factory() as session:
        creation = create_job(
            session,
            source_id=source_id,
            job_type=changes.pop("job_type", "fixture"),
            handler_version=version,
            idempotency_key=changes.pop("idempotency_key", _identity("job")),
            now=changes.pop("now", NOW),
            **changes,
        )
        session.commit()
        return creation.job.id


def test_change_summary_requires_reasons_for_unknown_metrics():
    with pytest.raises(
        ValueError,
        match="null change-summary metric requires reason: changed_facts",
    ):
        SourceChangeSummary(
            matched_companies=1,
            new_facts=1,
            changed_facts=None,
            removed_or_expired_facts=0,
            unchanged_facts=0,
            replayed_facts=0,
            quarantined_records=0,
            source_records=1,
            source_data_date=date(2026, 9, 25),
            previous_source_data_date=date(2026, 9, 24),
        )


def test_change_summary_is_persisted_in_worker_run_metadata(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(worker_db, registry, source_id, _change_summary_handler)
    job_id = _create(worker_db, source_id)
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="change-summary-worker",
        clock=lambda: NOW + timedelta(seconds=1),
    )

    run_id = executor.run_once()

    with worker_db() as session:
        run = session.get(WorkerRun, run_id)
        assert run.job_id == job_id
        assert run.checksum_metadata["release"] == "fixture-v1"
        assert run.checksum_metadata["change_summary"] == {
            "matched_companies": 2,
            "new_facts": 3,
            "changed_facts": 1,
            "removed_or_expired_facts": 0,
            "unchanged_facts": 4,
            "replayed_facts": 0,
            "quarantined_records": 0,
            "source_records": 8,
            "source_data_date": "2026-09-25",
            "previous_source_data_date": None,
            "unavailable_reasons": {
                "previous_source_data_date": "first accepted publication"
            },
        }


def test_job_create_and_duplicate_prevention(worker_db):
    key = _identity("idempotency")
    source_id = _identity("source")
    with worker_db() as session:
        first = create_job(
            session,
            source_id=source_id,
            job_type="fixture",
            handler_version="v1",
            idempotency_key=key,
            schedule_metadata={"trigger": "manual"},
            now=NOW,
        )
        second = create_job(
            session,
            source_id=source_id,
            job_type="fixture",
            handler_version="v1",
            idempotency_key=key,
            schedule_metadata={"trigger": "manual"},
            now=NOW,
        )
        session.commit()

        assert first.created is True
        assert second.created is False
        assert second.job.id == first.job.id
        assert session.scalar(
            sa.select(sa.func.count()).select_from(WorkerJob).where(
                WorkerJob.idempotency_key == key
            )
        ) == 1


def test_registered_handler_run_lifecycle_and_success(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")

    _register_fixture(worker_db, registry, source_id, _success_handler)
    job_id = _create(worker_db, source_id)
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="test-worker",
        clock=lambda: NOW + timedelta(seconds=1),
    )

    run_id = executor.run_once()

    with worker_db() as session:
        job = session.get(WorkerJob, job_id)
        run = session.get(WorkerRun, run_id)
        manifests = tuple(
            session.scalars(
                sa.select(WorkerRawManifest).where(WorkerRawManifest.run_id == run_id)
            )
        )
        assert job.status == "succeeded"
        assert run.status == "succeeded"
        assert run.current_stage == "complete"
        assert run.finished_at is not None
        assert run.duration_ms >= 0
        assert run.checksum_metadata["input_sha256"] == "a" * 64
        assert run.checksum_metadata["fencing_token"] == run.fencing_token
        assert manifests[0].immutable is True


def test_run_counters_persist_and_survive_success_lifecycle(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(worker_db, registry, source_id, _counter_handler)
    job_id = _create(worker_db, source_id)
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="counter-worker",
        clock=lambda: NOW + timedelta(seconds=1),
    )

    run_id = executor.run_once()

    with worker_db() as session:
        run = session.get(WorkerRun, run_id)
        observation = observe_run(
            session,
            run_id,
            stale_after=timedelta(seconds=30),
            now=NOW + timedelta(seconds=2),
        )
        assert session.get(WorkerJob, job_id).status == "succeeded"
        assert run.status == "succeeded"
        assert observation.counters == ExecutionCounters(
            records_seen=10,
            records_written=7,
            records_rejected=1,
            records_duplicated=2,
            records_published=7,
        )


def test_last_reported_counters_survive_failed_lifecycle(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(worker_db, registry, source_id, _counter_then_fail_handler)
    job_id = _create(worker_db, source_id, max_attempts=1)
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="counter-failure-worker",
        clock=lambda: NOW + timedelta(seconds=1),
    )

    with pytest.raises(InvalidDataError, match="failure after durable progress"):
        executor.run_once()

    with worker_db() as session:
        run = session.scalar(sa.select(WorkerRun).where(WorkerRun.job_id == job_id))
        assert run.status == "failed"
        assert (
            run.records_seen,
            run.records_written,
            run.records_rejected,
            run.records_duplicated,
            run.records_published,
        ) == (8, 3, 2, 1, 0)


def test_run_failure_is_terminal_for_invalid_data(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")

    _register_fixture(worker_db, registry, source_id, _invalid_handler)
    job_id = _create(worker_db, source_id)
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="failure-worker",
        clock=lambda: NOW + timedelta(seconds=1),
    )

    with pytest.raises(InvalidDataError, match="fixture row is invalid"):
        executor.run_once()

    with worker_db() as session:
        job = session.get(WorkerJob, job_id)
        run = session.scalar(sa.select(WorkerRun).where(WorkerRun.job_id == job_id))
        assert job.status == "failed"
        assert run.status == "failed"
        assert run.retryable is False
        assert run.errors[0]["kind"] == "invalid_data"


def test_network_failure_schedules_retry(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")

    _register_fixture(worker_db, registry, source_id, _network_handler)
    job_id = _create(worker_db, source_id, max_attempts=2)
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="retry-worker",
        retry_policy=RetryPolicy(base_delay_seconds=5),
        clock=lambda: NOW + timedelta(seconds=1),
    )

    with pytest.raises(WorkerNetworkError):
        executor.run_once()

    with worker_db() as session:
        job = session.get(WorkerJob, job_id)
        run = session.scalar(sa.select(WorkerRun).where(WorkerRun.job_id == job_id))
        assert job.status == "retry_scheduled"
        assert job.next_attempt_at == NOW + timedelta(seconds=6)
        assert run.retryable is True
        assert run.errors[0]["kind"] == "network_failure"


def test_lease_claim_conflict_expiry_and_fencing_token(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(worker_db, registry, source_id, _empty_handler)
    _create(worker_db, source_id, idempotency_key=_identity("job-a"))
    _create(worker_db, source_id, idempotency_key=_identity("job-b"))

    with worker_db() as session:
        first = claim_next_job(
            session,
            registry,
            worker_id="worker-a",
            lease_ttl=timedelta(seconds=10),
            now=NOW,
        )
        session.commit()
    assert first.fencing_token > 0

    with worker_db() as session:
        with pytest.raises(LeaseConflictError):
            claim_next_job(
                session,
                registry,
                worker_id="worker-b",
                lease_ttl=timedelta(seconds=10),
                now=NOW + timedelta(seconds=5),
            )
        session.rollback()

    with worker_db() as session:
        second = claim_next_job(
            session,
            registry,
            worker_id="worker-b",
            lease_ttl=timedelta(seconds=10),
            now=NOW + timedelta(seconds=11),
        )
        session.commit()
    assert second.fencing_token > first.fencing_token
    assert second.run_id != first.run_id


def test_claim_rotates_source_families_without_breaking_source_fifo(worker_db):
    registry = HandlerRegistry()
    source_a = _identity("source-a")
    source_b = _identity("source-b")
    _register_fixture(worker_db, registry, source_a, _empty_handler)
    _register_fixture(worker_db, registry, source_b, _empty_handler)
    first_a = _create(
        worker_db,
        source_a,
        idempotency_key=_identity("a-first"),
        now=NOW,
    )
    second_a = _create(
        worker_db,
        source_a,
        idempotency_key=_identity("a-second"),
        now=NOW + timedelta(seconds=1),
    )
    only_b = _create(
        worker_db,
        source_b,
        idempotency_key=_identity("b-only"),
        now=NOW + timedelta(seconds=2),
    )

    with worker_db() as session:
        first = claim_next_job(
            session,
            registry,
            worker_id="fair-worker",
            lease_ttl=timedelta(seconds=30),
            now=NOW + timedelta(seconds=3),
        )
        session.commit()
    assert first.job_id == first_a
    with worker_db() as session:
        complete_run_success(
            session, first, HandlerResult(), now=NOW + timedelta(seconds=4)
        )
        session.commit()

    with worker_db() as session:
        second = claim_next_job(
            session,
            registry,
            worker_id="fair-worker",
            lease_ttl=timedelta(seconds=30),
            now=NOW + timedelta(seconds=5),
        )
        session.commit()
    assert second.job_id == only_b
    assert second.job_id != second_a


def test_fencing_token_is_monotonic_after_success_deletes_lease(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(worker_db, registry, source_id, _empty_handler)
    _create(worker_db, source_id, idempotency_key=_identity("job-a"), now=NOW)
    _create(
        worker_db,
        source_id,
        idempotency_key=_identity("job-b"),
        now=NOW + timedelta(seconds=1),
    )

    with worker_db() as session:
        first = claim_next_job(
            session,
            registry,
            worker_id="worker-a",
            lease_ttl=timedelta(seconds=30),
            now=NOW,
        )
        session.commit()
    with worker_db() as session:
        complete_run_success(
            session,
            first,
            HandlerResult(),
            now=NOW + timedelta(seconds=1),
        )
        session.commit()
        assert session.get(WorkerLease, source_id) is None
    with worker_db() as session:
        second = claim_next_job(
            session,
            registry,
            worker_id="worker-b",
            lease_ttl=timedelta(seconds=30),
            now=NOW + timedelta(seconds=2),
        )
        session.commit()

    assert second.fencing_token > first.fencing_token


def test_fencing_token_is_monotonic_after_failure_deletes_lease(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(worker_db, registry, source_id, _empty_handler)
    _create(
        worker_db,
        source_id,
        idempotency_key=_identity("job-a"),
        max_attempts=1,
        now=NOW,
    )
    _create(
        worker_db,
        source_id,
        idempotency_key=_identity("job-b"),
        now=NOW + timedelta(seconds=1),
    )

    with worker_db() as session:
        first = claim_next_job(
            session,
            registry,
            worker_id="worker-a",
            lease_ttl=timedelta(seconds=30),
            now=NOW,
        )
        session.commit()
    with worker_db() as session:
        complete_run_failure(
            session,
            first,
            InvalidDataError("fixture failure"),
            RetryPolicy(),
            now=NOW + timedelta(seconds=1),
        )
        session.commit()
        assert session.get(WorkerLease, source_id) is None
    with worker_db() as session:
        second = claim_next_job(
            session,
            registry,
            worker_id="worker-b",
            lease_ttl=timedelta(seconds=30),
            now=NOW + timedelta(seconds=2),
        )
        session.commit()

    assert second.fencing_token > first.fencing_token


@pytest.mark.parametrize(
    "error",
    [
        WorkerTimeoutError("timeout"),
        WorkerNetworkError("network"),
        TemporaryInfrastructureError("database unavailable"),
    ],
)
def test_retry_policy_accepts_only_approved_transient_errors(error):
    policy = RetryPolicy(base_delay_seconds=5)
    assert policy.allows(error, attempt_no=1, max_attempts=3)


@pytest.mark.parametrize(
    "error",
    [
        InvalidDataError("invalid"),
        SchemaMismatchError("schema"),
        LegalBlockError("legal"),
    ],
)
def test_retry_policy_rejects_non_retryable_errors(error):
    policy = RetryPolicy(base_delay_seconds=5)
    assert not policy.allows(error, attempt_no=1, max_attempts=3)


def test_missing_handler_blocks_claim(worker_db):
    source_id = _identity("source")
    job_id = _create(worker_db, source_id, version="missing-v1")

    with worker_db() as session:
        with pytest.raises(HandlerNotRegisteredError, match="approved handler is missing"):
            claim_next_job(
                session,
                HandlerRegistry(),
                worker_id="worker",
                lease_ttl=timedelta(seconds=30),
                now=NOW,
            )
        session.rollback()

    with worker_db() as session:
        assert session.get(WorkerJob, job_id).status == "queued"
        assert session.scalar(
            sa.select(sa.func.count()).select_from(WorkerRun).where(
                WorkerRun.job_id == job_id
            )
        ) == 0


def test_registry_rejects_live_handler():
    with pytest.raises(LiveHandlerProhibitedError):
        HandlerRegistry().register(
            source_id="live-source",
            version="v1",
            handler=lambda _context: HandlerResult(),
            approved=True,
            live=True,
        )


def test_worker_heartbeat_timeout_and_recovery(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(worker_db, registry, source_id, _empty_handler)
    job_id = _create(worker_db, source_id, timeout_seconds=5)

    with worker_db() as session:
        claim = claim_next_job(
            session,
            registry,
            worker_id="stale-worker",
            lease_ttl=timedelta(seconds=5),
            now=NOW,
        )
        session.commit()
    with worker_db() as session:
        heartbeat_run(
            session,
            claim,
            lease_ttl=timedelta(seconds=5),
            stage="raw",
            now=NOW + timedelta(seconds=1),
        )
        session.commit()
    with worker_db() as session:
        before = observe_run(
            session,
            claim.run_id,
            stale_after=timedelta(seconds=5),
            now=NOW + timedelta(seconds=4),
        )
        assert before.current_stage == "raw"
        assert before.stale is False

        recovered = recover_stale_runs(
            session,
            stale_after=timedelta(seconds=5),
            retry_policy=RetryPolicy(base_delay_seconds=1),
            now=NOW + timedelta(seconds=7),
        )
        session.commit()
    assert recovered == (claim.run_id,)

    with worker_db() as session:
        job = session.get(WorkerJob, job_id)
        run = session.get(WorkerRun, claim.run_id)
        assert job.status == "retry_scheduled"
        assert run.status == "timed_out"
        assert run.retryable is True
        assert run.errors[0]["kind"] == "timeout"
        assert session.get(WorkerLease, source_id) is None


def test_stale_recovery_cannot_release_newer_lease_with_same_worker_id(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(worker_db, registry, source_id, _empty_handler)
    _create(
        worker_db,
        source_id,
        idempotency_key=_identity("old-job"),
        timeout_seconds=5,
    )
    _create(
        worker_db,
        source_id,
        idempotency_key=_identity("new-job"),
        timeout_seconds=30,
        now=NOW + timedelta(seconds=1),
    )

    with worker_db() as session:
        old_claim = claim_next_job(
            session,
            registry,
            worker_id="reused-worker-id",
            lease_ttl=timedelta(seconds=5),
            now=NOW,
        )
        session.commit()
    with worker_db() as session:
        new_claim = claim_next_job(
            session,
            registry,
            worker_id="reused-worker-id",
            lease_ttl=timedelta(seconds=20),
            now=NOW + timedelta(seconds=6),
        )
        session.commit()
    assert new_claim.fencing_token > old_claim.fencing_token

    with worker_db() as session:
        recovered = recover_stale_runs(
            session,
            stale_after=timedelta(seconds=5),
            retry_policy=RetryPolicy(base_delay_seconds=1),
            now=NOW + timedelta(seconds=7),
        )
        session.commit()
    assert recovered == (old_claim.run_id,)

    with worker_db() as session:
        lease = session.get(WorkerLease, source_id)
        assert lease.fencing_token == new_claim.fencing_token
        assert lease.owner_worker_id == new_claim.worker_id
        assert session.get(WorkerRun, new_claim.run_id).status == "running"


def test_stale_fencing_token_cannot_publish_after_reclaim(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(worker_db, registry, source_id, _empty_handler)
    _create(worker_db, source_id, idempotency_key=_identity("old-job"), now=NOW)
    _create(
        worker_db,
        source_id,
        idempotency_key=_identity("new-job"),
        now=NOW + timedelta(seconds=1),
    )

    with worker_db() as session:
        old_claim = claim_next_job(
            session,
            registry,
            worker_id="old-worker",
            lease_ttl=timedelta(seconds=5),
            now=NOW,
        )
        session.commit()
    with worker_db() as session:
        new_claim = claim_next_job(
            session,
            registry,
            worker_id="new-worker",
            lease_ttl=timedelta(seconds=30),
            now=NOW + timedelta(seconds=6),
        )
        session.commit()

    stale_result = HandlerResult(
        staging_result=StagingResult(
            staging_pointer="staging://stale",
            validation=ValidationResult(accepted=True),
        )
    )
    with worker_db() as session:
        with pytest.raises(LeaseLostError):
            complete_run_success(
                session,
                old_claim,
                stale_result,
                now=NOW + timedelta(seconds=7),
            )
        session.rollback()

    current_result = HandlerResult(
        staging_result=StagingResult(
            staging_pointer="staging://current",
            validation=ValidationResult(accepted=True),
        )
    )
    with worker_db() as session:
        complete_run_success(
            session,
            new_claim,
            current_result,
            now=NOW + timedelta(seconds=7),
        )
        session.commit()
    with worker_db() as session:
        state = session.get(WorkerPublicationState, source_id)
        assert state.active_pointer == "staging://current"
        assert state.rollback_pointer is None


def test_publication_pointer_transition_is_atomic_and_keeps_rollback(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")

    _register_fixture(worker_db, registry, source_id, _publication_handler)
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="publisher",
        clock=lambda: NOW + timedelta(seconds=1),
    )

    first_job_id = _create(
        worker_db, source_id, idempotency_key=_identity("publication-1")
    )
    executor.run_once()
    second_job_id = _create(
        worker_db, source_id, idempotency_key=_identity("publication-2")
    )
    executor.run_once()

    with worker_db() as session:
        state = session.get(WorkerPublicationState, source_id)
        assert state.active_pointer == f"staging://snapshot/{second_job_id}"
        assert state.rollback_pointer == f"staging://snapshot/{first_job_id}"
        assert state.generation == 2
        assert state.validation_metadata["validation"] == {"schema": "fixture-v1"}


def test_long_publisher_renews_lease_until_final_fencing_assertion(
    worker_db,
    monkeypatch,
):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(
        worker_db,
        registry,
        source_id,
        _publication_handler,
        publisher=_slow_publication_publisher,
    )
    job_id = _create(
        worker_db,
        source_id,
        timeout_seconds=5,
        now=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="slow-publisher",
        lease_ttl=timedelta(seconds=0.5),
    )
    heartbeat_stages = []
    original_heartbeat = executor._heartbeat

    def recording_heartbeat(claim, deadline_at, *, stage="handler"):
        heartbeat_stages.append(stage)
        return original_heartbeat(claim, deadline_at, stage=stage)

    monkeypatch.setattr(executor, "_heartbeat", recording_heartbeat)

    run_id = executor.run_once()

    assert heartbeat_stages.count("publication") >= 2
    with worker_db() as session:
        assert session.get(WorkerJob, job_id).status == "succeeded"
        assert session.get(WorkerRun, run_id).status == "succeeded"
        state = session.get(WorkerPublicationState, source_id)
        assert state.active_pointer == f"staging://snapshot/{job_id}"


def test_publisher_heartbeat_loss_rolls_back_uncommitted_state(
    worker_db,
    monkeypatch,
):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(
        worker_db,
        registry,
        source_id,
        _publication_handler,
        publisher=_slow_mutating_publisher,
    )
    job_id = _create(
        worker_db,
        source_id,
        timeout_seconds=5,
        max_attempts=1,
        now=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="fenced-publisher",
        lease_ttl=timedelta(seconds=0.5),
    )
    original_heartbeat = executor._heartbeat

    def lose_publication_lease(claim, deadline_at, *, stage="handler"):
        if stage == "publication":
            raise LeaseLostError("forced publication fence loss")
        return original_heartbeat(claim, deadline_at, stage=stage)

    monkeypatch.setattr(executor, "_heartbeat", lose_publication_lease)

    with pytest.raises(LeaseLostError, match="forced publication fence loss"):
        executor.run_once()

    with worker_db() as session:
        job = session.get(WorkerJob, job_id)
        run = session.scalar(sa.select(WorkerRun).where(WorkerRun.job_id == job_id))
        assert job.status == "failed"
        assert run.status == "failed"
        assert run.errors[0]["kind"] == "temporary_infrastructure"
        assert session.get(WorkerPublicationState, source_id) is None
        assert session.get(WorkerLease, source_id) is None


def test_rejected_staging_result_does_not_move_publication_pointer(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(
        worker_db,
        registry,
        source_id,
        _accepted_publication_handler,
        version="accepted-v1",
    )
    _register_fixture(
        worker_db,
        registry,
        source_id,
        _rejected_publication_handler,
        version="rejected-v1",
    )
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="atomic-publisher",
        clock=lambda: NOW + timedelta(seconds=1),
    )
    _create(
        worker_db,
        source_id,
        version="accepted-v1",
        idempotency_key=_identity("accepted-job"),
    )
    executor.run_once()
    rejected_job_id = _create(
        worker_db,
        source_id,
        version="rejected-v1",
        idempotency_key=_identity("rejected-job"),
    )

    with pytest.raises(PublicationValidationError, match="schema mismatch"):
        executor.run_once()

    with worker_db() as session:
        state = session.get(WorkerPublicationState, source_id)
        assert state.active_pointer == "staging://accepted"
        assert state.rollback_pointer is None
        assert state.generation == 1
        assert session.get(WorkerJob, rejected_job_id).status == "failed"


def test_timeout_terminates_stuck_handler_and_records_recovery_state(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(worker_db, registry, source_id, _stuck_handler)
    real_now = datetime.now(timezone.utc) - timedelta(seconds=1)
    job_id = _create(
        worker_db,
        source_id,
        timeout_seconds=1,
        max_attempts=1,
        now=real_now,
    )
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="timeout-worker",
        child_cleanup_seconds=0.5,
    )
    child_pids_before = {child.pid for child in multiprocessing.active_children()}
    started_at = time.monotonic()

    with pytest.raises(WorkerTimeoutError):
        executor.run_once()

    assert time.monotonic() - started_at < 4
    child_pids_after = {child.pid for child in multiprocessing.active_children()}
    assert child_pids_after <= child_pids_before
    with worker_db() as session:
        job = session.get(WorkerJob, job_id)
        run = session.scalar(sa.select(WorkerRun).where(WorkerRun.job_id == job_id))
        assert job.status == "failed"
        assert run.status == "timed_out"
        assert run.finished_at is not None
        assert run.errors[0]["kind"] == "timeout"
        assert session.get(WorkerLease, source_id) is None


def test_graceful_shutdown_is_visible_to_inflight_child(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    _register_fixture(worker_db, registry, source_id, _graceful_handler)
    real_now = datetime.now(timezone.utc) - timedelta(seconds=1)
    job_id = _create(
        worker_db,
        source_id,
        timeout_seconds=5,
        now=real_now,
    )
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="graceful-worker",
    )

    def request_shutdown():
        time.sleep(0.2)
        executor.request_shutdown()

    stopper = Thread(target=request_shutdown)
    stopper.start()
    run_id = executor.run_once()
    stopper.join(timeout=2)

    assert executor.shutdown_requested is True
    assert executor.run_once() is None
    with worker_db() as session:
        assert session.get(WorkerJob, job_id).status == "succeeded"
        run = session.get(WorkerRun, run_id)
        assert run.status == "succeeded"
        assert run.checksum_metadata == {"shutdown_observed": True}


def test_graceful_shutdown_stops_new_claims(worker_db):
    registry = HandlerRegistry()
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="stopping-worker",
    )
    executor.request_shutdown()
    assert executor.shutdown_requested is True
    assert executor.run_once() is None
