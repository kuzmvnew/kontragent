from datetime import date, datetime, timezone
from io import BytesIO
import json
from types import SimpleNamespace
from uuid import uuid4

from openpyxl import Workbook
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database.postgres import engine
from app.ingestion import fns_registry_master as registry
from app.ingestion import girbo_worker as girbo
from app.ingestion import mintrans_ted_worker as mintrans
from app.models.company import Company, CompanyManager
from app.models.girbo import GirboAccountingReport
from app.models.mintrans_ted import MintransTedEntry, MintransTedRawArtifact, TransportForwardingRegistryListing
from app.models.registry_master import RegistrySourceCheckpoint
from app.models.source import DataSet, DataSource
from app.models.worker import WorkerPublicationState
from app.services import source_factory_projection_service as projections
from app.worker.contracts import ExecutionCounters, HandlerResult, StagingResult, ValidationResult
from app.worker.errors import AccessRequiredError


NOW = datetime(2026, 9, 25, 8, tzinfo=timezone.utc)


def test_access_gates_report_names_without_secret_values():
    ready, missing = registry.access_state({})
    assert ready is False
    assert set(missing) == set(registry.REQUIRED_ACCESS)
    with pytest.raises(AccessRequiredError) as caught:
        registry.require_access({})
    assert "PASSWORD=" not in str(caught.value)

    ready, missing = girbo.access_state({})
    assert ready is False
    assert set(missing) == set(girbo.REQUIRED_ACCESS)


def test_egrul_and_egrip_formats_stream_to_one_normalized_contract():
    legal_xml = """<?xml version='1.0' encoding='windows-1251'?>
    <Файл ВерсФорм='4.08'><Документ><СвЮЛ ИНН='7700009911' КПП='770001001'
      ОГРН='1027700000001' ДатаОГРН='2002-08-01' ДатаВып='2026-09-24'>
      <СвНаимЮЛ НаимЮЛПолн='ООО ТЕСТ' НаимЮЛСокр='ТЕСТ'/>
      <СвОКВЭД><СвОКВЭДОсн КодОКВЭД='62.01' НаимОКВЭД='Разработка ПО'/></СвОКВЭД>
      <СведДолжнФЛ><СвФЛ Фамилия='ИВАНОВ' Имя='ИВАН' Отчество='ИВАНОВИЧ'/><СвДолжн НаимДолжн='ДИРЕКТОР'/></СведДолжнФЛ>
    </СвЮЛ></Документ></Файл>""".encode("cp1251")
    legal = list(registry.iter_registry_xml(BytesIO(legal_xml), registry.SPECS["fns_egrul"]))
    assert legal == [{
        "inn": "7700009911", "kpp": "770001001", "ogrn": "1027700000001",
        "entity_type": "legal", "name": "ООО ТЕСТ", "short_name": "ТЕСТ",
        "full_name": "ООО ТЕСТ", "status": None, "registration_date": "2002-08-01",
        "termination_date": None, "address": None, "region_code": None,
        "okved": "62.01", "activity": "Разработка ПО",
        "leaders": [{"full_name": "ИВАНОВ ИВАН ИВАНОВИЧ", "position": "ДИРЕКТОР"}],
        "source_data_date": "2026-09-24", "source_record_key": "1027700000001:2026-09-24",
    }]

    ip_xml = """<?xml version='1.0' encoding='windows-1251'?>
    <Файл ВерсФорм='4.07'><СвИП ИННФЛ='345907922962' ОГРНИП='326770000000001'
    ДатаОГРНИП='2026-01-10' ДатаВып='2026-09-24'><СвФЛ Фамилия='ПЕТРОВ'
    Имя='ПЕТР' Отчество='ПЕТРОВИЧ'/></СвИП></Файл>""".encode("cp1251")
    ip = list(registry.iter_registry_xml(BytesIO(ip_xml), registry.SPECS["fns_egrip"]))
    assert ip[0]["entity_type"] == "individual_entrepreneur"
    assert ip[0]["name"] == "ПЕТРОВ ПЕТР ПЕТРОВИЧ"
    assert ip[0]["leaders"] == []


def test_full_delta_checkpoint_selection_is_ordered_and_gap_safe():
    artifacts = (
        registry.RegistryArtifact("EGRUL_408/delta-2026-01-03.zip", "delta-2026-01-03.zip", date(2026, 1, 3), "delta", "4.08"),
        registry.RegistryArtifact("EGRUL_408/full-2026-01-01.zip", "full-2026-01-01.zip", date(2026, 1, 1), "full", "4.08"),
        registry.RegistryArtifact("EGRUL_408/delta-2026-01-02.zip", "delta-2026-01-02.zip", date(2026, 1, 2), "delta", "4.08"),
    )
    selected = registry.select_release_chain(artifacts, None)
    assert [item.data_date for item in selected] == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    checkpoint = SimpleNamespace(baseline_accepted=True, last_delta_date=date(2026, 1, 2), last_full_date=date(2026, 1, 1))
    assert [item.data_date for item in registry.select_release_chain(artifacts, checkpoint)] == [date(2026, 1, 3)]
    with pytest.raises(registry.SchemaMismatchError, match="delta chain has a gap"):
        registry.select_release_chain((artifacts[0], artifacts[1]), None)


def test_mintrans_current_xlsx_headers_stream_and_drop_unneeded_personal_data(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "Реестровый номер", "Дата включения в реестр уведомлений ТЭД", "ИНН ЮЛ", "ОГРН ЮЛ",
        "Полное наименование юридического лица", "Юридический адрес", "ИНН ИП", "ОГРН ИП",
        "Фамилия ИП", "Имя ИП", "Отчество ИП (при наличии)",
    ])
    sheet.append(["TED-1", "07.07.2026", "7707083893", None, "ООО ТЕСТ", "секретный адрес", None, None, None, None, None])
    source = tmp_path / "current.xlsx"
    workbook.save(source)
    output = tmp_path / "normalized.jsonl"

    stats = mintrans.normalize_mintrans_xlsx(source, output)
    row = json.loads(output.read_text())

    assert stats["source_records"] == 1
    assert stats["normalized_records"] == 1
    assert row["inn"] == "7707083893"
    assert "name" not in row and "address" not in row and "Фамилия" not in output.read_text()


def test_mintrans_official_discovery_uses_document_results_and_russian_date():
    html = """<div class="document-list-item">
      <span class="date-span"> 7 Июля 2026</span>
      <div class="document-download">
        <a class="download-item xlsx" download href="/file/564924">
          <p>Реестр уведомлений о транспортно-экспедиционной деятельности</p>
        </a>
      </div>
    </div>""".encode()
    provider = mintrans.MintransOfficialProvider()
    requested = []
    provider._fetch = lambda url: (requested.append(url) or html, {  # type: ignore[method-assign]
        "content-type": "text/html; charset=UTF-8"
    })

    release = provider.discover(max_pages=1)

    assert release.artifact_id == "564924"
    assert release.source_data_date == date(2026, 7, 7)
    assert "search_type=2" in requested[0]
    assert "check_name=1" in requested[0]


def test_girbo_parser_requires_identity_and_preserves_statement_values():
    xml = """<?xml version='1.0' encoding='utf-8'?>
    <Файл ВерсФорм='5.11'><Документ ОКЕИ='384'><СвНП ИННЮЛ='7700009911'/>
      <Баланс><Актив СумОтч='1000'/><Пассив СумОтч='1000'><Капитал СумОтч='400'/></Пассив></Баланс>
      <ФинРез><Выруч СумОтч='700' СумПрдщ='650'/><СебестПрод СумОтч='500'/><ЧистПрибУб СумОтч='120'/></ФинРез>
    </Документ></Файл>""".encode()
    metadata = {
        "id": "BFO-1", "fileName": "report.xml", "period": 2025,
        "periodType": "TWELVE_MONTHS", "reportType": "BFO_TKS",
        "fileType": "BFO", "uploadDate": "2026-04-01",
    }
    row = girbo.parse_girbo_xml(
        xml, requested_inn="7700009911", metadata=metadata,
        raw_checksum="a" * 64,
    )
    assert row["revenue"] == "700"
    assert row["expenses"] == "500"
    assert row["profit_loss"] == "120"
    assert row["assets"] == "1000"
    assert row["liabilities"] == "600"
    assert row["statement_values"]["_document"]["unit_code"] == "384"
    assert row["statement_values"]["Выруч"] == {
        "current": "700", "previous": "650", "previous_two": None,
    }
    with pytest.raises(girbo.SchemaMismatchError, match="INN differs"):
        girbo.parse_girbo_xml(
            xml, requested_inn="7700009928", metadata=metadata,
            raw_checksum="a" * 64,
        )


@pytest.fixture
def wave_db():
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()


def _dataset(session, code, domain):
    source = session.scalar(sa.select(DataSource).where(DataSource.code == f"test-{code}"))
    if source is None:
        source = DataSource(code=f"test-{code}", name=code, source_type="official", priority=1, enabled=True)
        session.add(source); session.flush()
    dataset = session.scalar(sa.select(DataSet).where(DataSet.code == code))
    if dataset is None:
        dataset = DataSet(source_id=source.id, code=code, name=code, domain=domain, update_mode="bulk", data_format="json", priority=1, enabled=False)
        session.add(dataset); session.flush()
    return dataset


def test_postgresql_egrul_publication_populates_manager_provenance(wave_db, tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "utc_now", lambda: NOW)
    snapshot = tmp_path / "egrul.jsonl"
    snapshot.write_text(json.dumps({
        "inn": "7700009911", "kpp": "770001001", "ogrn": "1027700000001",
        "entity_type": "legal", "name": "ООО ТЕСТ", "short_name": "ТЕСТ", "full_name": "ООО ТЕСТ",
        "status": "ДЕЙСТВУЮЩЕЕ", "registration_date": "2002-08-01", "termination_date": None,
        "address": "Москва", "region_code": "77", "okved": "62.01", "activity": "Разработка ПО",
        "leaders": [{"full_name": "ИВАНОВ ИВАН ИВАНОВИЧ", "position": "ДИРЕКТОР"}],
        "source_data_date": "2026-09-24", "source_record_key": "1027700000001:2026-09-24",
    }, ensure_ascii=False) + "\n")
    result = HandlerResult(
        staging_result=StagingResult(snapshot.as_uri(), ValidationResult(accepted=True, metadata={
            "release_identity": "full-1", "source_data_date": "2026-09-24", "source_records": 1,
            "imported_records": 1, "rejected_records": 0,
            "chain": [{"kind": "full", "data_date": "2026-09-24", "format_version": "4.08", "remote_path": "full.zip"}],
        }), checksum="abc"), counters=ExecutionCounters(records_seen=1, records_written=1),
    )
    with wave_db() as session:
        dataset = _dataset(session, "fns_egrul", "registry")
        session.execute(sa.delete(CompanyManager).where(CompanyManager.company_id.in_(sa.select(Company.id).where(Company.inn == "7700009911"))))
        session.execute(sa.delete(Company).where(Company.inn == "7700009911"))
        session.execute(sa.delete(RegistrySourceCheckpoint).where(RegistrySourceCheckpoint.source_id == "fns_egrul"))
        published = registry.publish_registry_result(
            session,
            SimpleNamespace(run_id=None, schedule_metadata={"release_identity": "full-1", "entity_limit": 450, "baseline_approved": True, "backup_reference": "backup://verified"}),
            result, spec=registry.SPECS["fns_egrul"],
        )
        company = session.scalar(sa.select(Company).where(Company.inn == "7700009911"))
        manager = session.scalar(sa.select(CompanyManager).where(CompanyManager.company_id == company.id))
        assert company.master_dataset_id == dataset.id
        assert manager.full_name == "ИВАНОВ ИВАН ИВАНОВИЧ"
        assert manager.source_dataset_id == dataset.id
        assert manager.source_data_date == date(2026, 9, 24)
        assert published.counters.records_published == 1
        assert session.get(RegistrySourceCheckpoint, "fns_egrul").baseline_accepted is True


def test_postgresql_mintrans_same_release_replays_new_master_idempotently(wave_db, tmp_path, monkeypatch):
    monkeypatch.setattr(mintrans, "utc_now", lambda: NOW)
    first_inn, second_inn = "7700009911", "7700009928"
    snapshot = tmp_path / "mintrans.jsonl"
    rows = []
    for number, inn in enumerate((first_inn, second_inn), 1):
        canonical = {"inn": inn, "ogrn": None, "registry_number": f"TED-{number}", "included_at": "2026-07-07"}
        rows.append({"kind": "entry", "source_row_number": number + 1, **canonical, "row_hash": __import__("hashlib").sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest(), "validation_state": "valid"})
    snapshot.write_text("".join(json.dumps(row) + "\n" for row in rows))
    checksum = "a" * 64
    raw = tmp_path / "artifact.xlsx"; raw.write_bytes(b"test")
    manifest = {"filename": "registry.xlsx", "source_url": "https://www.mintrans.gov.ru/file/564924", "artifact_id": "564924", "sha256": checksum}
    result = HandlerResult(
        raw_artifacts=(), staging_result=StagingResult(snapshot.as_uri(), ValidationResult(accepted=True, metadata={
            "release_identity": "564924:2026-07-07:registry.xlsx", "source_data_date": "2026-07-07",
            "artifact_sha256": checksum, "artifact_size": 4, "artifact_reference": raw.as_uri(), "manifest": manifest,
            "source_records": 2, "normalized_records": 2, "quarantined_records": 0, "duplicate_records": 0,
        }), checksum="b" * 64), counters=ExecutionCounters(records_seen=2, records_written=2),
    )
    claim = SimpleNamespace(schedule_metadata={"release_identity": "564924:2026-07-07:registry.xlsx"})
    with wave_db() as session:
        dataset = _dataset(session, mintrans.DATASET_CODE, "transport_forwarding")
        session.execute(sa.delete(TransportForwardingRegistryListing).where(TransportForwardingRegistryListing.dataset_id == dataset.id))
        session.execute(sa.delete(MintransTedEntry).where(MintransTedEntry.dataset_id == dataset.id))
        session.execute(sa.delete(MintransTedRawArtifact).where(MintransTedRawArtifact.dataset_id == dataset.id))
        session.execute(sa.delete(WorkerPublicationState).where(WorkerPublicationState.source_id == mintrans.SOURCE_ID))
        session.execute(sa.delete(Company).where(Company.inn.in_((first_inn, second_inn))))
        session.add(Company(inn=first_inn, name="First", entity_type="legal")); session.flush()
        published = mintrans.publish_mintrans_ted_result(session, claim, result)
        state = WorkerPublicationState(
            source_id=mintrans.SOURCE_ID, active_pointer=snapshot.as_uri(), generation=1,
            last_fencing_token=1, validation_metadata={"checksum": "b" * 64, "validation": published.staging_result.validation.metadata},
        )
        session.add(state); session.flush()
        assert published.counters.records_published == 1
        session.add(Company(inn=second_inn, name="Second", entity_type="legal")); session.flush()
        checked = mintrans.publish_mintrans_ted_result(session, claim, HandlerResult(checksum_metadata={"check_only": True}))
        again = mintrans.publish_mintrans_ted_result(session, claim, HandlerResult(checksum_metadata={"check_only": True}))
        assert checked.change_summary.replayed_facts == 1
        assert again.change_summary.replayed_facts == 0
        assert dataset.coverage["successful_scheduled_checks"] == 3
        assert dataset.coverage["operational_accepted"] is True
        assert session.scalar(sa.select(sa.func.count()).select_from(TransportForwardingRegistryListing).where(TransportForwardingRegistryListing.dataset_id == dataset.id)) == 2
        proxy = SimpleNamespace(
            scalar=session.scalar, scalars=session.scalars, close=lambda: None
        )
        monkeypatch.setattr(projections, "get_session", lambda: proxy)
        first_projection = projections.get_mintrans_ted_check_for_company(
            company_id=session.scalar(sa.select(Company.id).where(Company.inn == first_inn)),
            now=NOW,
        )
        assert first_projection["result"] == "found"
        assert first_projection["listings"][0]["registry_number"] == "TED-1"


def test_postgresql_girbo_correction_retains_history(wave_db, tmp_path, monkeypatch):
    monkeypatch.setattr(girbo, "utc_now", lambda: NOW)
    with wave_db() as session:
        dataset = _dataset(session, girbo.DATASET_CODE, "financials")
        session.execute(sa.delete(GirboAccountingReport).where(GirboAccountingReport.dataset_id == dataset.id))
        company = session.scalar(sa.select(Company).where(Company.inn == "7700009911"))
        if company is None:
            company = Company(inn="7700009911", name="GIRBO", entity_type="legal"); session.add(company); session.flush()
        for index, revenue in enumerate(("100", "120"), 1):
            path = tmp_path / f"girbo-{index}.jsonl"
            path.write_text(json.dumps({
                "inn": company.inn, "report_id": "BFO-1", "reporting_year": 2025,
                "publication_date": "2026-04-01", "correction_date": "2026-05-01" if index == 2 else None,
                "source_data_date": "2026-05-01" if index == 2 else "2026-04-01",
                "statement_values": {"2110": revenue}, "revenue": revenue, "expenses": None,
                "profit_loss": None, "assets": None, "liabilities": None, "equity": None,
                "raw_checksum": str(index) * 64,
            }) + "\n")
            result = HandlerResult(staging_result=StagingResult(path.as_uri(), ValidationResult(accepted=True, metadata={
                "source_records": 1, "normalized_reports": 1,
                "source_data_date": "2026-05-01", "years": [2025],
            }), checksum=str(index) * 64))
            girbo.publish_girbo_result(session, SimpleNamespace(schedule_metadata={}), result)
        reports = list(session.scalars(sa.select(GirboAccountingReport).where(GirboAccountingReport.dataset_id == dataset.id).order_by(GirboAccountingReport.revision)))
        assert [report.revision for report in reports] == [1, 2]
        assert [report.is_current for report in reports] == [False, True]
        assert str(reports[1].revenue) == "120.00"
        proxy = SimpleNamespace(
            scalar=session.scalar, scalars=session.scalars, close=lambda: None
        )
        monkeypatch.setattr(projections, "get_session", lambda: proxy)
        projection = projections.get_girbo_accounting_check_for_company(
            company_id=company.id, now=NOW
        )
        assert projection["result"] == "found"
        assert projection["reports"][0]["reporting_year"] == 2025
