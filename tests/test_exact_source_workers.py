from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import exact_source_workers as worker
from app.models.company import Company
from app.models.source import DataSet
from app.models.worker import WorkerHandlerRegistration, WorkerJob
from app.services.source_factory_registry_service import ensure_source_factory_datasets
from app.worker.contracts import HandlerResult, StagingResult, ValidationResult
from app.worker.errors import AccessRequiredError, LegalBlockError, WorkerNetworkError


NOW = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)


def test_each_source_has_an_independent_worker_source_id_and_lease_namespace():
    assert set(worker.SPECS) == {
        "fns_npd",
        "nostroy_sro_members_on_demand",
        "nopriz_sro_members_on_demand",
        "prime_corporate_disclosure",
        "rkn_personal_data_operators",
    }
    assert len(set(worker.SPECS)) == 5


def test_http_protection_and_transient_failures_are_not_clean_negatives():
    with pytest.raises(LegalBlockError):
        worker._validate_response_status(
            "nostroy_sro_members_on_demand", SimpleNamespace(status_code=403)
        )
    with pytest.raises(WorkerNetworkError):
        worker._validate_response_status(
            "fns_npd", SimpleNamespace(status_code=429)
        )


def test_eis_fails_closed_without_token(monkeypatch):
    monkeypatch.delenv("EIS_IP_TOKEN", raising=False)
    with pytest.raises(AccessRequiredError, match="EIS_IP_TOKEN"):
        worker.schedule_eis_rnp_check(
            SimpleNamespace(), raw_root=__import__("pathlib").Path("/tmp"), now=NOW
        )


def test_active_sweep_blocks_cohort_fanout_and_advances_daily_schedule(tmp_path):
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        with factory() as session:
            ensure_source_factory_datasets(session)
            session.add_all(
                (
                    Company(
                        inn="770708389312",
                        name="ONE",
                        entity_type="individual_entrepreneur",
                    ),
                    Company(
                        inn="590705603612",
                        name="TWO",
                        entity_type="individual_entrepreneur",
                    ),
                )
            )
            session.add(
                WorkerHandlerRegistration(
                    source_id="fns_npd",
                    handler_version=worker.HANDLER_VERSION,
                    approved=True,
                    enabled=True,
                    live_mode=False,
                    metadata_json={},
                )
            )
            session.flush()

            first = worker.schedule_exact_source_sweep(
                session,
                source_id="fns_npd",
                raw_root=Path(tmp_path),
                now=NOW,
            )
            dataset = session.scalar(
                select(DataSet).where(
                    DataSet.code == "fns_npd"
                )
            )
            assert len(first) == 2
            assert dataset.next_expected_update_at == NOW + timedelta(days=1)

            session.add(
                Company(
                    inn="781201456012",
                    name="THREE",
                    entity_type="individual_entrepreneur",
                )
            )
            session.flush()
            repeated = worker.schedule_exact_source_sweep(
                session,
                source_id="fns_npd",
                raw_root=Path(tmp_path),
                now=NOW + timedelta(minutes=1),
            )

            assert len(repeated) == 1
            assert repeated[0].created is False
            assert session.scalar(
                select(func.count())
                .select_from(WorkerJob)
                .where(WorkerJob.source_id == "fns_npd")
            ) == 2
    finally:
        transaction.rollback()
        connection.close()


def _npd_result(*, inn: str, request_date: str = "2026-09-25") -> HandlerResult:
    metadata = {
        "inn": inn,
        "request_date": request_date,
        "result": {
            "is_npd": False,
            "message": "not registered",
            "http_status": 200,
        },
    }
    return HandlerResult(
        staging_result=StagingResult(
            staging_pointer="file:///tmp/normalized.json",
            validation=ValidationResult(accepted=True, metadata=metadata),
            checksum="a" * 64,
        )
    )


def _exact_job(
    *,
    inn: str,
    sweep_id: str,
    status: str,
    suffix: str,
    request_date: str = "2026-09-25",
) -> WorkerJob:
    return WorkerJob(
        source_id="fns_npd",
        job_type="exact_inn_scheduled_sweep",
        handler_version=worker.HANDLER_VERSION,
        schedule_metadata={
            "sweep_id": sweep_id,
            "cohort_size": 2,
            "inn": inn,
            "request_date": request_date,
        },
        idempotency_key=f"{sweep_id}:{inn}:{suffix}",
        status=status,
        max_attempts=4,
        timeout_seconds=120,
    )


def test_duplicate_success_does_not_complete_or_reaccept_exact_sweep():
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        with factory() as session:
            ensure_source_factory_datasets(session)
            sweep_id = "fns_npd:2026-09-25:distinct-inn-test"
            first = _exact_job(
                inn="010000010380", sweep_id=sweep_id, status="succeeded", suffix="first"
            )
            duplicate = _exact_job(
                inn="010000010380", sweep_id=sweep_id, status="running", suffix="duplicate"
            )
            session.add_all((first, duplicate))
            session.flush()

            dataset = session.scalar(select(DataSet).where(DataSet.code == "fns_npd"))
            dataset.coverage = {
                "active_sweep": {
                    "sweep_id": sweep_id,
                    "cohort_size": 2,
                    "completed": 1,
                    "found": 0,
                    "not_found": 1,
                    "published_facts": 1,
                },
                "successful_scheduled_checks": 0,
                "operational_accepted": False,
            }
            claim = SimpleNamespace(
                source_id="fns_npd", schedule_metadata=duplicate.schedule_metadata
            )
            worker.publish_exact_source_result(
                session, claim, _npd_result(inn="010000010380")
            )

            coverage = dataset.coverage
            assert coverage["active_sweep"]["completed"] == 1
            assert coverage["active_sweep"]["not_found"] == 1
            assert coverage["active_sweep"]["published_facts"] == 1
            assert coverage["successful_scheduled_checks"] == 0
            assert coverage["operational_accepted"] is False
            assert dataset.operational_status == "updating"
            assert dataset.last_success_at is None

            second = _exact_job(
                inn="010000021695", sweep_id=sweep_id, status="running", suffix="second"
            )
            session.add(second)
            session.flush()
            claim = SimpleNamespace(
                source_id="fns_npd", schedule_metadata=second.schedule_metadata
            )
            worker.publish_exact_source_result(
                session, claim, _npd_result(inn="010000021695")
            )

            coverage = dataset.coverage
            assert coverage["active_sweep"]["completed"] == 2
            assert coverage["active_sweep"]["not_found"] == 2
            assert coverage["active_sweep"]["published_facts"] == 2
            assert coverage["successful_scheduled_checks"] == 1
            assert coverage["completed_sweep_ids"] == [sweep_id]
            assert coverage["operational_accepted"] is False
            assert dataset.operational_status == "current"
            assert dataset.last_success_at is not None

            second.status = "succeeded"
            session.flush()
            worker.publish_exact_source_result(
                session, claim, _npd_result(inn="010000021695")
            )
            assert dataset.coverage["successful_scheduled_checks"] == 1
            assert dataset.coverage["completed_sweep_ids"] == [sweep_id]
            assert dataset.coverage["active_sweep"]["not_found"] == 2
            assert dataset.coverage["active_sweep"]["published_facts"] == 2

            next_sweep_id = "fns_npd:2026-09-26:distinct-inn-test"
            next_job = _exact_job(
                inn="010000010380",
                sweep_id=next_sweep_id,
                status="running",
                suffix="next-day",
                request_date="2026-09-26",
            )
            session.add(next_job)
            session.flush()
            claim = SimpleNamespace(
                source_id="fns_npd", schedule_metadata=next_job.schedule_metadata
            )
            worker.publish_exact_source_result(
                session,
                claim,
                _npd_result(inn="010000010380", request_date="2026-09-26"),
            )
            assert dataset.coverage["active_sweep"]["sweep_id"] == next_sweep_id
            assert dataset.coverage["active_sweep"]["completed"] == 1
            assert dataset.coverage["active_sweep"]["not_found"] == 1
            assert dataset.coverage["active_sweep"]["published_facts"] == 1
            assert dataset.coverage["successful_scheduled_checks"] == 1
    finally:
        transaction.rollback()
        connection.close()
