import csv
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import sqlalchemy as sa
import pytest
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import fns_disqualified as disqualified
from app.models.company import Company
from app.models.disqualified_person import DisqualifiedPersonSnapshot
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerPublicationState
from app.services import disqualified_service
from app.worker.contracts import (
    ExecutionCounters,
    HandlerResult,
    StagingResult,
    ValidationResult,
)
from app.worker.errors import SchemaMismatchError


NOW = datetime(2026, 9, 25, 8, tzinfo=timezone.utc)


def _normalized_row(inn, number):
    return {
        "data_date": "2026-09-20",
        "register_number": number,
        "full_name": "ИВАНОВ ИВАН ИВАНОВИЧ",
        "organization_name": "ООО ТЕСТ",
        "organization_inn": inn,
        "position": "ДИРЕКТОР",
        "offence_article": "Ч. 5 СТ. 14.25 КОАП РФ",
        "disqualification_term": "1 г 0 м 0 д",
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "matching_state": (
            "organization_inn_exact_candidate"
            if inn
            else "no_organization_inn_name_match_prohibited"
        ),
    }


def test_live_passport_discovery_accepts_only_pinned_current_csv():
    html = b"""
      <a href="https://data.nalog.ru/opendata/7707329152-registerdisqualified/data-20260920-structure-20150624.csv">data</a>
      <a href="https://data.nalog.ru/opendata/7707329152-registerdisqualified/structure-20150624.csv">structure</a>
      <td property="dc:valid" content="30.09.2026">30.09.2026</td>
    """

    release = disqualified.discover_fns_disqualified_release(
        now=NOW, fetch=lambda _url: (html, {})
    )

    assert release.source_data_date == date(2026, 9, 20)
    assert release.actual_until == date(2026, 9, 30)
    assert release.artifact_url.endswith("data-20260920-structure-20150624.csv")


def test_discovery_rejects_ambiguous_current_csv():
    html = b"""
      <a href="https://data.nalog.ru/opendata/7707329152-registerdisqualified/data-20260920-structure-20150624.csv">one</a>
      <a href="https://data.nalog.ru/opendata/7707329152-registerdisqualified/data-20260920-structure-20150625.csv">two</a>
      <a href="https://data.nalog.ru/opendata/7707329152-registerdisqualified/structure-20150624.csv">s1</a>
      <a href="https://data.nalog.ru/opendata/7707329152-registerdisqualified/structure-20150625.csv">s2</a>
    """

    with pytest.raises(SchemaMismatchError, match="current artifact is ambiguous"):
        disqualified.discover_fns_disqualified_release(
            now=NOW, fetch=lambda _url: (html, {})
        )


def test_csv_normalization_drops_unneeded_personal_data(tmp_path):
    source = tmp_path / "data.csv"
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(disqualified.EXPECTED_HEADER)
        writer.writerow([
            "247700068330",
            "ИВАНОВ ИВАН ИВАНОВИЧ",
            "19.03.2000",
            "Г. МОСКВА",
            "ООО ТЕСТ",
            "7700009911",
            "ДИРЕКТОР",
            "СТАТЬЯ",
            "ПРОТОКОЛЬНЫЙ ОРГАН",
            "СУДЬЯ",
            "МИРОВОЙ СУДЬЯ",
            "1 г 0 м 0 д",
            "01.01.2026",
            "31.12.2026",
        ])

    normalized, _checksum, counters = disqualified._normalize_disqualified_csv(
        source, data_date=date(2026, 9, 20)
    )
    row = json.loads(normalized.read_text(encoding="utf-8"))

    assert counters["records_seen"] == 1
    assert counters["name_only_matching_used"] is False
    assert row["organization_inn"] == "7700009911"
    assert "birth_date" not in row
    assert "birth_place" not in row
    assert "judge_name" not in row
    assert "protocol_authority" not in row


def test_postgresql_exact_inn_publish_and_replay_new_master(tmp_path, monkeypatch):
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    first_inn = "7700009911"
    second_inn = "7700009928"
    snapshot = tmp_path / "normalized-disqualified.jsonl"
    snapshot.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in (
                _normalized_row(first_inn, "R-1"),
                _normalized_row(second_inn, "R-2"),
                _normalized_row(None, "R-NAME-ONLY"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    from app.ingestion.fns_bulk_worker import _hash_file

    checksum, _size = _hash_file(snapshot)
    monkeypatch.setattr("app.ingestion.fns_bulk_worker.utc_now", lambda: NOW)
    release = disqualified.discover_fns_disqualified_release(
        now=NOW,
        fetch=lambda _url: (
            b'<a href="https://data.nalog.ru/opendata/7707329152-registerdisqualified/data-20260920-structure-20150624.csv">data</a>'
            b'<a href="https://data.nalog.ru/opendata/7707329152-registerdisqualified/structure-20150624.csv">structure</a>'
            b'<td property="dc:valid" content="30.09.2026">30.09.2026</td>',
            {},
        ),
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
                sa.select(DataSet).where(DataSet.code == disqualified.DATASET_CODE)
            )
            if dataset is None:
                dataset = DataSet(
                    source_id=source.id,
                    code=disqualified.DATASET_CODE,
                    name="FNS disqualified",
                    domain="disqualification",
                    update_mode="bulk",
                    data_format="csv",
                    priority=10,
                    enabled=False,
                )
                session.add(dataset)
                session.flush()
            session.execute(sa.delete(DisqualifiedPersonSnapshot).where(
                DisqualifiedPersonSnapshot.dataset_id == dataset.id
            ))
            session.execute(sa.delete(WorkerPublicationState).where(
                WorkerPublicationState.source_id == disqualified.SOURCE_ID
            ))
            dataset.last_data_date = None
            session.add(Company(
                inn=first_inn, name="First disqualified", entity_type="legal"
            ))
            session.flush()
            claim = SimpleNamespace(
                run_id=uuid4(), schedule_metadata=release.as_metadata()
            )
            result = HandlerResult(
                staging_result=StagingResult(
                    staging_pointer=snapshot.as_uri(),
                    checksum=checksum,
                    validation=ValidationResult(
                        accepted=True,
                        metadata={"release_identity": release.identity},
                    ),
                    metadata={"dataset_code": disqualified.DATASET_CODE},
                ),
                checksum_metadata={"artifact_sha256": "b" * 64},
                counters=ExecutionCounters(records_seen=3, records_written=3),
            )
            published = disqualified.publish_fns_disqualified_worker_result(
                session, claim, result
            )
            assert published.counters.records_published == 1
            assert published.change_summary.quarantined_records == 1
            stored = session.scalar(sa.select(DisqualifiedPersonSnapshot).where(
                DisqualifiedPersonSnapshot.dataset_id == dataset.id
            ))
            assert stored.organization_inn == first_inn
            assert stored.birth_date is None
            assert stored.birth_place is None
            assert stored.judge_name is None
            assert dataset.coverage["matching_states"]["name_only_matching_used"] is False

            state = WorkerPublicationState(
                source_id=disqualified.SOURCE_ID,
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
            session.add(Company(
                inn=second_inn, name="Second disqualified", entity_type="legal"
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
            replay = disqualified.publish_fns_disqualified_worker_result(
                session, replay_claim, HandlerResult()
            )
            assert replay.counters.records_published == 1
            again = disqualified.publish_fns_disqualified_worker_result(
                session, replay_claim, HandlerResult()
            )
            assert again.counters.records_published == 0
            assert session.scalar(sa.select(sa.func.count()).select_from(
                DisqualifiedPersonSnapshot
            ).where(DisqualifiedPersonSnapshot.dataset_id == dataset.id)) == 2
            assert state.generation == 1
    finally:
        transaction.rollback()
        connection.close()


def test_product_ip_not_applicable_and_stale_is_unavailable(monkeypatch):
    ip = disqualified_service.get_disqualified_check_for_inn("770000000001")
    assert ip["result"] == "not_applicable"
    assert ip["matching_state"] == "not_applicable_entity_type"
    assert ip["name_only_matching_used"] is False

    dataset = SimpleNamespace(
        id=10,
        enabled=True,
        last_data_date=date(2026, 9, 20),
        operational_status="current",
        official_actual_until=date(2026, 9, 30),
    )

    class Result:
        def scalar_one_or_none(self):
            return dataset

    class Session:
        def execute(self, _statement):
            return Result()

        def close(self):
            pass

    monkeypatch.setattr(disqualified_service, "get_session", Session)
    stale = disqualified_service.get_disqualified_check_for_inn(
        "7700000000", now=datetime(2026, 10, 1, tzinfo=timezone.utc)
    )
    assert stale["result"] == "unavailable"
    assert stale["reason"] == "dataset_stale"
    assert stale["name_only_matching_used"] is False
