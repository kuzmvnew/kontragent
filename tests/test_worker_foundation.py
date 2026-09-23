from datetime import datetime, timedelta, timezone
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
    HandlerResult,
    RawArtifactReference,
    StagingResult,
    ValidationResult,
)
from app.worker.errors import (
    HandlerNotRegisteredError,
    InvalidDataError,
    LegalBlockError,
    LeaseConflictError,
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
    create_job,
    heartbeat_run,
    observe_run,
    recover_stale_runs,
    register_handler,
)
from app.worker.registry import HandlerRegistry


NOW = datetime(2026, 9, 23, 8, tzinfo=timezone.utc)


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


def _register_fixture(factory, registry, source_id, handler, *, version="fixture-v1"):
    with factory() as session:
        register_handler(
            session,
            registry,
            source_id=source_id,
            version=version,
            handler=handler,
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
    beats = []

    def fixture_handler(context):
        context.heartbeat()
        beats.append(context.fencing_token)
        return HandlerResult(
            raw_artifacts=(
                RawArtifactReference(
                    artifact_reference="fixture://artifact/one",
                    checksum="a" * 64,
                    manifest={"records": 2},
                ),
            ),
            checksum_metadata={"input_sha256": "a" * 64},
        )

    _register_fixture(worker_db, registry, source_id, fixture_handler)
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
        assert run.checksum_metadata == {"input_sha256": "a" * 64}
        assert manifests[0].immutable is True
        assert beats == [1]


def test_run_failure_is_terminal_for_invalid_data(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")

    def invalid_handler(_context):
        raise InvalidDataError("fixture row is invalid")

    _register_fixture(worker_db, registry, source_id, invalid_handler)
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

    def network_handler(_context):
        raise WorkerNetworkError("fixture network unavailable")

    _register_fixture(worker_db, registry, source_id, network_handler)
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
    _register_fixture(worker_db, registry, source_id, lambda _ctx: HandlerResult())
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
    assert first.fencing_token == 1

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
    assert second.fencing_token == 2
    assert second.run_id != first.run_id


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
    _register_fixture(worker_db, registry, source_id, lambda _ctx: HandlerResult())
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
    _register_fixture(worker_db, registry, source_id, lambda _ctx: HandlerResult())
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
    assert old_claim.fencing_token == 1
    assert new_claim.fencing_token == 2

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


def test_publication_pointer_transition_is_atomic_and_keeps_rollback(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    pointers = iter(("staging://snapshot/v1", "staging://snapshot/v2"))

    def fixture_handler(_context):
        pointer = next(pointers)
        return HandlerResult(
            staging_result=StagingResult(
                staging_pointer=pointer,
                checksum="b" * 64,
                validation=ValidationResult(
                    accepted=True,
                    metadata={"schema": "fixture-v1"},
                ),
            )
        )

    _register_fixture(worker_db, registry, source_id, fixture_handler)
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="publisher",
        clock=lambda: NOW + timedelta(seconds=1),
    )

    _create(worker_db, source_id, idempotency_key=_identity("publication-1"))
    executor.run_once()
    _create(worker_db, source_id, idempotency_key=_identity("publication-2"))
    executor.run_once()

    with worker_db() as session:
        state = session.get(WorkerPublicationState, source_id)
        assert state.active_pointer == "staging://snapshot/v2"
        assert state.rollback_pointer == "staging://snapshot/v1"
        assert state.generation == 2
        assert state.validation_metadata["validation"] == {"schema": "fixture-v1"}


def test_rejected_staging_result_does_not_move_publication_pointer(worker_db):
    registry = HandlerRegistry()
    source_id = _identity("source")
    validations = iter(
        (
            ValidationResult(accepted=True),
            ValidationResult(accepted=False, errors=("schema mismatch",)),
        )
    )

    def fixture_handler(_context):
        validation = next(validations)
        return HandlerResult(
            staging_result=StagingResult(
                staging_pointer=(
                    "staging://accepted" if validation.accepted else "staging://rejected"
                ),
                validation=validation,
            )
        )

    _register_fixture(worker_db, registry, source_id, fixture_handler)
    executor = WorkerExecutor(
        session_factory=worker_db,
        registry=registry,
        worker_id="atomic-publisher",
        clock=lambda: NOW + timedelta(seconds=1),
    )
    _create(worker_db, source_id, idempotency_key=_identity("accepted-job"))
    executor.run_once()
    rejected_job_id = _create(
        worker_db, source_id, idempotency_key=_identity("rejected-job")
    )

    with pytest.raises(PublicationValidationError, match="schema mismatch"):
        executor.run_once()

    with worker_db() as session:
        state = session.get(WorkerPublicationState, source_id)
        assert state.active_pointer == "staging://accepted"
        assert state.rollback_pointer is None
        assert state.generation == 1
        assert session.get(WorkerJob, rejected_job_id).status == "failed"


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
