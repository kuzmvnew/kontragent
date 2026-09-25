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
