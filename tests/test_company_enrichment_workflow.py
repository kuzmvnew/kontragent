from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import threading
import time
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.database.postgres import engine
from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichmentRun, CompanySourceCoverage
from app.models.registry_master import CompanyRegistryChange, MasterReplaySignal
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from app.models.source import DataSet, DataSource
from app.models.worker import (
    WorkerHandlerRegistration,
    WorkerJob,
    WorkerPublicationState,
    WorkerRun,
)
from app.services import company_enrichment_service as enrichment_service
from app.services import factory_generation_service
from app.services import publication_service
from app.services.company_enrichment_service import (
    _enrichment_refill_capacity,
    _semantic_coverage,
    canonical_enrichment_metrics,
    consume_master_replay_signals,
    get_company_public_readiness,
    prioritize_public_cohort_enrichment,
    reconcile_enrichment_run,
    recover_legacy_replay_denominators,
    restart_enrichment_run,
    seed_existing_master_enrichment,
)
from app.services.factory_metrics_service import collect_factory_metrics
from app.services.factory_scale_service import collect_factory_pressure
from app.services.publication_service import publishable_runs


NOW = datetime(2026, 9, 26, 8, tzinfo=timezone.utc)


def test_enrichment_refill_batches_snapshot_replay_work():
    assert _enrichment_refill_capacity(100) == 0
    assert _enrichment_refill_capacity(99) == 0
    assert _enrichment_refill_capacity(51) == 0
    assert _enrichment_refill_capacity(50) == 50
    assert _enrichment_refill_capacity(0) == 100


def test_semantic_coverage_excludes_not_applicable_and_fails_closed():
    resolved, terminal, complete, percent = _semantic_coverage(
        ["FOUND", "NOT_FOUND", "NOT_APPLICABLE"],
        frozen_expected=3,
        run_status="succeeded",
    )
    assert (resolved, terminal, complete, percent) == (2, 3, True, 100.0)

    resolved, terminal, complete, percent = _semantic_coverage(
        ["FOUND", "NOT_FOUND", "NOT_APPLICABLE", "STALE_DATA"],
        frozen_expected=4,
        run_status="waiting_sources",
    )
    assert resolved == 2
    assert terminal == 3
    assert complete is False
    assert round(percent, 4) == 66.6667


def _dataset(
    *,
    source_id: int,
    code: str,
    update_mode: str,
    dataset_kind: str,
    operational: bool = True,
) -> DataSet:
    return DataSet(
        source_id=source_id,
        code=code,
        name=code,
        domain="test",
        update_mode=update_mode,
        data_format="json",
        enabled=True,
        dataset_kind=dataset_kind,
        freshness_policy="irregular",
        last_success_at=NOW if operational else None,
        last_data_date=NOW.date(),
        source_as_of=NOW,
        retrieved_at=NOW,
        checked_at=NOW,
        official_actual_until=NOW.date() + timedelta(days=30),
        published_at=NOW,
        record_count=1,
        coverage={
            "operational_accepted": operational,
            "successful_scheduled_checks": 2 if operational else 0,
        },
        applicability={"entity_types": ["legal"]},
        operational_status="current" if operational else "not_configured",
        auto_update_status="configured" if operational else "not_configured",
    )


def _worker_job(
    *,
    source_id: str,
    handler_version: str,
    metadata: dict,
    status: str = "succeeded",
) -> WorkerJob:
    return WorkerJob(
        source_id=source_id,
        job_type="fixture_seed",
        handler_version=handler_version,
        schedule_metadata=metadata,
        idempotency_key=f"fixture:{source_id}:{uuid4()}",
        status=status,
        max_attempts=4,
        timeout_seconds=7200,
        next_attempt_at=None,
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(hours=1),
    )


def _seed_workflow(session: Session, tmp_path: Path):
    suffix = uuid4().hex[:10]
    source = DataSource(
        code=f"enrich_{suffix}",
        name="Enrichment test",
        source_type="official",
        enabled=True,
    )
    company = Company(
        inn=str(uuid4().int)[:10],
        name="Enrichment workflow company",
        entity_type="legal",
        status="ACTIVE",
    )
    session.add_all((source, company))
    session.flush()

    bulk_code = f"bulk_{suffix}"
    point_code = f"point_{suffix}"
    dormant_code = f"dormant_{suffix}"
    bulk = _dataset(
        source_id=source.id,
        code=bulk_code,
        update_mode="bulk",
        dataset_kind="bulk_snapshot",
    )
    point = _dataset(
        source_id=source.id,
        code=point_code,
        update_mode="api",
        dataset_kind="on_demand_api",
    )
    dormant = _dataset(
        source_id=source.id,
        code=dormant_code,
        update_mode="bulk",
        dataset_kind="bulk_snapshot",
        operational=False,
    )
    session.add_all((bulk, point, dormant))
    session.flush()

    bulk_version = "fixture-bulk-v1"
    point_version = "fixture-point-v1"
    session.add_all(
        (
            WorkerHandlerRegistration(
                source_id=bulk_code,
                handler_version=bulk_version,
                approved=True,
                enabled=True,
                live_mode=False,
                metadata_json={"mode": "official_bulk_release"},
            ),
            WorkerHandlerRegistration(
                source_id=point_code,
                handler_version=point_version,
                approved=True,
                enabled=True,
                live_mode=False,
                metadata_json={"mode": "bounded_daily_master_exact_inn_sweep"},
            ),
            WorkerHandlerRegistration(
                source_id=dormant_code,
                handler_version=bulk_version,
                approved=True,
                enabled=True,
                live_mode=False,
                metadata_json={"mode": "official_bulk_release"},
            ),
        )
    )
    normalized = tmp_path / f"{bulk_code}.jsonl"
    normalized.write_text("{}\n", encoding="utf-8")
    replay_checksum = "a" * 64
    producer = _worker_job(
        source_id=bulk_code,
        handler_version=bulk_version,
        metadata={
            "source_page_url": "https://example.invalid/passport",
            "artifact_url": "https://example.invalid/archive.zip",
            "xsd_url": "https://example.invalid/schema.xsd",
            "source_data_date": NOW.date().isoformat(),
            "actual_until": (NOW.date() + timedelta(days=30)).isoformat(),
            "discovered_at": NOW.isoformat(),
            "provenance": "official_test_fixture",
            "release_identity": f"release-{suffix}",
            "raw_root": str(tmp_path),
        },
    )
    point_seed = _worker_job(
        source_id=point_code,
        handler_version=point_version,
        metadata={"raw_root": str(tmp_path), "inn": company.inn},
    )
    session.add_all((producer, point_seed))
    session.flush()
    producer_run = WorkerRun(
        job_id=producer.id,
        attempt_no=1,
        started_at=NOW - timedelta(hours=1),
        finished_at=NOW - timedelta(minutes=59),
        status="succeeded",
        worker_id="fixture-worker",
        fencing_token=1,
        handler_version=bulk_version,
        current_stage="complete",
        errors=[],
        checksum_metadata={},
        heartbeat_at=NOW - timedelta(minutes=59),
        duration_ms=60_000,
        retryable=False,
    )
    session.add(producer_run)
    session.flush()
    session.add(
        WorkerPublicationState(
            source_id=bulk_code,
            active_pointer=normalized.as_uri(),
            rollback_pointer=None,
            generation=7,
            last_fencing_token=1,
            published_by_run_id=producer_run.id,
            validation_metadata={
                "checksum": replay_checksum,
                "validation": {
                    "release_identity": f"release-{suffix}",
                    "source_data_date": NOW.date().isoformat(),
                },
            },
            updated_at=NOW,
        )
    )
    change = CompanyRegistryChange(
        source_id=f"master_{suffix}",
        company_id=company.id,
        run_id=None,
        inn=company.inn,
        event_type="created",
        changed_fields={"created": True},
        source_data_date=NOW.date(),
        source_record_key=f"record:{suffix}",
    )
    session.add(change)
    session.flush()
    signals = {
        code: MasterReplaySignal(
            company_id=company.id,
            target_source_id=code,
            registry_change_id=change.id,
            status="pending",
            created_at=NOW,
        )
        for code in (bulk_code, point_code, dormant_code)
    }
    session.add_all(signals.values())
    session.flush()
    return company, signals


def _workflow_rows(session: Session, company_id: int):
    run = session.scalar(
        sa.select(CompanyEnrichmentRun).where(
            CompanyEnrichmentRun.company_id == company_id
        )
    )
    coverage = tuple(
        session.scalars(
            sa.select(CompanySourceCoverage)
            .where(CompanySourceCoverage.enrichment_run_id == run.id)
            .order_by(CompanySourceCoverage.mode)
        )
    )
    return run, coverage


def _set_signal_policy(
    session: Session,
    signals: dict[str, MasterReplaySignal],
    *,
    mode: str,
) -> str:
    signal = next(
        item
        for code, item in signals.items()
        if session.scalar(sa.select(DataSet.update_mode).where(DataSet.code == code))
        == mode
    )
    dataset = session.scalar(
        sa.select(DataSet).where(DataSet.code == signal.target_source_id)
    )
    dataset.applicability = None
    session.flush()
    return dataset.code


def _legacy_ready_run_with_replay_outside_coverage(
    session: Session,
    tmp_path: Path,
    *,
    applicability: dict | None,
):
    """Build the pre-correction-02 persisted shape on the migrated schema."""

    company, signals = _seed_workflow(session, tmp_path)
    replay = next(
        signal
        for code, signal in signals.items()
        if session.scalar(sa.select(DataSet.update_mode).where(DataSet.code == code))
        == "api"
    )
    replay_dataset = session.scalar(
        sa.select(DataSet).where(DataSet.code == replay.target_source_id)
    )
    replay_dataset.last_success_at = None
    replay_dataset.coverage = {"operational_accepted": False}
    replay_dataset.operational_status = "not_configured"
    replay_dataset.auto_update_status = "not_configured"
    session.flush()

    consumed = consume_master_replay_signals(session, limit=10, now=NOW)
    run, coverage = _workflow_rows(session, company.id)
    assert consumed.runs_created == 1
    assert run.source_count == 1
    assert len(coverage) == 1
    session.get(WorkerJob, coverage[0].worker_job_id).status = "succeeded"
    completed = reconcile_enrichment_run(
        session, run.id, now=NOW + timedelta(minutes=1)
    )
    assert completed.status == "succeeded"
    assert completed.stage == "complete"
    assert completed.public_ready is True
    assert completed.risk_assessment_id
    assert completed.summary_id

    replay_dataset.last_success_at = NOW
    replay_dataset.coverage = {"operational_accepted": True}
    replay_dataset.operational_status = "current"
    replay_dataset.auto_update_status = "configured"
    replay_dataset.applicability = applicability
    session.flush()
    assert replay.status == "pending"
    assert session.scalar(
        sa.select(sa.func.count())
        .select_from(CompanySourceCoverage)
        .where(
            CompanySourceCoverage.enrichment_run_id == run.id,
            CompanySourceCoverage.source_id == replay.target_source_id,
        )
    ) == 0
    return company, signals, run, replay_dataset, replay


def test_mixed_applicable_unknown_is_a_durable_non_actionable_denominator_blocker(
    tmp_path,
):
    with Session(engine) as session:
        baseline_metrics = collect_factory_metrics(
            session, window_hours=1, now=NOW, enforce_read_only=False
        )
        company, signals = _seed_workflow(session, tmp_path)
        unknown_code = _set_signal_policy(session, signals, mode="api")
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )

        result = consume_master_replay_signals(session, limit=10, now=NOW)
        run, coverage = _workflow_rows(session, company.id)
        blocker = next(row for row in coverage if row.source_id == unknown_code)
        actionable = next(row for row in coverage if row.source_id != unknown_code)

        assert result.signals_seen == 2
        assert result.signals_scheduled == 1
        assert result.runs_created == 1
        assert result.jobs_created == 1
        assert run.source_count == 2
        assert blocker.status == "APPLICABILITY_UNKNOWN"
        assert blocker.execution_status == "blocked"
        assert blocker.worker_job_id is None
        assert blocker.dataset_id is not None
        assert blocker.source_snapshot["applicability"] == "UNKNOWN"
        assert blocker.source_snapshot["applicability_policy"] is None
        assert blocker.master_replay_signal_ids == [str(signals[unknown_code].id)]
        assert signals[unknown_code].status == "pending"
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            - jobs_before
        ) == 1

        session.get(WorkerJob, actionable.worker_job_id).status = "succeeded"
        blocked = reconcile_enrichment_run(session, run.id, now=NOW)
        session.flush()
        assert blocked.status == "waiting_sources"
        assert blocked.stage == "source_enrichment"
        assert blocked.last_error_code == "applicability_unknown"
        assert blocked.completed_source_count == 1
        assert blocked.risk_assessment_id is None
        assert blocked.summary_id is None
        assert blocked.public_ready is False
        assert get_company_public_readiness(session, company.id)[
            "applicability_blocker_count"
        ] == 1
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(CompanyRiskAssessmentV3)
            .where(CompanyRiskAssessmentV3.company_id == company.id)
        ) == 0
        metrics = collect_factory_metrics(
            session, window_hours=1, now=NOW, enforce_read_only=False
        )
        assert metrics["totals"]["fully_enriched"] == baseline_metrics["totals"][
            "fully_enriched"
        ]
        assert metrics["totals"]["public_ready"] == baseline_metrics["totals"][
            "public_ready"
        ]
        assert metrics["queues"]["applicability_blockers"] == (
            baseline_metrics["queues"]["applicability_blockers"] + 1
        )

        session.expire_all()
        persisted = session.scalar(
            sa.select(CompanySourceCoverage).where(
                CompanySourceCoverage.enrichment_run_id == run.id,
                CompanySourceCoverage.source_id == unknown_code,
            )
        )
        assert persisted.status == "APPLICABILITY_UNKNOWN"
        assert persisted.execution_status == "blocked"
        assert persisted.worker_job_id is None
        session.rollback()


def test_found_applicable_source_is_still_blocked_by_unknown(tmp_path):
    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        unknown_code = _set_signal_policy(session, signals, mode="api")
        consume_master_replay_signals(session, limit=10, now=NOW)
        run, coverage = _workflow_rows(session, company.id)
        actionable = next(row for row in coverage if row.source_id != unknown_code)
        job = session.get(WorkerJob, actionable.worker_job_id)
        job.status = "succeeded"
        session.add(
            WorkerRun(
                job_id=job.id,
                attempt_no=1,
                started_at=NOW,
                finished_at=NOW + timedelta(seconds=1),
                status="succeeded",
                worker_id="found-regression-worker",
                fencing_token=1,
                handler_version=job.handler_version,
                current_stage="complete",
                records_written=1,
                records_published=1,
                errors=[],
                checksum_metadata={},
                heartbeat_at=NOW + timedelta(seconds=1),
                duration_ms=1_000,
                retryable=False,
            )
        )
        session.flush()

        blocked = reconcile_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=1)
        )
        assert actionable.status == "FOUND"
        assert blocked.status == "waiting_sources"
        assert blocked.last_error_code == "applicability_unknown"
        assert blocked.risk_assessment_id is None
        assert blocked.summary_id is None
        assert blocked.public_ready is False
        session.rollback()


def test_pure_and_multiple_unknown_sources_create_no_worker_jobs(tmp_path):
    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        for code in signals:
            dataset = session.scalar(sa.select(DataSet).where(DataSet.code == code))
            if dataset.last_success_at is not None:
                dataset.applicability = None
        session.flush()
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )
        pressure_before = collect_factory_pressure(
            session, raw_root=tmp_path, now=NOW
        )

        first = consume_master_replay_signals(session, limit=10, now=NOW)
        repeated = consume_master_replay_signals(session, limit=10, now=NOW)
        run, coverage = _workflow_rows(session, company.id)

        assert first.signals_seen == 2
        assert first.signals_scheduled == 0
        assert first.runs_created == 1
        assert first.jobs_created == 0
        assert repeated.runs_created == 0
        assert repeated.jobs_created == 0
        assert run.source_count == 2
        assert {row.status for row in coverage} == {"APPLICABILITY_UNKNOWN"}
        assert {row.execution_status for row in coverage} == {"blocked"}
        assert {row.worker_job_id for row in coverage} == {None}
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            == jobs_before
        )
        pressure_after = collect_factory_pressure(
            session, raw_root=tmp_path, now=NOW
        )
        assert pressure_after.actionable_backlog == pressure_before.actionable_backlog
        assert {signal.status for signal in signals.values() if signal.target_source_id in {
            row.source_id for row in coverage
        }} == {"pending"}

        resolved_code = next(
            row.source_id for row in coverage if row.mode == "point_check"
        )
        dataset = session.scalar(
            sa.select(DataSet).where(DataSet.code == resolved_code)
        )
        dataset.applicability = {"entity_types": ["legal"]}
        session.flush()
        still_blocked = reconcile_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=1)
        )
        session.flush()
        refreshed_rows = tuple(
            session.scalars(
                sa.select(CompanySourceCoverage).where(
                    CompanySourceCoverage.enrichment_run_id == run.id
                )
            )
        )
        assert sum(
            row.status == "APPLICABILITY_UNKNOWN" for row in refreshed_rows
        ) == 1
        resolved = next(
            row for row in refreshed_rows if row.source_id == resolved_code
        )
        assert resolved.execution_status == "queued"
        assert resolved.worker_job_id is not None
        assert still_blocked.last_error_code == "applicability_unknown"
        assert still_blocked.risk_assessment_id is None
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            == jobs_before + 1
        )
        session.rollback()


def test_unknown_blocker_survives_commit_and_new_session(tmp_path):
    company_id = source_id = run_id = None
    source_codes: tuple[str, ...] = ()
    try:
        with Session(engine) as session:
            company, signals = _seed_workflow(session, tmp_path)
            unknown_code = _set_signal_policy(session, signals, mode="api")
            consume_master_replay_signals(session, limit=10, now=NOW)
            run, coverage = _workflow_rows(session, company.id)
            company_id = company.id
            run_id = run.id
            source_codes = tuple(signals)
            source_id = session.scalar(
                sa.select(DataSet.source_id).where(DataSet.code == unknown_code)
            )
            blocker = next(row for row in coverage if row.source_id == unknown_code)
            assert blocker.status == "APPLICABILITY_UNKNOWN"
            session.commit()

        with Session(engine) as restarted:
            run = restarted.get(CompanyEnrichmentRun, run_id)
            blocker = restarted.scalar(
                sa.select(CompanySourceCoverage).where(
                    CompanySourceCoverage.enrichment_run_id == run_id,
                    CompanySourceCoverage.status == "APPLICABILITY_UNKNOWN",
                )
            )
            assert run.status == "waiting_sources"
            assert run.last_error_code == "applicability_unknown"
            assert run.public_ready is False
            assert blocker is not None
            assert blocker.execution_status == "blocked"
            assert blocker.worker_job_id is None
            assert restarted.scalar(
                sa.select(MasterReplaySignal.status).where(
                    MasterReplaySignal.id
                    == UUID(blocker.master_replay_signal_ids[0])
                )
            ) == "pending"
    finally:
        if company_id is not None and source_id is not None:
            with Session(engine) as cleanup:
                cleanup.execute(
                    sa.delete(Company).where(Company.id == company_id)
                )
                cleanup.execute(
                    sa.delete(WorkerPublicationState).where(
                        WorkerPublicationState.source_id.in_(source_codes)
                    )
                )
                job_ids = tuple(
                    cleanup.scalars(
                        sa.select(WorkerJob.id).where(
                            WorkerJob.source_id.in_(source_codes)
                        )
                    )
                )
                if job_ids:
                    cleanup.execute(
                        sa.delete(WorkerRun).where(WorkerRun.job_id.in_(job_ids))
                    )
                    cleanup.execute(
                        sa.delete(WorkerJob).where(WorkerJob.id.in_(job_ids))
                    )
                cleanup.execute(
                    sa.delete(WorkerHandlerRegistration).where(
                        WorkerHandlerRegistration.source_id.in_(source_codes)
                    )
                )
                cleanup.execute(
                    sa.delete(DataSource).where(DataSource.id == source_id)
                )
                cleanup.commit()


def test_unknown_resolves_to_not_applicable_without_worker_work(tmp_path):
    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        unknown_code = _set_signal_policy(session, signals, mode="api")
        consume_master_replay_signals(session, limit=10, now=NOW)
        run, coverage = _workflow_rows(session, company.id)
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )
        dataset = session.scalar(
            sa.select(DataSet).where(DataSet.code == unknown_code)
        )
        dataset.applicability = {
            "entity_types": ["individual_entrepreneur"]
        }
        session.flush()

        refreshed = reconcile_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=1)
        )
        blocker = next(row for row in coverage if row.source_id == unknown_code)
        assert blocker.status == "NOT_APPLICABLE"
        assert blocker.execution_status == "succeeded"
        assert blocker.worker_job_id is None
        assert blocker.source_snapshot["applicability"] == "NOT_APPLICABLE"
        assert blocker.source_snapshot["applicability_resolution"]["from"] == "UNKNOWN"
        assert signals[unknown_code].status == "complete"
        assert refreshed.last_error_code != "applicability_unknown"
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            == jobs_before
        )
        session.rollback()


def test_unknown_resolves_to_applicable_before_risk_and_enqueues_once(tmp_path):
    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        unknown_code = _set_signal_policy(session, signals, mode="api")
        consume_master_replay_signals(session, limit=10, now=NOW)
        run, coverage = _workflow_rows(session, company.id)
        applicable = next(row for row in coverage if row.source_id != unknown_code)
        session.get(WorkerJob, applicable.worker_job_id).status = "succeeded"
        dataset = session.scalar(
            sa.select(DataSet).where(DataSet.code == unknown_code)
        )
        dataset.applicability = {"entity_types": ["legal"]}
        session.flush()
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )

        waiting = reconcile_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=1)
        )
        converted = next(row for row in coverage if row.source_id == unknown_code)
        assert converted.status == "RUNNING"
        assert converted.execution_status == "queued"
        assert converted.worker_job_id is not None
        assert converted.source_snapshot["applicability"] == "APPLICABLE"
        assert signals[unknown_code].status == "scheduled"
        assert waiting.stage == "source_enrichment"
        assert waiting.public_ready is False
        assert waiting.risk_assessment_id is None
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            == jobs_before + 1
        )

        reconcile_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=2)
        )
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            == jobs_before + 1
        )
        session.rollback()


def test_late_unknown_signal_is_attached_before_risk(tmp_path):
    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        consume_master_replay_signals(session, limit=10, now=NOW)
        run, coverage = _workflow_rows(session, company.id)
        for row in coverage:
            session.get(WorkerJob, row.worker_job_id).status = "succeeded"

        dormant_code = next(
            code
            for code in signals
            if code not in {row.source_id for row in coverage}
        )
        dataset = session.scalar(
            sa.select(DataSet).where(DataSet.code == dormant_code)
        )
        dataset.update_mode = "api"
        dataset.dataset_kind = "on_demand_api"
        dataset.last_success_at = NOW
        dataset.coverage = {"operational_accepted": True}
        dataset.operational_status = "current"
        dataset.auto_update_status = "configured"
        dataset.applicability = None
        handler = session.scalar(
            sa.select(WorkerHandlerRegistration).where(
                WorkerHandlerRegistration.source_id == dormant_code
            )
        )
        handler.metadata_json = {
            "mode": "bounded_daily_master_exact_inn_sweep"
        }
        session.flush()
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )

        blocked = reconcile_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=1)
        )
        late = session.scalar(
            sa.select(CompanySourceCoverage).where(
                CompanySourceCoverage.enrichment_run_id == run.id,
                CompanySourceCoverage.source_id == dormant_code,
            )
        )
        assert late is not None
        assert late.status == "APPLICABILITY_UNKNOWN"
        assert late.execution_status == "blocked"
        assert late.worker_job_id is None
        assert run.source_count == 3
        assert blocked.last_error_code == "applicability_unknown"
        assert blocked.stage == "source_enrichment"
        assert blocked.risk_assessment_id is None
        assert blocked.summary_id is None
        assert blocked.public_ready is False
        assert signals[dormant_code].status == "pending"
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            == jobs_before
        )
        session.rollback()


def test_committed_concurrent_unknown_signal_wins_before_risk(tmp_path):
    company_id = source_id = run_id = None
    source_codes: tuple[str, ...] = ()
    writer: Session | None = None
    try:
        with Session(engine) as setup:
            company, signals = _seed_workflow(setup, tmp_path)
            source_codes = tuple(signals)
            dormant_code = next(
                code
                for code in signals
                if setup.scalar(
                    sa.select(DataSet.last_success_at).where(DataSet.code == code)
                )
                is None
            )
            setup.delete(signals[dormant_code])
            dataset = setup.scalar(
                sa.select(DataSet).where(DataSet.code == dormant_code)
            )
            dataset.update_mode = "api"
            dataset.dataset_kind = "on_demand_api"
            dataset.last_success_at = NOW
            dataset.coverage = {"operational_accepted": True}
            dataset.operational_status = "current"
            dataset.auto_update_status = "configured"
            dataset.applicability = None
            handler = setup.scalar(
                sa.select(WorkerHandlerRegistration).where(
                    WorkerHandlerRegistration.source_id == dormant_code
                )
            )
            handler.metadata_json = {
                "mode": "bounded_daily_master_exact_inn_sweep"
            }
            setup.flush()
            consume_master_replay_signals(setup, limit=10, now=NOW)
            run, coverage = _workflow_rows(setup, company.id)
            for row in coverage:
                setup.get(WorkerJob, row.worker_job_id).status = "succeeded"
            company_id = company.id
            run_id = run.id
            source_id = dataset.source_id
            setup.commit()

        writer = Session(engine)
        locked_company = writer.scalar(
            sa.select(Company)
            .where(Company.id == company_id)
            .with_for_update()
        )
        change = CompanyRegistryChange(
            source_id="concurrent-regression",
            company_id=company_id,
            run_id=None,
            inn=locked_company.inn,
            event_type="identity_changed",
            changed_fields={"late_unknown": True},
            source_data_date=NOW.date(),
            source_record_key=f"concurrent:{uuid4().hex}",
        )
        writer.add(change)
        writer.flush()
        signal = MasterReplaySignal(
            company_id=company_id,
            target_source_id=dormant_code,
            registry_change_id=change.id,
            status="pending",
            created_at=NOW + timedelta(seconds=1),
        )
        writer.add(signal)
        writer.flush()
        signal_id = signal.id

        result: dict[str, object] = {}

        def finish_run() -> None:
            try:
                with Session(engine) as gate:
                    refreshed = reconcile_enrichment_run(
                        gate, run_id, now=NOW + timedelta(minutes=1)
                    )
                    gate.commit()
                    result.update(
                        status=refreshed.status,
                        stage=refreshed.stage,
                        error=refreshed.last_error_code,
                        risk=refreshed.risk_assessment_id,
                        summary=refreshed.summary_id,
                        public_ready=refreshed.public_ready,
                    )
            except Exception as error:  # pragma: no cover - surfaced below
                result["exception"] = error

        gate_thread = threading.Thread(target=finish_run, daemon=True)
        gate_thread.start()
        time.sleep(0.2)
        assert gate_thread.is_alive(), "Risk gate did not wait for company lock"
        writer.commit()
        writer.close()
        writer = None
        gate_thread.join(timeout=10)
        assert not gate_thread.is_alive()
        assert "exception" not in result
        assert result == {
            "status": "waiting_sources",
            "stage": "source_enrichment",
            "error": "applicability_unknown",
            "risk": None,
            "summary": None,
            "public_ready": False,
        }

        with Session(engine) as verify:
            blocker = verify.scalar(
                sa.select(CompanySourceCoverage).where(
                    CompanySourceCoverage.enrichment_run_id == run_id,
                    CompanySourceCoverage.source_id == dormant_code,
                )
            )
            assert blocker.status == "APPLICABILITY_UNKNOWN"
            assert blocker.execution_status == "blocked"
            assert blocker.worker_job_id is None
            assert str(signal_id) in blocker.master_replay_signal_ids
            assert verify.get(MasterReplaySignal, signal_id).status == "pending"
    finally:
        if writer is not None:
            writer.rollback()
            writer.close()
        if company_id is not None and source_id is not None:
            with Session(engine) as cleanup:
                cleanup.execute(sa.delete(Company).where(Company.id == company_id))
                cleanup.execute(
                    sa.delete(WorkerPublicationState).where(
                        WorkerPublicationState.source_id.in_(source_codes)
                    )
                )
                job_ids = tuple(
                    cleanup.scalars(
                        sa.select(WorkerJob.id).where(
                            WorkerJob.source_id.in_(source_codes)
                        )
                    )
                )
                if job_ids:
                    cleanup.execute(
                        sa.delete(WorkerRun).where(WorkerRun.job_id.in_(job_ids))
                    )
                    cleanup.execute(
                        sa.delete(WorkerJob).where(WorkerJob.id.in_(job_ids))
                    )
                cleanup.execute(
                    sa.delete(WorkerHandlerRegistration).where(
                        WorkerHandlerRegistration.source_id.in_(source_codes)
                    )
                )
                cleanup.execute(
                    sa.delete(DataSource).where(DataSource.id == source_id)
                )
                cleanup.commit()


def test_existing_master_bootstrap_persists_unknown_blocker(tmp_path, monkeypatch):
    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        unknown_code = _set_signal_policy(session, signals, mode="api")
        company.official_registry_verified = True
        company.master_source = "fns_egrul"
        company.created_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        monkeypatch.setattr(
            enrichment_service,
            "_accepted_public_cohort_companies",
            lambda _session: (),
        )
        session.flush()

        created, jobs = seed_existing_master_enrichment(
            session, limit=1, now=NOW
        )
        run, coverage = _workflow_rows(session, company.id)
        blocker = next(row for row in coverage if row.source_id == unknown_code)
        assert created == 1
        assert jobs >= 1
        assert run.trigger == "existing_master_bootstrap"
        assert blocker.status == "APPLICABILITY_UNKNOWN"
        assert blocker.execution_status == "blocked"
        assert blocker.worker_job_id is None
        assert run.public_ready is False
        session.rollback()


def test_public_cohort_unknown_protection_uses_canonical_decision(tmp_path, monkeypatch):
    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        unknown_code = _set_signal_policy(session, signals, mode="api")
        monkeypatch.setattr(
            enrichment_service,
            "_accepted_public_cohort_companies",
            lambda _session: (company,),
        )
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )

        result = prioritize_public_cohort_enrichment(session, now=NOW)
        assert result.blocked == 1
        assert result.runs_created == 0
        assert result.jobs_created == 0
        assert session.scalar(
            sa.select(CompanyEnrichmentRun).where(
                CompanyEnrichmentRun.company_id == company.id
            )
        ) is None
        assert signals[unknown_code].status == "pending"
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            == jobs_before
        )
        session.rollback()


def test_publication_contract_rejects_unknown_blocker_even_if_ready_flag_is_stale(
    tmp_path,
):
    with Session(engine) as session:
        company, _signals = _seed_workflow(session, tmp_path)
        consume_master_replay_signals(session, limit=10, now=NOW)
        run, coverage = _workflow_rows(session, company.id)
        for row in coverage:
            session.get(WorkerJob, row.worker_job_id).status = "succeeded"
        completed = reconcile_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=1)
        )
        assert completed.status == "succeeded"
        assert completed.public_ready is True
        assert publishable_runs(session, [company]) == {company.id: completed}

        session.add(
            CompanySourceCoverage(
                enrichment_run_id=run.id,
                company_id=company.id,
                dataset_id=None,
                source_id=f"late_unknown_public_{uuid4().hex[:8]}",
                worker_source_id="late_unknown_public",
                mode="point_check",
                status="APPLICABILITY_UNKNOWN",
                execution_status="blocked",
                source_snapshot={
                    "applicability": "UNKNOWN",
                    "applicability_policy": None,
                    "frozen_at": NOW.isoformat(),
                },
                handler_version="blocked-v1",
                master_replay_signal_ids=[],
                last_error="Source applicability is unresolved",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()

        # Simulate a stale/corrupt ready bit: the publication query must still
        # enforce the durable blocker independently of orchestration state.
        assert completed.public_ready is True
        assert publishable_runs(session, [company]) == {}
        readiness = get_company_public_readiness(session, company.id)
        assert readiness["ready"] is False
        assert readiness["applicability_blocker_count"] == 1
        session.rollback()


def test_legacy_unknown_fails_closed_before_recovery_then_materializes_once(
    tmp_path,
):
    with Session(engine) as session:
        assert session.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "c2a4f6d8e0b1"
        )
        baseline = collect_factory_metrics(
            session, window_hours=1, now=NOW, enforce_read_only=False
        )
        baseline_canonical = canonical_enrichment_metrics(session)
        baseline_pressure = collect_factory_pressure(
            session, raw_root=tmp_path, now=NOW
        )
        company, _signals, run, dataset, signal = (
            _legacy_ready_run_with_replay_outside_coverage(
                session, tmp_path, applicability=None
            )
        )
        historical_risk_id = run.risk_assessment_id
        historical_summary_id = run.summary_id
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )

        readiness = get_company_public_readiness(session, company.id)
        assert readiness["ready"] is False
        assert readiness["status"] == "REPLAY_PENDING"
        assert readiness["unresolved_replay"] is True
        assert readiness["risk_assessment_id"] is None
        assert readiness["summary_id"] is None
        assert publication_service._run_is_current_and_public_ready(session, run) is False
        assert enrichment_service._current_public_ready_run(session, run) is False
        assert publishable_runs(session, [company]) == {}
        ranked = factory_generation_service._latest_complete_run_query(now=NOW)
        assert company.id not in set(
            session.scalars(
                sa.select(ranked.c.company_id).where(ranked.c.position == 1)
            )
        )
        before = collect_factory_metrics(
            session, window_hours=1, now=NOW, enforce_read_only=False
        )
        assert before["totals"]["fully_enriched"] == baseline["totals"][
            "fully_enriched"
        ]
        assert before["totals"]["public_ready"] == baseline["totals"][
            "public_ready"
        ]
        assert before["current_companies_per_day"] == baseline[
            "current_companies_per_day"
        ]
        assert before["totals"]["applicability_blocked_runs"] == (
            baseline["totals"]["applicability_blocked_runs"] + 1
        )
        canonical = canonical_enrichment_metrics(session)
        assert canonical["companies_complete"] == baseline_canonical[
            "companies_complete"
        ]
        assert canonical["public_ready_companies"] == baseline_canonical[
            "public_ready_companies"
        ]
        pressure = collect_factory_pressure(session, raw_root=tmp_path, now=NOW)
        assert pressure.enrichment_companies_per_hour == (
            baseline_pressure.enrichment_companies_per_hour
        )
        blocker_units_before = before["queues"]["applicability_blockers"]

        recovered = recover_legacy_replay_denominators(
            session, limit=10, now=NOW + timedelta(minutes=2)
        )
        session.flush()
        blocker = session.scalar(
            sa.select(CompanySourceCoverage).where(
                CompanySourceCoverage.enrichment_run_id == run.id,
                CompanySourceCoverage.source_id == dataset.code,
            )
        )
        assert recovered.candidates == 1
        assert recovered.reopened == 1
        assert recovered.materialized == 1
        assert recovered.jobs_created == 0
        assert run.status == "waiting_sources"
        assert run.stage == "source_enrichment"
        assert run.public_ready is False
        assert run.finished_at is None
        assert run.source_count == 2
        assert run.risk_assessment_id is None
        assert run.summary_id is None
        assert blocker.status == "APPLICABILITY_UNKNOWN"
        assert blocker.execution_status == "blocked"
        assert blocker.worker_job_id is None
        assert blocker.master_replay_signal_ids == [str(signal.id)]
        assert session.scalar(
            sa.select(CompanyRiskAssessmentV3).where(
                CompanyRiskAssessmentV3.assessment_id == historical_risk_id
            )
        ) is not None
        assert session.scalar(
            sa.select(CompanySummaryV3).where(
                CompanySummaryV3.summary_id == historical_summary_id
            )
        ) is not None
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            == jobs_before
        )

        after = collect_factory_metrics(
            session, window_hours=1, now=NOW, enforce_read_only=False
        )
        assert after["queues"]["applicability_blockers"] == blocker_units_before
        assert after["current_companies_per_day"] == baseline[
            "current_companies_per_day"
        ]
        repeated = recover_legacy_replay_denominators(
            session, limit=10, now=NOW + timedelta(minutes=3)
        )
        assert repeated.candidates == 0
        assert repeated.materialized == 0
        assert repeated.jobs_created == 0
        session.rollback()


def test_legacy_unknown_recovery_transitions_to_one_actionable_job(tmp_path):
    with Session(engine) as session:
        company, _signals, run, dataset, _signal = (
            _legacy_ready_run_with_replay_outside_coverage(
                session, tmp_path, applicability=None
            )
        )
        recover_legacy_replay_denominators(
            session, limit=10, now=NOW + timedelta(minutes=2)
        )
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )
        dataset.applicability = {"entity_types": ["legal"]}
        session.flush()

        first = reconcile_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=3)
        )
        second = reconcile_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=4)
        )
        coverage = session.scalar(
            sa.select(CompanySourceCoverage).where(
                CompanySourceCoverage.enrichment_run_id == run.id,
                CompanySourceCoverage.source_id == dataset.code,
            )
        )
        assert first.stage == "source_enrichment"
        assert second.stage == "source_enrichment"
        assert coverage.status == "RUNNING"
        assert coverage.execution_status == "queued"
        assert coverage.worker_job_id is not None
        assert run.risk_assessment_id is None
        assert run.summary_id is None
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            == jobs_before + 1
        )
        assert get_company_public_readiness(session, company.id)["ready"] is False
        session.rollback()


def test_legacy_recovery_is_restart_safe_across_commits(tmp_path):
    company_id = source_id = run_id = None
    source_codes: tuple[str, ...] = ()
    try:
        with Session(engine) as setup:
            company, signals, run, _dataset, _signal = (
                _legacy_ready_run_with_replay_outside_coverage(
                    setup, tmp_path, applicability=None
                )
            )
            company_id = company.id
            run_id = run.id
            source_codes = tuple(signals)
            source_id = setup.scalar(
                sa.select(DataSet.source_id).where(
                    DataSet.code == next(iter(source_codes))
                )
            )
            setup.commit()

        with Session(engine) as first_process:
            result = recover_legacy_replay_denominators(
                first_process, limit=10, now=NOW + timedelta(minutes=2)
            )
            assert result.candidates == 1
            assert result.materialized == 1
            first_process.commit()

        with Session(engine) as restarted_process:
            result = recover_legacy_replay_denominators(
                restarted_process, limit=10, now=NOW + timedelta(minutes=3)
            )
            blockers = tuple(
                restarted_process.scalars(
                    sa.select(CompanySourceCoverage).where(
                        CompanySourceCoverage.enrichment_run_id == run_id,
                        CompanySourceCoverage.status == "APPLICABILITY_UNKNOWN",
                    )
                )
            )
            assert result.candidates == 0
            assert result.materialized == 0
            assert len(blockers) == 1
            assert restarted_process.get(CompanyEnrichmentRun, run_id).source_count == 2
    finally:
        if company_id is not None and source_id is not None:
            with Session(engine) as cleanup:
                cleanup.execute(sa.delete(Company).where(Company.id == company_id))
                cleanup.execute(
                    sa.delete(WorkerPublicationState).where(
                        WorkerPublicationState.source_id.in_(source_codes)
                    )
                )
                job_ids = tuple(
                    cleanup.scalars(
                        sa.select(WorkerJob.id).where(
                            WorkerJob.source_id.in_(source_codes)
                        )
                    )
                )
                if job_ids:
                    cleanup.execute(
                        sa.delete(WorkerRun).where(WorkerRun.job_id.in_(job_ids))
                    )
                    cleanup.execute(sa.delete(WorkerJob).where(WorkerJob.id.in_(job_ids)))
                cleanup.execute(
                    sa.delete(WorkerHandlerRegistration).where(
                        WorkerHandlerRegistration.source_id.in_(source_codes)
                    )
                )
                cleanup.execute(
                    sa.delete(DataSource).where(DataSource.id == source_id)
                )
                cleanup.commit()


def test_legacy_unknown_recovery_resolves_not_applicable_without_worker(tmp_path):
    with Session(engine) as session:
        _company, _signals, run, dataset, signal = (
            _legacy_ready_run_with_replay_outside_coverage(
                session, tmp_path, applicability=None
            )
        )
        recover_legacy_replay_denominators(
            session, limit=10, now=NOW + timedelta(minutes=2)
        )
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )
        dataset.applicability = {"entity_types": ["individual_entrepreneur"]}
        session.flush()
        reconcile_enrichment_run(session, run.id, now=NOW + timedelta(minutes=3))
        coverage = session.scalar(
            sa.select(CompanySourceCoverage).where(
                CompanySourceCoverage.enrichment_run_id == run.id,
                CompanySourceCoverage.source_id == dataset.code,
            )
        )
        assert coverage.status == "NOT_APPLICABLE"
        assert coverage.execution_status == "succeeded"
        assert coverage.worker_job_id is None
        assert signal.status == "complete"
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            == jobs_before
        )
        session.rollback()


def test_legacy_applicable_and_not_applicable_replay_have_distinct_recovery(
    tmp_path,
):
    with Session(engine) as session:
        company, _signals, run, dataset, _signal = (
            _legacy_ready_run_with_replay_outside_coverage(
                session,
                tmp_path,
                applicability={"entity_types": ["legal"]},
            )
        )
        assert get_company_public_readiness(session, company.id)["ready"] is False
        recovered = recover_legacy_replay_denominators(
            session, limit=1, now=NOW + timedelta(minutes=2)
        )
        actionable = session.scalar(
            sa.select(CompanySourceCoverage).where(
                CompanySourceCoverage.enrichment_run_id == run.id,
                CompanySourceCoverage.source_id == dataset.code,
            )
        )
        assert recovered.candidates == 1
        assert recovered.jobs_created == 1
        assert actionable.execution_status == "queued"
        session.rollback()

    with Session(engine) as session:
        company, _signals, run, dataset, signal = (
            _legacy_ready_run_with_replay_outside_coverage(
                session,
                tmp_path,
                applicability={"entity_types": ["individual_entrepreneur"]},
            )
        )
        historical_refs = (run.risk_assessment_id, run.summary_id)
        jobs_before = int(
            session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0
        )
        assert get_company_public_readiness(session, company.id)["ready"] is True
        recovered = recover_legacy_replay_denominators(
            session, limit=1, now=NOW + timedelta(minutes=2)
        )
        terminal = session.scalar(
            sa.select(CompanySourceCoverage).where(
                CompanySourceCoverage.enrichment_run_id == run.id,
                CompanySourceCoverage.source_id == dataset.code,
            )
        )
        assert recovered.reopened == 0
        assert recovered.jobs_created == 0
        assert terminal.status == "NOT_APPLICABLE"
        assert terminal.execution_status == "succeeded"
        assert signal.status == "complete"
        assert run.status == "succeeded"
        assert run.public_ready is True
        assert (run.risk_assessment_id, run.summary_id) == historical_refs
        assert (
            int(session.scalar(sa.select(sa.func.count()).select_from(WorkerJob)) or 0)
            == jobs_before
        )
        session.rollback()


def test_replay_signal_already_terminally_represented_does_not_false_block(
    tmp_path,
):
    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        consume_master_replay_signals(session, limit=10, now=NOW)
        run, coverage = _workflow_rows(session, company.id)
        for row in coverage:
            session.get(WorkerJob, row.worker_job_id).status = "succeeded"
        reconcile_enrichment_run(session, run.id, now=NOW + timedelta(minutes=1))
        represented = coverage[0]
        signal_id = UUID(represented.master_replay_signal_ids[0])
        represented_signal = session.get(MasterReplaySignal, signal_id)
        represented_signal.status = "pending"
        represented_signal.completed_at = None
        session.flush()

        assert get_company_public_readiness(session, company.id)["ready"] is True
        assert publication_service._run_is_current_and_public_ready(session, run) is True
        recovered = recover_legacy_replay_denominators(
            session, limit=10, now=NOW + timedelta(minutes=2)
        )
        assert recovered.candidates == 0
        session.rollback()


def test_legacy_recovery_targets_latest_run_and_obeys_limit(tmp_path):
    with Session(engine) as session:
        first_company, _signals, first, _dataset, first_signal = (
            _legacy_ready_run_with_replay_outside_coverage(
                session, tmp_path, applicability=None
            )
        )
        first_signal.status = "scheduled"
        older = CompanyEnrichmentRun(
            company_id=first_company.id,
            trigger="legacy-history",
            idempotency_key=f"legacy-history:{uuid4()}",
            status="succeeded",
            stage="complete",
            source_count=0,
            completed_source_count=0,
            failed_source_count=0,
            public_ready=False,
            created_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(days=1),
            finished_at=NOW - timedelta(days=1),
        )
        session.add(older)
        second_company, _signals, second, _dataset, second_signal = (
            _legacy_ready_run_with_replay_outside_coverage(
                session, tmp_path, applicability=None
            )
        )
        second_signal.status = "scheduled"
        session.flush()

        bounded = recover_legacy_replay_denominators(
            session, limit=1, now=NOW + timedelta(minutes=2)
        )
        assert bounded.candidates == 1
        reopened = [run for run in (first, second) if run.status != "succeeded"]
        assert len(reopened) == 1
        assert older.status == "succeeded"
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(CompanySourceCoverage)
            .where(CompanySourceCoverage.enrichment_run_id == older.id)
        ) == 0

        remainder = recover_legacy_replay_denominators(
            session, limit=1, now=NOW + timedelta(minutes=3)
        )
        assert remainder.candidates == 1
        assert first.status == "waiting_sources"
        assert second.status == "waiting_sources"
        assert get_company_public_readiness(session, first_company.id)["ready"] is False
        assert get_company_public_readiness(session, second_company.id)["ready"] is False
        session.rollback()


def test_no_run_cancelled_and_failed_remain_not_ready_and_are_not_recovered(
    tmp_path,
):
    with Session(engine) as session:
        no_run_company, no_run_signals = _seed_workflow(session, tmp_path)
        _set_signal_policy(session, no_run_signals, mode="api")
        assert get_company_public_readiness(session, no_run_company.id)["ready"] is False
        assert recover_legacy_replay_denominators(
            session, limit=10, now=NOW
        ).candidates == 0

        for status in ("cancelled", "failed"):
            company, signals = _seed_workflow(session, tmp_path)
            _set_signal_policy(session, signals, mode="api")
            run = CompanyEnrichmentRun(
                company_id=company.id,
                trigger=f"legacy-{status}",
                idempotency_key=f"legacy-{status}:{uuid4()}",
                status=status,
                stage="failed" if status == "failed" else "source_enrichment",
                source_count=0,
                completed_source_count=0,
                failed_source_count=0,
                public_ready=False,
                created_at=NOW,
                updated_at=NOW,
                finished_at=NOW,
            )
            session.add(run)
            session.flush()
            assert get_company_public_readiness(session, company.id)["ready"] is False
        assert recover_legacy_replay_denominators(
            session, limit=10, now=NOW + timedelta(minutes=1)
        ).candidates == 0
        session.rollback()


def test_postgresql_backlog_creates_local_replay_and_bounded_point_jobs(tmp_path):
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
        assert {
            "company_enrichment_runs",
            "company_source_coverage",
        } <= set(sa.inspect(connection).get_table_names())

    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        result = consume_master_replay_signals(session, limit=10, now=NOW)
        # Only operational, explicitly applicable signals enter the consumer.
        assert result.signals_seen == 2
        assert result.signals_scheduled == 2
        assert result.runs_created == 1
        assert result.jobs_created == 2

        run, coverage = _workflow_rows(session, company.id)
        assert run.source_count == 2
        assert {item["source_id"] for item in run.applicable_sources} == {
            row.source_id for row in coverage
        }
        assert {row.mode for row in coverage} == {
            "local_bulk_replay",
            "point_check",
        }
        bulk_row = next(row for row in coverage if row.mode == "local_bulk_replay")
        point_row = next(row for row in coverage if row.mode == "point_check")
        bulk_job = session.get(WorkerJob, bulk_row.worker_job_id)
        point_job = session.get(WorkerJob, point_row.worker_job_id)
        assert bulk_job.job_type == "company_enrichment_local_replay"
        assert bulk_job.schedule_metadata["check_only"] is True
        assert bulk_job.schedule_metadata["replay_snapshot"] is True
        assert bulk_job.schedule_metadata["download_allowed"] is False
        assert bulk_job.schedule_metadata["replay_pointer"].startswith("file://")
        assert point_job.job_type == "company_enrichment_point_check"
        assert point_job.max_attempts == 4
        assert point_job.timeout_seconds <= 300
        assert point_job.schedule_metadata["company_id"] == company.id
        assert point_job.schedule_metadata["cohort_size"] == 1
        assert signals[bulk_row.source_id].status == "scheduled"
        assert signals[point_row.source_id].status == "scheduled"
        dormant = next(
            signal for code, signal in signals.items() if code not in {bulk_row.source_id, point_row.source_id}
        )
        assert dormant.status == "pending"

        repeated = consume_master_replay_signals(session, limit=10, now=NOW)
        # Non-operational signals are filtered before LIMIT, so they neither
        # starve eligible companies nor masquerade as executable work.
        assert repeated.signals_seen == 0
        assert repeated.signals_scheduled == 0
        assert repeated.runs_created == 0
        assert repeated.jobs_created == 0
        session.rollback()


def test_public_cohort_priority_is_idempotent_and_preserves_normal_backlog(
    tmp_path, monkeypatch
):
    with Session(engine) as session:
        public_company, public_signals = _seed_workflow(session, tmp_path)
        normal_company, normal_signals = _seed_workflow(session, tmp_path)
        monkeypatch.setattr(
            enrichment_service,
            "_accepted_public_cohort_companies",
            lambda _session: (public_company,),
        )

        first = prioritize_public_cohort_enrichment(session, now=NOW)
        session.flush()
        public_runs = tuple(
            session.scalars(
                sa.select(CompanyEnrichmentRun).where(
                    CompanyEnrichmentRun.company_id == public_company.id
                )
            )
        )
        assert first.cohort_count == 1
        assert first.runs_created == 1
        assert len(public_runs) == 1
        assert public_runs[0].trigger == "public_cohort_priority"
        public_coverage = tuple(
            session.scalars(
                sa.select(CompanySourceCoverage).where(
                    CompanySourceCoverage.enrichment_run_id == public_runs[0].id
                )
            )
        )
        public_jobs = {
            session.get(WorkerJob, row.worker_job_id)
            for row in public_coverage
            if row.worker_job_id is not None
        }
        assert public_jobs
        assert {
            job.schedule_metadata.get("enrichment_priority") for job in public_jobs
        } == {"accepted_public_cohort"}
        dormant_public = next(
            signal
            for code, signal in public_signals.items()
            if code not in {row.source_id for row in public_coverage}
        )
        assert dormant_public.status == "pending"
        assert {signal.status for signal in normal_signals.values()} == {"pending"}

        second = prioritize_public_cohort_enrichment(
            session, now=NOW + timedelta(seconds=30)
        )
        assert second.runs_created == 0
        assert second.runs_resumed == 1
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(CompanyEnrichmentRun)
            .where(CompanyEnrichmentRun.company_id == public_company.id)
        ) == 1

        consumed = consume_master_replay_signals(
            session, limit=10, now=NOW + timedelta(minutes=1)
        )
        assert consumed.runs_created == 1
        normal_run = session.scalar(
            sa.select(CompanyEnrichmentRun).where(
                CompanyEnrichmentRun.company_id == normal_company.id
            )
        )
        assert normal_run is not None
        assert normal_run.trigger == "master_replay"
        assert dormant_public.status == "pending"
        session.rollback()


def test_postgresql_legacy_null_acceptance_is_operational_but_false_is_not(tmp_path):
    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        for dataset in session.scalars(
            sa.select(DataSet).where(
                DataSet.code.in_(tuple(signals)), DataSet.last_success_at.is_not(None)
            )
        ):
            coverage = dict(dataset.coverage or {})
            coverage.pop("operational_accepted", None)
            dataset.coverage = coverage
        session.flush()

        result = consume_master_replay_signals(session, limit=10, now=NOW)

        assert result.runs_created == 1
        run, coverage_rows = _workflow_rows(session, company.id)
        assert run.source_count == 2
        assert {row.source_id for row in coverage_rows} == {
            code for code, signal in signals.items() if signal.status == "scheduled"
        }
        session.rollback()


def test_postgresql_completeness_gate_then_persisted_risk_then_summary(tmp_path):
    with Session(engine) as session:
        initial_master_count = int(
            session.scalar(sa.select(sa.func.count()).select_from(Company)) or 0
        )
        company, _signals = _seed_workflow(session, tmp_path)
        consume_master_replay_signals(session, limit=10, now=NOW)
        run, coverage = _workflow_rows(session, company.id)
        bulk_row = next(row for row in coverage if row.mode == "local_bulk_replay")
        point_row = next(row for row in coverage if row.mode == "point_check")
        session.get(WorkerJob, bulk_row.worker_job_id).status = "succeeded"
        point_job = session.get(WorkerJob, point_row.worker_job_id)
        point_job.status = "retry_scheduled"
        point_job.next_attempt_at = NOW + timedelta(minutes=1)
        waiting = reconcile_enrichment_run(session, run.id, now=NOW)
        assert waiting.status == "retry_scheduled"
        assert waiting.completed_source_count == 1
        assert waiting.public_ready is False
        assert session.scalar(
            sa.select(sa.func.count())
            .select_from(CompanyRiskAssessmentV3)
            .where(CompanyRiskAssessmentV3.company_id == company.id)
        ) == 0
        assert get_company_public_readiness(session, company.id)["status"] == "RETRY_SCHEDULED"

        point_job.status = "succeeded"
        point_job.next_attempt_at = None
        completed = reconcile_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=2)
        )
        assert completed.status == "succeeded"
        assert completed.stage == "complete"
        assert completed.public_ready is True
        risk = session.scalar(
            sa.select(CompanyRiskAssessmentV3).where(
                CompanyRiskAssessmentV3.assessment_id == completed.risk_assessment_id
            )
        )
        summary = session.scalar(
            sa.select(CompanySummaryV3).where(
                CompanySummaryV3.summary_id == completed.summary_id
            )
        )
        assert risk is not None
        assert summary is not None
        assert summary.risk_assessment_id == risk.assessment_id
        readiness = get_company_public_readiness(session, company.id)
        assert readiness["status"] == "READY"
        assert readiness["ready"] is True
        assert readiness["expected_source_count"] == 2
        assert readiness["completed_source_count"] == 2
        assert readiness["failed_source_count"] == 0
        assert readiness["pending_source_count"] == 0
        session.add(
            Company(
                inn=str(uuid4().int)[:10],
                name="Master identity without enrichment run",
                entity_type="legal",
                status="ACTIVE",
            )
        )
        session.flush()
        metrics = canonical_enrichment_metrics(session)
        assert metrics["master_company_count"] == initial_master_count + 2
        assert metrics["companies_not_started"] == initial_master_count + 1
        assert metrics["companies_complete"] == 1
        assert metrics["risk_ready_companies"] == 1
        assert metrics["summary_ready_companies"] == 1
        assert metrics["source_expectation_count"] == 2
        assert metrics["source_expectations_succeeded"] == 2
        assert metrics["source_completion_percent"] == 100.0
        assert metrics["public_ready_companies"] == 1
        assert metrics["coverage_at_least_1"] == 1
        assert metrics["coverage_100_percent"] == 1
        assert metrics["average_coverage_percent"] == round(
            100 / (initial_master_count + 2), 4
        )
        expected_median = 50.0 if initial_master_count == 0 else 0.0
        assert metrics["median_coverage_percent"] == expected_median
        session.rollback()


def test_postgresql_restart_preserves_success_and_requeues_only_failure(tmp_path):
    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        consume_master_replay_signals(session, limit=10, now=NOW)
        run, coverage = _workflow_rows(session, company.id)
        bulk_row = next(row for row in coverage if row.mode == "local_bulk_replay")
        point_row = next(row for row in coverage if row.mode == "point_check")
        bulk_job_id = bulk_row.worker_job_id
        failed_point_job_id = point_row.worker_job_id
        session.get(WorkerJob, bulk_job_id).status = "succeeded"
        session.get(WorkerJob, failed_point_job_id).status = "failed"
        point_signal = signals[point_row.source_id]
        point_signal.status = "failed"
        point_signal.last_error = "fixture failure"
        failed = reconcile_enrichment_run(session, run.id, now=NOW)
        assert failed.status == "failed"
        assert failed.completed_source_count == 1
        assert failed.failed_source_count == 1

        restarted = restart_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=1)
        )
        session.flush()
        assert restarted.restart_count == 1
        assert restarted.status == "waiting_sources"
        assert bulk_row.worker_job_id == bulk_job_id
        assert bulk_row.status == "NOT_FOUND"
        assert bulk_row.execution_status == "succeeded"
        assert point_row.worker_job_id != failed_point_job_id
        assert point_row.status == "RUNNING"
        assert point_row.execution_status == "queued"
        assert point_signal.status == "scheduled"
        replacement = session.get(WorkerJob, point_row.worker_job_id)
        assert replacement.idempotency_key != session.get(
            WorkerJob, failed_point_job_id
        ).idempotency_key
        session.rollback()


def test_postgresql_restart_replaces_failed_local_bulk_job(tmp_path):
    with Session(engine) as session:
        company, signals = _seed_workflow(session, tmp_path)
        consume_master_replay_signals(session, limit=10, now=NOW)
        run, coverage = _workflow_rows(session, company.id)
        bulk_row = next(row for row in coverage if row.mode == "local_bulk_replay")
        point_row = next(row for row in coverage if row.mode == "point_check")
        failed_bulk_job_id = bulk_row.worker_job_id
        session.get(WorkerJob, failed_bulk_job_id).status = "failed"
        session.get(WorkerJob, point_row.worker_job_id).status = "succeeded"
        bulk_signal = signals[bulk_row.source_id]
        bulk_signal.status = "failed"
        bulk_signal.last_error = "fixture failure"
        assert reconcile_enrichment_run(session, run.id, now=NOW).status == "failed"

        restarted = restart_enrichment_run(
            session, run.id, now=NOW + timedelta(minutes=1)
        )
        session.flush()
        assert restarted.restart_count == 1
        assert restarted.status == "waiting_sources"
        assert point_row.status == "NOT_FOUND"
        assert point_row.execution_status == "succeeded"
        assert bulk_row.worker_job_id != failed_bulk_job_id
        assert bulk_row.status == "RUNNING"
        assert bulk_row.execution_status == "queued"
        assert bulk_signal.status == "scheduled"
        replacement = session.get(WorkerJob, bulk_row.worker_job_id)
        assert replacement.idempotency_key != session.get(
            WorkerJob, failed_bulk_job_id
        ).idempotency_key
        session.rollback()
