from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.database.postgres import engine
from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichmentRun, CompanySourceCoverage
from app.models.registry_master import CompanyRegistryChange, MasterReplaySignal
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerJob, WorkerLease, WorkerRun
from app.services.dataset_applicability_policy import (
    CANONICAL_DATASET_POLICIES,
    PRODUCTION_DATASET_CODES_2026_09_28,
    canonical_dataset_applicability,
)
from app.services.dataset_applicability_sync_service import (
    apply_dataset_applicability_sync,
    plan_dataset_applicability_sync,
)
from app.services.post_migration_recovery_service import (
    EnrichmentRecoveryCursor,
    apply_enrichment_recovery_batch,
    apply_stale_worker_recovery,
    plan_enrichment_recovery_batch,
    plan_stale_worker_recovery,
    reconcile_not_applicable_replay_batch,
    recovery_invariants,
)
from app.services.source_applicability_service import parse_source_applicability
from app.worker.execution import RetryPolicy
from scripts.recover_post_migration_factory import _apply_worker_recovery_batches


NOW = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)


def _source(session: Session, suffix: str) -> DataSource:
    row = DataSource(
        code=f"recovery-{suffix}",
        name=f"Recovery {suffix}",
        source_type="official",
        enabled=True,
    )
    session.add(row)
    session.flush()
    return row


def _dataset(
    session: Session,
    *,
    source: DataSource,
    code: str,
    applicability,
) -> DataSet:
    row = DataSet(
        source_id=source.id,
        code=code,
        name=code,
        domain="test",
        update_mode="api",
        data_format="json",
        refresh_schedule="manual",
        priority=77,
        enabled=True,
        source_url="https://example.invalid/source",
        description="recovery fixture",
        dataset_kind="on_demand_api",
        freshness_policy="daily",
        operational_status="current",
        auto_update_status="configured",
        last_success_at=NOW,
        coverage={"operational_accepted": True},
        applicability=applicability,
    )
    session.add(row)
    session.flush()
    return row


def _company(session: Session, suffix: str, *, entity_type: str = "legal") -> Company:
    inn = str(uuid4().int).zfill(32)[-10 if entity_type == "legal" else -12 :]
    row = Company(
        inn=inn,
        name=f"Recovery company {suffix}",
        entity_type=entity_type,
        status="ACTIVE",
    )
    session.add(row)
    session.flush()
    return row


def _risk_and_summary(session: Session, company: Company, suffix: str):
    assessment_id = str(uuid4())
    summary_id = str(uuid4())
    risk = CompanyRiskAssessmentV3(
        assessment_id=assessment_id,
        company_id=company.id,
        subject_scope="legal",
        risk_model_version="recovery-v1",
        ruleset_version="recovery-v1",
        coverage_policy_version="recovery-v1",
        applicability_policy_version="recovery-v1",
        source_resolution_policy_version="recovery-v1",
        freshness_policy_version="recovery-v1",
        input_hash=(suffix * 64)[:64],
        calculated_at=NOW,
        evidence_snapshot=[],
        resolved_checks=[],
        factors=[],
        coverage_snapshot={},
        mandatory_gate={},
        limitations=[],
        result_payload={},
    )
    summary = CompanySummaryV3(
        summary_id=summary_id,
        company_id=company.id,
        risk_assessment_id=assessment_id,
        summary_model_version="recovery-v1",
        projection_policy_version="recovery-v1",
        generated_at=NOW,
        structured_payload={},
        explainability_refs=[],
    )
    session.add_all((risk, summary))
    session.flush()
    return risk, summary


def _run(
    session: Session,
    *,
    company: Company,
    dataset: DataSet,
    suffix: str,
    status: str = "running",
    stage: str = "risk",
    public_ready: bool = True,
    created_at: datetime | None = None,
):
    created_at = created_at or NOW - timedelta(hours=2)
    risk, summary = _risk_and_summary(session, company, suffix)
    run = CompanyEnrichmentRun(
        company_id=company.id,
        trigger="legacy_recovery_fixture",
        idempotency_key=f"legacy-recovery:{suffix}",
        status=status,
        stage=stage,
        applicable_sources=[],
        source_count=1,
        completed_source_count=1,
        failed_source_count=0,
        risk_assessment_id=risk.assessment_id,
        summary_id=summary.summary_id,
        public_ready=public_ready,
        started_at=NOW - timedelta(hours=2),
        created_at=created_at,
        updated_at=NOW - timedelta(hours=1),
    )
    session.add(run)
    session.flush()
    coverage = CompanySourceCoverage(
        enrichment_run_id=run.id,
        company_id=company.id,
        dataset_id=dataset.id,
        source_id=dataset.code,
        worker_source_id=dataset.code,
        mode="point_check",
        status="FOUND",
        execution_status="succeeded",
        source_snapshot={"fixture": True},
        handler_version="recovery-v1",
        master_replay_signal_ids=[],
        fact_count=1,
        checked_at=NOW,
        finished_at=NOW,
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW,
    )
    session.add(coverage)
    session.flush()
    return run, risk, summary


def _cursor_before(company: Company) -> EnrichmentRecoveryCursor:
    return EnrichmentRecoveryCursor(
        company_id=company.id - 1,
        created_at=NOW,
        run_id=uuid4(),
    )


def _stale_worker(session: Session, suffix: str, *, recoverable: bool = True):
    job = WorkerJob(
        source_id=f"stale-{suffix}",
        job_type="fixture",
        handler_version="fixture-v1",
        schedule_metadata={},
        idempotency_key=f"stale:{suffix}",
        status="running",
        max_attempts=3,
        timeout_seconds=60 if recoverable else 86_400,
        next_attempt_at=NOW - timedelta(hours=2),
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(hours=2),
    )
    session.add(job)
    session.flush()
    run = WorkerRun(
        job_id=job.id,
        attempt_no=1,
        started_at=NOW - timedelta(hours=2),
        status="running",
        worker_id=f"dead-worker-{suffix}",
        fencing_token=1,
        handler_version="fixture-v1",
        current_stage="handler",
        errors=[],
        checksum_metadata={},
        heartbeat_at=NOW - timedelta(hours=2),
        retryable=False,
    )
    lease = WorkerLease(
        source_id=job.source_id,
        owner_worker_id=run.worker_id,
        fencing_token=1,
        acquired_at=NOW - timedelta(hours=2),
        heartbeat_at=NOW - timedelta(hours=2),
        expires_at=(
            NOW - timedelta(hours=1)
            if recoverable
            else NOW + timedelta(hours=1)
        ),
    )
    session.add_all((run, lease))
    session.flush()
    return job, run, lease


def _signal(
    session: Session,
    *,
    company: Company,
    dataset: DataSet,
    suffix: str,
    status: str,
) -> MasterReplaySignal:
    change = CompanyRegistryChange(
        source_id=f"recovery-master-{suffix}",
        company_id=company.id,
        run_id=None,
        inn=company.inn,
        event_type="created",
        changed_fields={"fixture": True},
        source_data_date=NOW.date(),
        source_record_key=f"recovery:{suffix}",
    )
    session.add(change)
    session.flush()
    signal = MasterReplaySignal(
        company_id=company.id,
        target_source_id=dataset.code,
        registry_change_id=change.id,
        status=status,
        last_error="preserved failure" if status == "failed" else None,
        completed_at=NOW if status == "failed" else None,
        created_at=NOW,
    )
    session.add(signal)
    session.flush()
    return signal


def test_canonical_policy_covers_exact_production_inventory_and_is_valid():
    assert len(PRODUCTION_DATASET_CODES_2026_09_28) == 45
    assert PRODUCTION_DATASET_CODES_2026_09_28 <= set(CANONICAL_DATASET_POLICIES)
    assert all(
        parse_source_applicability(policy.as_persisted()) is not None
        for policy in CANONICAL_DATASET_POLICIES.values()
    )


def test_exact_45_row_inventory_reaches_45_valid_then_second_apply_is_noop():
    with Session(engine) as session:
        source = _source(session, uuid4().hex[:10])
        for code in sorted(PRODUCTION_DATASET_CODES_2026_09_28):
            _dataset(
                session,
                source=source,
                code=code,
                applicability=None,
            )

        before = plan_dataset_applicability_sync(session)
        assert before.totals == {
            "total": 45,
            "valid_before": 0,
            "missing_before": 45,
            "invalid_before": 0,
            "will_set": 45,
            "will_change": 45,
            "unmapped": 0,
            "valid_after_expected": 45,
        }
        first = apply_dataset_applicability_sync(session)
        assert first.mutations == 45
        assert first.plan_after.totals == {
            "total": 45,
            "valid_before": 45,
            "missing_before": 0,
            "invalid_before": 0,
            "will_set": 0,
            "will_change": 0,
            "unmapped": 0,
            "valid_after_expected": 45,
        }
        second = apply_dataset_applicability_sync(session)
        assert second.mutations == 0
        session.rollback()


def test_applicability_sync_changes_only_policy_and_is_idempotent():
    with Session(engine) as session:
        source = _source(session, uuid4().hex[:10])
        dataset = session.scalar(
            sa.select(DataSet).where(DataSet.code == "worker_failure_probe")
        )
        if dataset is None:
            dataset = _dataset(
                session,
                source=source,
                code="worker_failure_probe",
                applicability=None,
            )
        else:
            dataset.applicability = None
            session.flush()
        dataset.updated_at = NOW - timedelta(days=2)
        session.flush()
        before = {
            column.name: deepcopy(getattr(dataset, column.name))
            for column in DataSet.__table__.columns
            if column.name != "applicability"
        }
        plan = plan_dataset_applicability_sync(session)
        item = next(item for item in plan.items if item.code == dataset.code)
        assert item.action == "SET"
        first = apply_dataset_applicability_sync(session)
        assert first.mutations >= 1
        session.refresh(dataset)
        assert dataset.applicability == canonical_dataset_applicability(dataset.code)
        assert before == {
            column.name: getattr(dataset, column.name)
            for column in DataSet.__table__.columns
            if column.name != "applicability"
        }
        second = apply_dataset_applicability_sync(session)
        assert second.mutations == 0
        session.rollback()


def test_unknown_dataset_blocks_applicability_apply_before_mutation():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        source = _source(session, suffix)
        unknown = _dataset(
            session,
            source=source,
            code=f"unmapped-recovery-{suffix}",
            applicability=None,
        )
        with pytest.raises(RuntimeError, match="unmapped datasets"):
            apply_dataset_applicability_sync(session)
        assert unknown.applicability is None
        session.rollback()


def test_stale_worker_plan_and_apply_use_retry_policy_without_handler():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        job, run, _lease = _stale_worker(session, suffix)
        policy = RetryPolicy()
        plan = plan_stale_worker_recovery(
            session,
            stale_after=timedelta(minutes=5),
            retry_policy=policy,
            now=NOW,
        )
        item = next(item for item in plan.batch_candidates if item.run_id == run.id)
        assert item.recoverable is True
        assert item.expected_job_status == "retry_scheduled"
        recovered = apply_stale_worker_recovery(
            session,
            stale_after=timedelta(minutes=5),
            retry_policy=policy,
            now=NOW,
        )
        assert run.id in recovered
        assert run.status == "timed_out"
        assert job.status == "retry_scheduled"
        assert session.get(WorkerLease, job.source_id) is None
        assert apply_stale_worker_recovery(
            session,
            stale_after=timedelta(minutes=5),
            retry_policy=policy,
            now=NOW,
        ) == ()
        session.rollback()


def test_worker_plan_counts_all_stale_and_skips_nonrecoverable_for_preview():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        _job, protected_run, _lease = _stale_worker(
            session, f"{suffix}-protected", recoverable=False
        )
        protected_run.heartbeat_at = NOW - timedelta(hours=3)
        _job, recoverable_run, _lease = _stale_worker(
            session, f"{suffix}-recoverable"
        )
        session.flush()

        plan = plan_stale_worker_recovery(
            session,
            stale_after=timedelta(minutes=5),
            retry_policy=RetryPolicy(),
            now=NOW,
            limit=1,
        )

        assert plan.total_stale == 2
        assert plan.total_recoverable == 1
        assert [item.run_id for item in plan.batch_candidates] == [
            recoverable_run.id
        ]
        session.rollback()


def test_two_stale_workers_apply_in_exact_one_one_zero_batches():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        jobs_and_runs = (
            _stale_worker(session, f"{suffix}-first")[:2],
            _stale_worker(session, f"{suffix}-second")[:2],
        )
        policy = RetryPolicy()
        recovered_batches: list[tuple] = []
        for expected_total in (2, 1):
            plan = plan_stale_worker_recovery(
                session,
                stale_after=timedelta(minutes=5),
                retry_policy=policy,
                now=NOW,
                limit=1,
            )
            assert plan.total_recoverable == expected_total
            planned_ids = tuple(item.run_id for item in plan.batch_candidates)
            recovered = apply_stale_worker_recovery(
                session,
                stale_after=timedelta(minutes=5),
                retry_policy=policy,
                now=NOW,
                candidate_run_ids=planned_ids,
                limit=1,
            )
            assert recovered == planned_ids
            recovered_batches.append(recovered)

        final_plan = plan_stale_worker_recovery(
            session,
            stale_after=timedelta(minutes=5),
            retry_policy=policy,
            now=NOW,
            limit=1,
        )
        assert final_plan.total_recoverable == 0
        assert apply_stale_worker_recovery(
            session,
            stale_after=timedelta(minutes=5),
            retry_policy=policy,
            now=NOW,
            candidate_run_ids=(),
            limit=1,
        ) == ()
        assert [len(batch) for batch in recovered_batches] == [1, 1]
        for job, run in jobs_and_runs:
            assert run.status == "timed_out"
            assert run.retryable is True
            assert job.status == "retry_scheduled"
            assert session.get(WorkerLease, job.source_id) is None
        assert recovery_invariants(session, now=NOW) == {
            "public_ready_not_succeeded": 0,
            "public_ready_not_complete": 0,
            "public_ready_with_unresolved_replay": 0,
            "public_ready_with_failed_replay": 0,
            "active_enrichment_runs": 0,
            "running_worker_jobs": 0,
            "running_worker_runs": 0,
        }
        session.rollback()


def test_cli_worker_batches_commit_one_recovery_per_fresh_session():
    connection = engine.connect()
    outer_transaction = connection.begin()
    made_sessions: list[Session] = []
    factory = sessionmaker(
        bind=connection,
        join_transaction_mode="create_savepoint",
    )

    def tracked_session():
        session = factory()
        made_sessions.append(session)
        return session

    try:
        suffix = uuid4().hex[:10]
        with tracked_session() as session:
            first_job, first_run, _lease = _stale_worker(
                session, f"{suffix}-first"
            )
            second_job, second_run, _lease = _stale_worker(
                session, f"{suffix}-second"
            )
            worker_ids = (first_run.id, second_run.id)
            job_ids = (first_job.id, second_job.id)
            session.commit()

        result = _apply_worker_recovery_batches(
            stale_after=timedelta(minutes=5),
            retry_policy=RetryPolicy(),
            batch_size=1,
            now=NOW,
            session_factory=tracked_session,
        )

        assert result["initial_total_stale"] == 2
        assert result["initial_total_recoverable"] == 2
        assert [batch["recovered"] for batch in result["batches"]] == [1, 1]
        assert all(
            batch["planned_run_ids"] == batch["recovered_run_ids"]
            for batch in result["batches"]
        )
        # Seed, two mutating batch sessions, then a final empty-plan session.
        assert len(made_sessions) == 4
        with tracked_session() as session:
            assert set(
                session.scalars(
                    sa.select(WorkerRun.id).where(
                        WorkerRun.id.in_(worker_ids),
                        WorkerRun.status == "timed_out",
                    )
                )
            ) == set(worker_ids)
            assert set(
                session.scalars(
                    sa.select(WorkerJob.id).where(
                        WorkerJob.id.in_(job_ids),
                        WorkerJob.status == "retry_scheduled",
                    )
                )
            ) == set(job_ids)
            assert recovery_invariants(session, now=NOW) == {
                "public_ready_not_succeeded": 0,
                "public_ready_not_complete": 0,
                "public_ready_with_unresolved_replay": 0,
                "public_ready_with_failed_replay": 0,
                "active_enrichment_runs": 0,
                "running_worker_jobs": 0,
                "running_worker_runs": 0,
            }
    finally:
        outer_transaction.rollback()
        connection.close()


def test_legacy_recovery_completes_proven_run_and_parks_failed_replay():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        source = _source(session, suffix)
        dataset = _dataset(
            session,
            source=source,
            code=f"legacy-{suffix}",
            applicability={"entity_types": ["legal"]},
        )
        company = _company(session, suffix)
        run, risk, summary = _run(
            session, company=company, dataset=dataset, suffix=suffix
        )
        plan = plan_enrichment_recovery_batch(
            session, after_cursor=_cursor_before(company), limit=1, now=NOW
        )
        assert plan.items[0].action == "RECONCILE_TO_COMPLETE"
        applied = apply_enrichment_recovery_batch(
            session, after_cursor=_cursor_before(company), limit=1, now=NOW
        )
        assert applied.changed == 1
        assert (run.status, run.stage, run.public_ready) == (
            "succeeded",
            "complete",
            True,
        )
        repeated = apply_enrichment_recovery_batch(
            session, after_cursor=_cursor_before(company), limit=1, now=NOW
        )
        assert repeated.changed == 0

        signal = _signal(
            session,
            company=company,
            dataset=dataset,
            suffix=suffix,
            status="failed",
        )
        plan = plan_enrichment_recovery_batch(
            session, after_cursor=_cursor_before(company), limit=1, now=NOW
        )
        assert plan.items[0].action == "BLOCKED_FAILED_REPLAY"
        historical = (risk.id, summary.id)
        applied = apply_enrichment_recovery_batch(
            session, after_cursor=_cursor_before(company), limit=1, now=NOW
        )
        assert applied.changed == 1
        assert run.status == "cancelled"
        assert run.stage == "failed"
        assert run.public_ready is False
        assert run.risk_assessment_id is None
        assert run.summary_id is None
        assert session.get(CompanyRiskAssessmentV3, historical[0]) is not None
        assert session.get(CompanySummaryV3, historical[1]) is not None
        assert signal.status == "failed"
        session.rollback()


def test_pending_replay_is_preserved_while_legacy_run_is_parked():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        source = _source(session, suffix)
        dataset = _dataset(
            session,
            source=source,
            code=f"pending-replay-{suffix}",
            applicability={"entity_types": ["legal"]},
        )
        company = _company(session, suffix)
        run, _risk, _summary = _run(
            session, company=company, dataset=dataset, suffix=suffix
        )
        signal = _signal(
            session,
            company=company,
            dataset=dataset,
            suffix=suffix,
            status="pending",
        )

        plan = plan_enrichment_recovery_batch(
            session, after_cursor=_cursor_before(company), limit=1, now=NOW
        )
        assert plan.items[0].pending_replay_blockers == 1
        assert plan.items[0].action == "INVALIDATE_AND_PARK"
        applied = apply_enrichment_recovery_batch(
            session, after_cursor=_cursor_before(company), limit=1, now=NOW
        )
        assert applied.changed == 1
        assert (run.status, run.stage, run.public_ready) == (
            "cancelled",
            "failed",
            False,
        )
        assert signal.status == "pending"
        session.rollback()


def test_unknown_applicability_parks_without_creating_work():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        source = _source(session, suffix)
        dataset = _dataset(
            session,
            source=source,
            code=f"unknown-applicability-{suffix}",
            applicability=None,
        )
        company = _company(session, suffix)
        run, _risk, _summary = _run(
            session, company=company, dataset=dataset, suffix=suffix
        )
        coverage = session.scalar(
            sa.select(CompanySourceCoverage).where(
                CompanySourceCoverage.enrichment_run_id == run.id
            )
        )
        coverage.status = "APPLICABILITY_UNKNOWN"
        coverage.execution_status = "blocked"
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )

        plan = plan_enrichment_recovery_batch(
            session, after_cursor=_cursor_before(company), limit=1, now=NOW
        )
        assert plan.items[0].applicability_blocker_count == 1
        assert plan.items[0].action == "BLOCKED_APPLICABILITY"
        applied = apply_enrichment_recovery_batch(
            session, after_cursor=_cursor_before(company), limit=1, now=NOW
        )
        assert applied.changed == 1
        assert run.status == "cancelled"
        assert run.public_ready is False
        assert int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        ) == jobs_before
        session.rollback()


def test_terminal_semantic_status_with_failed_execution_is_not_completed():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        source = _source(session, suffix)
        dataset = _dataset(
            session,
            source=source,
            code=f"failed-execution-{suffix}",
            applicability={"entity_types": ["legal"]},
        )
        company = _company(session, suffix)
        run, risk, summary = _run(
            session, company=company, dataset=dataset, suffix=suffix
        )
        coverage = session.scalar(
            sa.select(CompanySourceCoverage).where(
                CompanySourceCoverage.enrichment_run_id == run.id
            )
        )
        coverage.execution_status = "failed"
        session.flush()

        plan = plan_enrichment_recovery_batch(
            session, after_cursor=_cursor_before(company), limit=1, now=NOW
        )
        assert plan.items[0].coverage_terminal_count == 0
        assert plan.items[0].coverage_failed_count == 1
        assert plan.items[0].action == "INVALIDATE_AND_PARK"
        apply_enrichment_recovery_batch(
            session, after_cursor=_cursor_before(company), limit=1, now=NOW
        )
        assert run.status == "cancelled"
        assert run.public_ready is False
        assert session.get(CompanyRiskAssessmentV3, risk.id) is not None
        assert session.get(CompanySummaryV3, summary.id) is not None
        session.rollback()


def test_not_applicable_replay_reconciles_exact_uuid_without_jobs():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        source = _source(session, suffix)
        dataset = _dataset(
            session,
            source=source,
            code=f"not-applicable-{suffix}",
            applicability={"entity_types": ["legal"]},
        )
        company = _company(
            session, suffix, entity_type="individual_entrepreneur"
        )
        run, _risk, _summary = _run(
            session,
            company=company,
            dataset=dataset,
            suffix=suffix,
            status="succeeded",
            stage="complete",
        )
        existing = session.scalar(
            sa.select(CompanySourceCoverage).where(
                CompanySourceCoverage.enrichment_run_id == run.id
            )
        )
        session.delete(existing)
        run.source_count = 0
        run.completed_source_count = 0
        pending = _signal(
            session,
            company=company,
            dataset=dataset,
            suffix=suffix,
            status="pending",
        )
        failed = _signal(
            session,
            company=company,
            dataset=dataset,
            suffix=f"{suffix}-failed",
            status="failed",
        )
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )
        first = reconcile_not_applicable_replay_batch(session, limit=10, now=NOW)
        coverage = session.scalar(
            sa.select(CompanySourceCoverage).where(
                CompanySourceCoverage.enrichment_run_id == run.id,
                CompanySourceCoverage.source_id == dataset.code,
            )
        )
        assert first == {
            "signals_reconciled": 2,
            "signals_terminalized": 1,
            "coverage_created": 1,
        }
        assert pending.status == "complete"
        assert failed.status == "failed"
        assert coverage.status == "NOT_APPLICABLE"
        assert coverage.execution_status == "succeeded"
        assert set(coverage.master_replay_signal_ids) == {
            str(pending.id),
            str(failed.id),
        }
        assert int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        ) == jobs_before
        assert reconcile_not_applicable_replay_batch(
            session, limit=10, now=NOW
        ) == {
            "signals_reconciled": 0,
            "signals_terminalized": 0,
            "coverage_created": 0,
        }
        session.rollback()


def test_enrichment_recovery_batches_resume_in_stable_company_order():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        source = _source(session, suffix)
        dataset = _dataset(
            session,
            source=source,
            code=f"bounded-{suffix}",
            applicability={"entity_types": ["legal"]},
        )
        first_company = _company(session, f"{suffix}-first")
        second_company = _company(session, f"{suffix}-second")
        first_run, _risk, _summary = _run(
            session,
            company=first_company,
            dataset=dataset,
            suffix=f"{suffix}-first",
        )
        second_run, _risk, _summary = _run(
            session,
            company=second_company,
            dataset=dataset,
            suffix=f"{suffix}-second",
        )

        first = apply_enrichment_recovery_batch(
            session,
            after_cursor=_cursor_before(first_company),
            limit=1,
            now=NOW,
        )
        assert first.scanned == first.changed == 1
        assert first.next_cursor is not None
        assert first.next_cursor.company_id == first_company.id
        assert first_run.status == "succeeded"
        assert second_run.status == "running"

        second = apply_enrichment_recovery_batch(
            session,
            after_cursor=first.next_cursor,
            limit=1,
            now=NOW,
        )
        assert second.scanned == second.changed == 1
        assert second.next_cursor is not None
        assert second.next_cursor.company_id == second_company.id
        assert second_run.status == "succeeded"

        repeated = apply_enrichment_recovery_batch(
            session,
            after_cursor=_cursor_before(first_company),
            limit=2,
            now=NOW,
        )
        assert repeated.scanned == 2
        assert repeated.changed == 0
        assert {item.action for item in repeated.items} == {"NO_CHANGE"}
        session.rollback()


def test_older_active_run_is_recovered_when_newer_run_is_cancelled():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        source = _source(session, suffix)
        dataset = _dataset(
            session,
            source=source,
            code=f"older-active-{suffix}",
            applicability={"entity_types": ["legal"]},
        )
        company = _company(session, suffix)
        older, _risk, _summary = _run(
            session,
            company=company,
            dataset=dataset,
            suffix=f"{suffix}-older",
            created_at=NOW - timedelta(hours=3),
        )
        newer, _risk, _summary = _run(
            session,
            company=company,
            dataset=dataset,
            suffix=f"{suffix}-newer",
            status="cancelled",
            stage="failed",
            public_ready=False,
            created_at=NOW - timedelta(hours=1),
        )

        plan = plan_enrichment_recovery_batch(
            session,
            after_cursor=_cursor_before(company),
            limit=1,
            now=NOW,
        )
        assert [item.run_id for item in plan.items] == [older.id]
        applied = apply_enrichment_recovery_batch(
            session,
            after_cursor=_cursor_before(company),
            limit=1,
            now=NOW,
        )
        assert applied.changed == 1
        assert older.status not in {
            "pending",
            "waiting_sources",
            "retry_scheduled",
            "running",
        }
        assert newer.status == "cancelled"
        assert recovery_invariants(session, now=NOW) == {
            "public_ready_not_succeeded": 0,
            "public_ready_not_complete": 0,
            "public_ready_with_unresolved_replay": 0,
            "public_ready_with_failed_replay": 0,
            "active_enrichment_runs": 0,
            "running_worker_jobs": 0,
            "running_worker_runs": 0,
        }
        session.rollback()


def test_same_company_active_runs_cross_run_cursor_without_skip_and_repeat_noop():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        source = _source(session, suffix)
        dataset = _dataset(
            session,
            source=source,
            code=f"same-company-batches-{suffix}",
            applicability={"entity_types": ["legal"]},
        )
        company = _company(session, suffix)
        first_run, _risk, _summary = _run(
            session,
            company=company,
            dataset=dataset,
            suffix=f"{suffix}-a",
            created_at=NOW - timedelta(hours=4),
        )
        second_run, _risk, _summary = _run(
            session,
            company=company,
            dataset=dataset,
            suffix=f"{suffix}-b",
            created_at=NOW - timedelta(hours=3),
        )
        _newest, _risk, _summary = _run(
            session,
            company=company,
            dataset=dataset,
            suffix=f"{suffix}-c",
            status="cancelled",
            stage="failed",
            public_ready=False,
            created_at=NOW - timedelta(hours=2),
        )

        cursor = _cursor_before(company)
        handled: list = []
        changed_per_batch: list[int] = []
        while True:
            batch = apply_enrichment_recovery_batch(
                session,
                after_cursor=cursor,
                limit=1,
                now=NOW,
            )
            if not batch.items:
                break
            assert batch.scanned == 1
            handled.append(batch.items[0].run_id)
            changed_per_batch.append(batch.changed)
            cursor = batch.next_cursor

        assert handled == [first_run.id, second_run.id]
        assert changed_per_batch == [1, 1]
        assert recovery_invariants(session, now=NOW)["active_enrichment_runs"] == 0

        cursor = _cursor_before(company)
        second_apply_changed = 0
        while True:
            batch = apply_enrichment_recovery_batch(
                session,
                after_cursor=cursor,
                limit=1,
                now=NOW,
            )
            if not batch.items:
                break
            second_apply_changed += batch.changed
            cursor = batch.next_cursor
        assert second_apply_changed == 0
        session.rollback()


def test_multiple_companies_and_runs_have_stable_run_level_pagination():
    with Session(engine) as session:
        suffix = uuid4().hex[:10]
        source = _source(session, suffix)
        dataset = _dataset(
            session,
            source=source,
            code=f"multi-company-batches-{suffix}",
            applicability={"entity_types": ["legal"]},
        )
        companies = (
            _company(session, f"{suffix}-first"),
            _company(session, f"{suffix}-second"),
        )
        expected: list = []
        for company_index, company in enumerate(companies):
            for run_index in range(2):
                run, _risk, _summary = _run(
                    session,
                    company=company,
                    dataset=dataset,
                    suffix=f"{suffix}-{company_index}-{run_index}",
                    created_at=NOW
                    - timedelta(hours=4 - company_index - run_index),
                )
                expected.append(run.id)

        cursor = _cursor_before(companies[0])
        handled: list = []
        while True:
            batch = apply_enrichment_recovery_batch(
                session,
                after_cursor=cursor,
                limit=1,
                now=NOW,
            )
            if not batch.items:
                break
            assert batch.scanned <= 1
            assert batch.changed <= 1
            handled.append(batch.items[0].run_id)
            cursor = batch.next_cursor

        assert handled == expected
        assert len(set(handled)) == len(expected)
        assert recovery_invariants(session, now=NOW) == {
            "public_ready_not_succeeded": 0,
            "public_ready_not_complete": 0,
            "public_ready_with_unresolved_replay": 0,
            "public_ready_with_failed_replay": 0,
            "active_enrichment_runs": 0,
            "running_worker_jobs": 0,
            "running_worker_runs": 0,
        }
        session.rollback()
