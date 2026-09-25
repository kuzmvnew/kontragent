from datetime import date, datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import fns_sme_support as support
from app.models.company import Company
from app.models.fns_sme_support import FnsSmeSupportEntry
from app.models.source import DataSet, DataSource, IngestionRun
from app.models.worker import WorkerPublicationState
from app.worker.contracts import (
    ExecutionCounters,
    HandlerResult,
    StagingResult,
    ValidationResult,
)


NOW = datetime(2026, 9, 25, 8, tzinfo=timezone.utc)


def _row(inn, key):
    return {
        "data_date": "2026-09-15",
        "source_record_key": key,
        "source_document_id": key,
        "provider_inn": "7707329152",
        "recipient_inn": inn,
        "recipient_ogrn": "1027700000001",
        "recipient_kind": "legal",
        "information_date": "2026-09-15",
        "support_until": "2026-12-31",
        "decision_date": "2026-09-01",
        "termination_date": None,
        "violation_code": "2",
        "support_form_code": "1",
        "support_form_name": "Финансовая поддержка",
        "support_type_code": "1",
        "support_type_name": "Субсидия",
        "amounts": [{"value": "100.00", "unit_code": "1"}],
        "violations": [],
        "regulatory_document_ids": [],
        "eligible_for_company_projection": True,
    }


def _factory():
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    return connection, transaction, factory


def test_worker_discovery_keeps_source_date_and_freshness_separate():
    discovered = SimpleNamespace(
        data_url=(
            "https://file.nalog.ru/opendata/7707329152-rsmppp/"
            "data-20260915-structure-20230615.zip"
        ),
        structure_url=(
            "https://file.nalog.ru/opendata/7707329152-rsmppp/"
            "structure-20230615.xsd"
        ),
        modified_date=date(2026, 9, 15),
        data_date=date(2026, 10, 15),
    )
    provider = SimpleNamespace(discover_release=lambda: discovered)

    release = support._discover_worker_release(now=NOW, provider=provider)

    assert release.source_data_date == date(2026, 9, 15)
    assert release.actual_until == date(2026, 10, 15)
    assert support._worker_spec().source_id == "fns_sme_support"


def test_postgresql_publish_then_same_release_replay_for_new_master(tmp_path, monkeypatch):
    connection, transaction, factory = _factory()
    first_inn = "7800009911"
    second_inn = "7800009928"
    snapshot = tmp_path / "normalized-support.jsonl"
    snapshot.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in (
            _row(first_inn, "SUP-1"),
            _row(second_inn, "SUP-2"),
        )) + "\n",
        encoding="utf-8",
    )
    checksum = support.sha256_file(snapshot)
    monkeypatch.setattr("app.ingestion.fns_bulk_worker.utc_now", lambda: NOW)
    release = support._discover_worker_release(
        now=NOW,
        provider=SimpleNamespace(discover_release=lambda: SimpleNamespace(
            data_url="https://file.nalog.ru/opendata/7707329152-rsmppp/data-20260915-structure-20230615.zip",
            structure_url="https://file.nalog.ru/opendata/7707329152-rsmppp/structure-20230615.xsd",
            modified_date=date(2026, 9, 15),
            data_date=date(2026, 10, 15),
        )),
    )
    try:
        with factory() as session:
            source = session.scalar(sa.select(DataSource).where(DataSource.code == "fns"))
            if source is None:
                source = DataSource(
                    code="fns", name="FNS", source_type="official", priority=10,
                    enabled=True,
                )
                session.add(source)
                session.flush()
            dataset = session.scalar(
                sa.select(DataSet).where(DataSet.code == support.DATASET_CODE)
            )
            if dataset is None:
                dataset = DataSet(
                    source_id=source.id,
                    code=support.DATASET_CODE,
                    name="FNS support",
                    domain="sme_support",
                    update_mode="bulk",
                    data_format="xml",
                    priority=10,
                    enabled=False,
                )
                session.add(dataset)
                session.flush()
            session.execute(sa.delete(FnsSmeSupportEntry).where(
                FnsSmeSupportEntry.dataset_id == dataset.id
            ))
            session.execute(sa.delete(IngestionRun).where(
                IngestionRun.dataset_id == dataset.id
            ))
            session.execute(sa.delete(WorkerPublicationState).where(
                WorkerPublicationState.source_id == support.SOURCE_ID
            ))
            dataset.last_data_date = None
            first = Company(inn=first_inn, name="First support", entity_type="legal")
            session.add(first)
            session.flush()

            claim = SimpleNamespace(
                run_id=uuid4(),
                schedule_metadata=release.as_metadata(),
            )
            result = HandlerResult(
                staging_result=StagingResult(
                    staging_pointer=snapshot.as_uri(),
                    checksum=checksum,
                    validation=ValidationResult(
                        accepted=True,
                        metadata={"release_identity": release.identity},
                    ),
                    metadata={"dataset_code": support.DATASET_CODE},
                ),
                checksum_metadata={"artifact_sha256": "a" * 64},
                counters=ExecutionCounters(records_seen=2, records_written=2),
            )
            published = support.publish_fns_sme_support_worker_result(
                session, claim, result
            )
            assert published.counters.records_published == 1
            assert published.change_summary.new_facts == 1
            ingestion_run_id = published.staging_result.validation.metadata[
                "legacy_ingestion_run_id"
            ]
            state = WorkerPublicationState(
                source_id=support.SOURCE_ID,
                active_pointer=snapshot.as_uri(),
                generation=1,
                last_fencing_token=1,
                validation_metadata={
                    "checksum": checksum,
                    "validation": published.staging_result.validation.metadata,
                    "staging": published.staging_result.metadata,
                },
            )
            session.add(state)
            session.flush()
            assert session.scalar(sa.select(sa.func.count()).select_from(
                FnsSmeSupportEntry
            ).where(FnsSmeSupportEntry.ingestion_run_id == ingestion_run_id)) == 1

            session.add(Company(
                inn=second_inn, name="Second support", entity_type="legal"
            ))
            session.flush()
            replay_claim = SimpleNamespace(
                run_id=uuid4(),
                schedule_metadata={
                    **release.as_metadata(),
                    "check_only": True,
                    "replay_snapshot": True,
                    "replay_pointer": snapshot.as_uri(),
                    "replay_checksum": checksum,
                },
            )
            replay = support.publish_fns_sme_support_worker_result(
                session, replay_claim, HandlerResult()
            )
            assert replay.counters.records_published == 1
            assert replay.change_summary.replayed_facts == 1
            again = support.publish_fns_sme_support_worker_result(
                session, replay_claim, HandlerResult()
            )
            assert again.counters.records_published == 0
            assert session.scalar(sa.select(sa.func.count()).select_from(
                FnsSmeSupportEntry
            ).where(FnsSmeSupportEntry.ingestion_run_id == ingestion_run_id)) == 2
            assert dataset.coverage["change_summary"]["replayed_facts"] == 0
    finally:
        transaction.rollback()
        connection.close()
