from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import erknm_worker as worker
from app.models.company import Company
from app.models.erknm import ErknmInspection
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerPublicationState
from app.services import erknm_service


NOW = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
INN = "7701364231"
OGRN = "1137746586251"
ERPID = "77260061000120428959"
SOURCE_URL = (
    "https://proverki.gov.ru/blob/erknm-opendata/2026/9/"
    "data-20260924-structure-20220125.zip"
)
XSD_URL = "https://proverki.gov.ru/blob/erknm-opendata/structure-20220125.xsd"


def _source_xml(*, inn=INN, ogrn=OGRN, erpid=ERPID, status="Запланировано"):
    return (
        '<INSPECTIONS><INSPECTION ERPID="%s" STATUS="%s" START_DATE="2026-09-26">'
        '<SUBJECT INN="%s" OGRN="%s" NAME="ООО ТЕСТ" TYPE="ЮЛ" />'
        "</INSPECTION></INSPECTIONS>"
    ) % (erpid, status, inn, ogrn)


XSD = b'''<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="INSPECTIONS">
    <xs:complexType><xs:sequence>
      <xs:element name="INSPECTION" minOccurs="0" maxOccurs="unbounded">
        <xs:complexType><xs:sequence>
          <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
        </xs:sequence><xs:anyAttribute processContents="lax"/></xs:complexType>
      </xs:element>
    </xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>'''


def _zip_bytes(xml):
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("data.xml", xml)
    return stream.getvalue()


def _metadata_bytes():
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<root>
  <identifier>7710146102-inspection-2026-9</identifier>
  <title>ФГИС ЕРКНМ</title><subject>Проверки на 2026 год по 248 ФЗ</subject>
  <erknmStructure><structureversion>{XSD_URL}</structureversion></erknmStructure>
  <data><dataversion>
    <source>{SOURCE_URL}</source><created>20260924</created>
    <provenance>Обновление набора данных от 20260924</provenance>
  </dataversion></data>
</root>'''.encode()


def _fetcher(xml=None):
    metadata = _metadata_bytes()
    payloads = {
        worker.build_metadata_url(2026, 9): metadata,
        SOURCE_URL: _zip_bytes(xml or _source_xml()),
        XSD_URL: XSD,
    }

    def fetch(url):
        value = payloads[url]
        return value, {"content-length": str(len(value))}, 200

    return fetch


class FakeContext:
    def __init__(self, metadata, raw_root: Path):
        self.schedule_metadata = {**metadata, "raw_root": str(raw_root)}
        self.progress = []

    def ensure_active(self, *, now):
        assert now.tzinfo is not None

    def heartbeat(self):
        return None

    def report_counters(self, counters):
        self.progress.append(counters)


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


def test_handler_validates_xsd_and_persists_immutable_raw(tmp_path):
    release = worker.discover_erknm_release(now=NOW, fetch=_fetcher())
    first = worker.run_erknm_handler(
        FakeContext(release.as_metadata(), tmp_path), fetch=_fetcher()
    )
    second = worker.run_erknm_handler(
        FakeContext(release.as_metadata(), tmp_path), fetch=_fetcher()
    )

    assert first.raw_artifacts[0].checksum == second.raw_artifacts[0].checksum
    assert first.staging_result.staging_pointer == second.staging_result.staging_pointer
    assert first.staging_result.validation.metadata["records_unique"] == 1
    assert first.staging_result.validation.metadata["official_actual_until"] == "2026-09-25"
    assert first.counters.records_seen == 1
    raw_path = Path(first.raw_artifacts[0].artifact_reference.removeprefix("file://"))
    assert raw_path.is_file()
    assert (raw_path.parent / "manifest.json").is_file()


def test_schedule_same_release_is_daily_check_only_without_redownload(monkeypatch, tmp_path):
    release = worker.discover_erknm_release(now=NOW, fetch=_fetcher())
    state = SimpleNamespace(
        validation_metadata={"validation": {"release_identity": release.identity}}
    )
    approval = SimpleNamespace(approved=True, enabled=True, live_mode=False)

    class Session:
        def get(self, model, _key):
            return approval if model is worker.WorkerHandlerRegistration else state

    calls = []
    monkeypatch.setattr(
        worker, "create_job", lambda _session, **kwargs: calls.append(kwargs) or kwargs
    )
    result = worker.schedule_erknm_check(
        Session(), raw_root=tmp_path, now=NOW, fetch=_fetcher()
    )

    assert result["schedule_metadata"]["check_only"] is True
    assert result["schedule_metadata"]["check_frequency"] == "daily"
    assert "check:2026-09-25" in result["idempotency_key"]
    assert len(calls) == 1


def test_stale_dataset_blocks_clean_negative_product(monkeypatch):
    dataset = SimpleNamespace(
        operational_status="current",
        official_actual_until=NOW.date() - timedelta(days=1),
        last_data_date=NOW.date() - timedelta(days=1),
    )

    class Session:
        def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: dataset)

        def close(self):
            return None

    monkeypatch.setattr(erknm_service, "get_session", lambda: Session())
    result = erknm_service.get_erknm_check_for_company(INN, OGRN)
    assert result["checked"] is False
    assert result["result"] == "unavailable"
    assert result["reason"] == "dataset_stale"


def test_postgresql_publication_and_next_day_check_preserve_generation(
    worker_db, tmp_path, monkeypatch
):
    suffix = str(uuid4().int % 10**8).zfill(8)
    inn = f"77{suffix}"
    ogrn = f"1{str(uuid4().int % 10**12).zfill(12)}"
    erpid = str(uuid4().int)[:20]
    fetch = _fetcher(_source_xml(inn=inn, ogrn=ogrn, erpid=erpid))
    release = worker.discover_erknm_release(now=NOW, fetch=fetch)
    first = worker.run_erknm_handler(
        FakeContext(release.as_metadata(), tmp_path), fetch=fetch
    )
    monkeypatch.setattr(worker, "utc_now", lambda: NOW)

    with worker_db() as session:
        source = session.scalar(
            sa.select(DataSource).where(DataSource.code == "genproc")
        )
        if source is None:
            source = DataSource(
                code="genproc", name="Genproc", source_type="official", priority=10, enabled=True
            )
            session.add(source)
            session.flush()
        dataset = session.scalar(
            sa.select(DataSet).where(DataSet.code == worker.DATASET_CODE)
        )
        if dataset is None:
            dataset = DataSet(
                source_id=source.id,
                code=worker.DATASET_CODE,
                name="ERKNM",
                domain="inspections",
                update_mode="bulk",
                data_format="xml",
                priority=10,
                enabled=False,
            )
            session.add(dataset)
            session.flush()
        session.execute(
            sa.delete(ErknmInspection).where(ErknmInspection.dataset_id == dataset.id)
        )
        session.execute(
            sa.delete(WorkerPublicationState).where(
                WorkerPublicationState.source_id == worker.SOURCE_ID
            )
        )
        session.add(Company(inn=inn, ogrn=ogrn, name="Exact", entity_type="legal"))
        session.flush()

        claim = SimpleNamespace(schedule_metadata={**release.as_metadata(), "check_only": False})
        published = worker.publish_erknm_worker_result(session, claim, first)
        assert published.change_summary.new_facts == 1
        assert published.change_summary.matched_companies == 1
        assert dataset.coverage["change_summary"]["new_facts"] == 1

        state = WorkerPublicationState(
            source_id=worker.SOURCE_ID,
            active_pointer=published.staging_result.staging_pointer,
            generation=1,
            last_fencing_token=1,
            validation_metadata={
                "checksum": published.staging_result.checksum,
                "validation": published.staging_result.validation.metadata,
                "staging": published.staging_result.metadata,
            },
        )
        session.add(state)
        session.flush()
        last_success = dataset.last_success_at

        next_day = NOW + timedelta(days=1)
        checked_release = worker.discover_erknm_release(now=next_day, fetch=_fetcher())
        check_metadata = {**checked_release.as_metadata(), "check_only": True}
        check_result = worker.run_erknm_handler(
            FakeContext(check_metadata, tmp_path), fetch=lambda _url: pytest.fail("redownload")
        )
        monkeypatch.setattr(worker, "utc_now", lambda: next_day)
        checked = worker.publish_erknm_worker_result(
            session, SimpleNamespace(schedule_metadata=check_metadata), check_result
        )

        assert checked.staging_result is None
        assert checked.change_summary.unchanged_facts == 1
        assert checked.counters.records_published == 0
        assert dataset.last_success_at == last_success
        assert dataset.checked_at == next_day
        assert dataset.official_actual_until == next_day.date()
        assert session.get(WorkerPublicationState, worker.SOURCE_ID).generation == 1
        assert session.scalar(
            sa.select(sa.func.count()).select_from(ErknmInspection).where(
                ErknmInspection.dataset_id == dataset.id
            )
        ) == 1
