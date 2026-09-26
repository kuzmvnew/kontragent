from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

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
from app.services.company_enrichment_service import (
    _enrichment_refill_capacity,
    _semantic_coverage,
    canonical_enrichment_metrics,
    consume_master_replay_signals,
    get_company_public_readiness,
    reconcile_enrichment_run,
    restart_enrichment_run,
)


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
        assert result.signals_seen == 3
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
