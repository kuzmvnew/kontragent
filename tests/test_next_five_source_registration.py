from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import next_five_source_workers as worker
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerHandlerRegistration, WorkerJob
from app.services.data_readiness_scheduler import SCHEDULED_SOURCE_DATASET_CODES
from app.services.source_factory_registry_service import ensure_source_factory_datasets
from app.worker.errors import AccessRequiredError
from app.worker.registry import HandlerRegistry


SOURCE_IDS = {
    "fedresurs_messages",
    "checko_arbitration_cases",
    "fssp_enforcement",
    "moscow_general_court_cases",
    "eis_procurements",
}


def _context(source_id: str):
    return SimpleNamespace(
        job_id=uuid4(),
        run_id=uuid4(),
        source_id=source_id,
        worker_id="test-worker",
        fencing_token=1,
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        schedule_metadata={},
        heartbeat=lambda: None,
        report_counters=lambda _counters: None,
        shutdown_requested=lambda: False,
    )


def test_next_five_contracts_have_distinct_worker_source_ids():
    assert set(worker.CONTRACTS) == SOURCE_IDS
    assert {contract.source_id for contract in worker.CONTRACTS.values()} == SOURCE_IDS
    assert "eis_rnp" not in worker.CONTRACTS
    assert SOURCE_IDS.isdisjoint(SCHEDULED_SOURCE_DATASET_CODES)


@pytest.mark.parametrize("source_id", sorted(SOURCE_IDS))
def test_unaccepted_access_fails_closed_without_clean_negative(source_id, monkeypatch):
    contract = worker.CONTRACTS[source_id]
    for name in contract.required_environment:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(AccessRequiredError) as caught:
        worker.access_pending_handler(_context(source_id))

    assert caught.value.kind.value == "access_required"
    assert source_id in str(caught.value)
    assert "not_found" not in str(caught.value)


def test_credential_presence_does_not_bypass_unaccepted_baseline(monkeypatch):
    monkeypatch.setenv("CHECKO_API_KEY", "secret-value-not-printed")
    monkeypatch.setenv("EIS_IP_TOKEN", "secret-value-not-printed")

    for source_id in ("checko_arbitration_cases", "eis_procurements"):
        ready, missing = worker.access_state(source_id)
        assert ready is False
        assert missing == ()
        with pytest.raises(AccessRequiredError, match="baseline|schema") as caught:
            worker.access_pending_handler(_context(source_id))
        assert "secret-value-not-printed" not in str(caught.value)


def test_postgresql_registration_connects_handlers_without_activation_or_jobs():
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
            for dataset in session.scalars(
                select(DataSet).where(DataSet.code.in_(SOURCE_IDS))
            ):
                dataset.enabled = False
                dataset.operational_status = "not_configured"
                dataset.auto_update_status = "not_configured"
            session.flush()
            jobs_before = session.scalar(select(func.count()).select_from(WorkerJob))

            ensure_source_factory_datasets(session)

            registry = HandlerRegistry()
            registrations = worker.register_next_five_workers(session, registry)

            assert {item.source_id for item in registrations} == SOURCE_IDS
            assert session.scalar(select(func.count()).select_from(WorkerJob)) == jobs_before
            for source_id in SOURCE_IDS:
                dataset = session.scalar(select(DataSet).where(DataSet.code == source_id))
                assert dataset is not None
                assert dataset.enabled is False
                assert dataset.operational_status == "access_pending"
                assert dataset.auto_update_status == "access_pending"
                assert session.get(DataSource, dataset.source_id) is not None

                durable = session.get(
                    WorkerHandlerRegistration,
                    (source_id, worker.HANDLER_VERSION),
                )
                assert durable is not None
                assert durable.approved is True
                assert durable.enabled is True
                assert durable.live_mode is False
                assert durable.metadata_json["fail_closed"] is True
                assert durable.metadata_json["schedule_enabled"] is False
                assert registry.resolve(source_id, worker.HANDLER_VERSION).fixture is False
    finally:
        transaction.rollback()
        connection.close()
