from datetime import date, datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

from app.aggregators import company_product_aggregator
from app.ingestion.fns_sme_support import (
    parse_support_document,
    stream_xml_file,
)
from app.providers.fns_sme_support_provider import (
    FnsSmeSupportProvider,
    METADATA_URL,
    parse_release_metadata,
)
from app.services import fns_sme_support_service
from app.services.fns_sme_support_registry_service import (
    build_fns_sme_support_dataset_spec,
    build_fns_sme_support_source_spec,
)


CURRENT_ZIP = (
    "https://file.nalog.ru/opendata/7707329152-rsmppp/"
    "data-20260815-structure-20230615.zip"
)
CURRENT_XSD = (
    "https://file.nalog.ru/opendata/7707329152-rsmppp/"
    "structure-20230615.xsd"
)


def sample_metadata_html():
    return f"""
    <html><body>
      <a href="https://file.nalog.ru/opendata/7707329152-rsmppp/data-20260615-structure-20230615.zip">old</a>
      <a href="{CURRENT_ZIP}">current</a>
      <a href="{CURRENT_XSD}">xsd</a>
      <div>Дата последнего внесения изменений 15.08.2026</div>
      <div>Дата актуальности 15.09.2026</div>
    </body></html>
    """


def sample_xml():
    return """<?xml version="1.0" encoding="windows-1251"?>
    <Файл ИдФайл="fixture" ВерсФорм="4.04" ТипИнф="РМСП_ПП_ПОДДЕРЖ" КолДок="3">
      <ИдОтпр ИННЮЛ="7707329152" ДолжОтв="Тест" />
      <Документ ИдДок="SUP-LEGAL" ДатаСвед="2026-09-15" СрокПод="2026-12-31" ДатаОказ="2026-01-15" ИнфНаруш="2">
        <ИННЮЛ>7701234567</ИННЮЛ>
        <ОГРН>1027700000001</ОГРН>
        <ФормПод КодФорм="0001" НаимФорм="Финансовая поддержка" />
        <ВидПод КодВид="0001" НаимВид="Субсидия" />
        <РазмПод РазмПод="150000.00" ЕдПод="1" />
        <РегДок ИдРД="REG-1" />
      </Документ>
      <Документ ИдДок="SUP-IP" ДатаСвед="2026-09-15" СрокПод="2026-10-01" ДатаОказ="2026-02-01" ИнфНаруш="1">
        <ИННФЛ>770123456789</ИННФЛ>
        <ОГРНИП>326770000000001</ОГРНИП>
        <ФормПод КодФорм="0002" НаимФорм="Консультационная поддержка" />
        <ВидПод КодВид="0002" НаимВид="Консультация" />
        <РазмПод РазмПод="12.00" ЕдПод="3" />
        <Нарушения ВидНаруш="2" ДатаНаруш="2026-05-01" СрокНаруш="2026-06-01" />
        <РегДок ИдРД="REG-2" />
      </Документ>
      <Документ ИдДок="SUP-NPD" ДатаСвед="2026-09-15" СрокПод="2026-11-01" ДатаОказ="2026-03-01" ИнфНаруш="2">
        <ИННФЛ>771234567890</ИННФЛ>
        <ФормПод КодФорм="0003" НаимФорм="Образовательная поддержка" />
        <ВидПод КодВид="0003" НаимВид="Обучение" />
        <РазмПод РазмПод="1" ЕдПод="5" />
        <РегДок ИдРД="REG-3" />
      </Документ>
    </Файл>
    """.encode("cp1251")


class FakeResponse:
    status_code = 200
    text = sample_metadata_html()


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return FakeResponse()


class FakeDbResult:
    def __init__(self, *, scalar=None, rows=None):
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one_or_none(self):
        return self.scalar

    def scalar_one(self):
        return self.scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, results):
        self.results = list(results)

    def execute(self, _statement):
        return self.results.pop(0)

    def close(self):
        pass


def test_metadata_selects_current_bulk_release_and_relevance_date():
    release = parse_release_metadata(sample_metadata_html())
    assert release.data_url == CURRENT_ZIP
    assert release.structure_url == CURRENT_XSD
    assert release.modified_date == date(2026, 8, 15)
    assert release.data_date == date(2026, 9, 15)


def test_provider_discovers_official_metadata_page():
    client = FakeClient()
    release = FnsSmeSupportProvider(client=client).discover_release()
    assert release.data_url == CURRENT_ZIP
    assert client.calls == [METADATA_URL]


def test_parser_reads_legal_support_fact():
    import xml.etree.ElementTree as ET
    root = ET.fromstring(sample_xml())
    document = [child for child in root if child.tag == "Документ"][0]
    result = parse_support_document(
        document, provider_inn="7707329152", data_date=date(2026, 9, 15)
    )
    assert result["source_document_id"] == "SUP-LEGAL"
    assert result["recipient_inn"] == "7701234567"
    assert result["recipient_ogrn"] == "1027700000001"
    assert result["recipient_kind"] == "legal"
    assert result["provider_inn"] == "7707329152"
    assert result["support_form_name"] == "Финансовая поддержка"
    assert result["support_type_name"] == "Субсидия"
    assert result["amounts"] == [
        {"value": "150000.00", "unit_code": "1", "unit_name": "рубль"}
    ]
    assert result["regulatory_document_ids"] == ["REG-1"]


def test_stream_scans_npd_but_excludes_person_from_company_storage():
    accepted = []
    result = stream_xml_file(
        BytesIO(sample_xml()), data_date=date(2026, 9, 15), consume=accepted.append
    )
    assert result == {
        "expected_documents": 3,
        "source_records": 3,
        "eligible_records": 2,
        "excluded_npd_records": 1,
    }
    assert [row["recipient_kind"] for row in accepted] == [
        "legal", "individual_entrepreneur_or_kfh"
    ]
    assert all(row["recipient_inn"] != "771234567890" for row in accepted)


def test_stream_rejects_incomplete_snapshot_count():
    broken = sample_xml().replace('КолДок="3"'.encode('cp1251'), 'КолДок="4"'.encode('cp1251'))
    with pytest.raises(ValueError, match="КолДок=4"):
        stream_xml_file(
            BytesIO(broken), data_date=date(2026, 9, 15), consume=lambda row: None
        )


def test_stream_requires_koldok_for_full_snapshot_semantics():
    broken = sample_xml().replace(' КолДок="3"'.encode('cp1251'), b'')
    with pytest.raises(ValueError, match="КолДок"):
        stream_xml_file(
            BytesIO(broken), data_date=date(2026, 9, 15), consume=lambda row: None
        )


def test_complete_snapshot_absence_is_not_found(monkeypatch):
    dataset = SimpleNamespace(
        id=10,
        enabled=True,
        last_data_date=date(2026, 9, 15),
        operational_status="current",
        official_actual_until=date(2026, 10, 15),
    )
    run = SimpleNamespace(
        id=20,
        data_date=date(2026, 9, 15),
        details={
            "complete_snapshot": True,
            "eligible_records": 2,
            "source_records": 3,
            "excluded_npd_records": 1,
        },
    )
    session = FakeSession(
        [
            FakeDbResult(scalar=dataset),
            FakeDbResult(scalar=run),
            FakeDbResult(scalar=2),
            FakeDbResult(scalar=0),
            FakeDbResult(rows=[]),
        ]
    )
    monkeypatch.setattr(
        fns_sme_support_service, "get_session", lambda: session
    )
    check = fns_sme_support_service.get_fns_sme_support_check_for_inn(
        "7701234567"
    )
    assert check["result"] == "not_found"
    assert check["checked"] is True
    assert check["applicable"] is True
    assert check["is_support_recipient"] is False
    assert check["data_date"] == date(2026, 9, 15)


def test_incomplete_snapshot_is_unavailable_not_false(monkeypatch):
    dataset = SimpleNamespace(
        id=10,
        enabled=True,
        last_data_date=None,
        operational_status="unavailable",
        official_actual_until=None,
    )
    session = FakeSession(
        [
            FakeDbResult(scalar=dataset),
            FakeDbResult(scalar=None),
        ]
    )
    monkeypatch.setattr(
        fns_sme_support_service, "get_session", lambda: session
    )
    check = fns_sme_support_service.get_fns_sme_support_check_for_inn(
        "7701234567"
    )
    assert check["result"] == "unavailable"
    assert check["checked"] is False
    assert check["is_support_recipient"] is None


def test_expired_official_release_blocks_support_clean_negative(monkeypatch):
    dataset = SimpleNamespace(
        id=10,
        enabled=True,
        last_data_date=date(2026, 9, 15),
        operational_status="current",
        official_actual_until=date(2026, 10, 15),
    )
    session = FakeSession([FakeDbResult(scalar=dataset)])
    monkeypatch.setattr(fns_sme_support_service, "get_session", lambda: session)

    check = fns_sme_support_service.get_fns_sme_support_check_for_inn(
        "7701234567",
        now=datetime(2026, 10, 16, tzinfo=timezone.utc),
    )

    assert check["result"] == "unavailable"
    assert check["checked"] is False
    assert check["reason"] == "dataset_stale"


def test_product_aggregator_adds_support_source_only_after_checked_snapshot(monkeypatch):
    company = {"inn": "7701234567", "sources_used": ["fns"]}
    monkeypatch.setattr(
        company_product_aggregator,
        "get_fns_sme_support_check_for_inn",
        lambda inn: {"result": "found", "record_count": 1},
    )
    result = company_product_aggregator.enrich_company_with_fns_sme_support(company)
    assert result["fns_sme_support_check"]["result"] == "found"
    assert "fns_sme_support" in result["sources_used"]
    assert company["sources_used"] == ["fns"]


def test_unavailable_support_source_is_not_marked_used(monkeypatch):
    company = {"inn": "7701234567", "sources_used": []}
    monkeypatch.setattr(
        company_product_aggregator,
        "get_fns_sme_support_check_for_inn",
        lambda inn: {"result": "unavailable"},
    )
    result = company_product_aggregator.enrich_company_with_fns_sme_support(company)
    assert "fns_sme_support" not in result["sources_used"]


def test_registry_spec_is_official_monthly_bulk_xml():
    source = build_fns_sme_support_source_spec()
    dataset = build_fns_sme_support_dataset_spec(10)
    assert source["code"] == "fns"
    assert source["source_type"] == "official"
    assert dataset["code"] == "fns_sme_support"
    assert dataset["update_mode"] == "bulk"
    assert dataset["data_format"] == "xml"
    assert dataset["refresh_schedule"] == "monthly"
    assert dataset["source_url"] == METADATA_URL


def test_support_partial_compiles():
    env = Environment(loader=FileSystemLoader("templates"))
    assert env.get_template("partials/fns_sme_support.html") is not None
