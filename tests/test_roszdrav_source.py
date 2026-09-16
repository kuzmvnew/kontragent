from datetime import date
import hashlib
import ssl
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader
import pytest

from app.aggregators import company_product_aggregator
from app.ingestion.roszdrav import (
    parse_clinical_csv,
    parse_license_xml,
    publish_snapshots,
    validate_complete_snapshot,
)
from app.providers.roszdrav_provider import (
    MEDICAL_DEVICE_SEARCH_URL,
    LICENSE_SEARCH_URL,
    RUSSIAN_TRUSTED_ROOT_CA,
    RoszdravMedicalDeviceProvider,
    RoszdravProviderError,
    RoszdravUnifiedLicenseProvider,
    parse_medical_device_payload,
    parse_unified_license_payload,
)
from app.services.roszdrav_registry_service import (
    build_roszdrav_dataset_specs,
    build_roszdrav_source_spec,
)
from app.services.roszdrav_service import get_roszdrav_medical_device_company_check


LICENSE_XML = b'''<?xml version="1.0" encoding="utf-8"?>
<licenses_list><licenses>
<name>Roszdravnadzor</name><activity_type>Pharma</activity_type>
<full_name_licensee>Test LLC</full_name_licensee><form>LLC</form>
<address>Moscow</address><ogrn>1027700000000</ogrn><inn>7701234567</inn>
<work_address_list><address_place><address>Work address</address><region>Moscow</region>
<works><work>Storage</work><work>Retail</work></works></address_place></work_address_list>
<number>L-001</number><date>01.09.2026</date><date_order>02.09.2026</date_order>
<date_register>03.09.2026</date_register><termination></termination>
<date_termination></date_termination><information_suspension_resumption></information_suspension_resumption>
<information_cancellation></information_cancellation></licenses></licenses_list>'''


def unified_row(inn="7701234567"):
    return {
        "DT_RowId": "lic_1", "col1": {"label": "REG-1"},
        "col2": {"label": "01.09.2026"}, "col3": {"label": "Test LLC"},
        "col4": {"label": "Roszdravnadzor"}, "col5": {"label": "Moscow"},
        "col6": {"label": "1027700000000"}, "col7": {"label": inn},
        "col8": {"label": "123"}, "col9": {"label": "L-001"},
        "col10": {"label": "01.09.2026"}, "col11": {"label": "01.09.2026"},
        "col12": {"label": "Forever"}, "col13": {"label": ""},
        "col14": {"label": ""}, "col15": {"label": ""},
        "col16": {"label": ""}, "col17": {"label": ""},
        "col18": {"label": "Order"}, "objects": [{"activity": "Storage"}],
    }


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_license_xml_parser_preserves_exact_identifiers_and_works():
    parsed = parse_license_xml(LICENSE_XML, category="pharma", data_date=date(2026, 9, 13))
    assert parsed["source_records"] == 1
    assert parsed["rejected_records"] == 0
    row = parsed["records"][0]
    assert row["inn"] == "7701234567"
    assert row["ogrn"] == "1027700000000"
    assert row["license_number"] == "L-001"
    assert row["start_date"] == date(2026, 9, 3)
    assert row["work_places"][0]["works"] == ["Storage", "Retail"]


def test_snapshot_validation_rejects_any_skipped_record():
    parsed = parse_license_xml(
        LICENSE_XML.replace(b"7701234567", b"bad"),
        category="pharma", data_date=date(2026, 9, 13),
    )
    with pytest.raises(ValueError, match="Пустой snapshot|отклонённые"):
        validate_complete_snapshot(parsed)


def test_group_publication_validates_every_snapshot_before_opening_database(monkeypatch):
    valid = parse_license_xml(
        LICENSE_XML, category="pharma", data_date=date(2026, 9, 13)
    )
    invalid = {**valid, "rejected_records": 1}
    monkeypatch.setattr(
        "app.ingestion.roszdrav.get_session",
        lambda: pytest.fail("Database transaction must not start"),
    )
    with pytest.raises(ValueError, match="отклонённые"):
        publish_snapshots([
            {"dataset_code": "one", "parsed": valid, "model": object, "data_date": date(2026, 9, 13)},
            {"dataset_code": "two", "parsed": invalid, "model": object, "data_date": date(2026, 9, 13)},
        ])


def test_clinical_csv_parser_preserves_duplicate_inn_as_distinct_facts():
    content = (
        '"date";"name";"inn";"address";"phone";"email"\n'
        '"01.09.2026";"Org A";"7701234567";"A";"1";"a@test"\n'
        '"02.09.2026";"Org B";"7701234567";"B";"2";"b@test"\n'
    ).encode()
    parsed = parse_clinical_csv(content, data_date=date(2026, 9, 13))
    assert parsed["source_records"] == 2
    assert parsed["imported_records"] == 2
    assert {row["inn"] for row in parsed["records"]} == {"7701234567"}


def test_unified_parser_requires_exact_inn():
    payload = {"data": [unified_row()], "recordsFiltered": 1, "recordsTotal": 1}
    parsed = parse_unified_license_payload(payload, "7701234567")
    assert parsed["found"] is True
    assert parsed["records"][0]["license_number"] == "L-001"
    with pytest.raises(RoszdravProviderError, match="другого ИНН"):
        parse_unified_license_payload(payload, "7709999999")


def test_unified_provider_uses_one_low_load_official_query():
    client = FakeClient(FakeResponse({"data": [unified_row()], "recordsFiltered": 1, "recordsTotal": 1}))
    result = RoszdravUnifiedLicenseProvider(client=client).check_inn("7701234567")
    assert result["found"] is True
    assert len(client.calls) == 1
    assert client.calls[0][0] == LICENSE_SEARCH_URL
    assert client.calls[0][1]["data"]["q_org_inn"] == "7701234567"


def test_unified_provider_does_not_hide_protection_message():
    client = FakeClient(FakeResponse({"data": [], "message": "captcha"}))
    with pytest.raises(RoszdravProviderError) as error:
        RoszdravUnifiedLicenseProvider(client=client).check_inn("7701234567")
    assert error.value.kind == "source_protection"


def test_unified_provider_recognizes_official_empty_result_message():
    client = FakeClient(FakeResponse({"message": "Документов не найдено."}))
    result = RoszdravUnifiedLicenseProvider(client=client).check_inn("7701234567")
    assert result["found"] is False
    assert result["records"] == []
    assert result["total"] == 0


def test_medical_device_parser_matches_registration_number_exactly():
    record = {"id": 1, "noRu": "RU-TEST-1", "name": "Device"}
    parsed = parse_medical_device_payload({"content": [record], "totalElements": 1}, "RU-TEST-1")
    assert parsed == {"found": True, "records": [record], "total": 1}
    with pytest.raises(RoszdravProviderError):
        parse_medical_device_payload({"content": [record], "totalElements": 1}, "TEST")


def test_medical_device_provider_uses_exact_number_filter():
    client = FakeClient(FakeResponse({"content": [], "totalElements": 0}))
    result = RoszdravMedicalDeviceProvider(client=client).check_registration_number("RU-1")
    assert result["found"] is False
    assert client.calls[0][0] == MEDICAL_DEVICE_SEARCH_URL
    assert client.calls[0][1]["json"] == {"noRu": "RU-1"}


def test_medical_device_trust_anchor_is_pinned_repository_asset():
    pem = RUSSIAN_TRUSTED_ROOT_CA.read_text()
    assert pem.startswith("-----BEGIN CERTIFICATE-----")
    der = ssl.PEM_cert_to_DER_cert(pem)
    assert hashlib.sha256(der).hexdigest() == "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"


def test_medical_devices_are_not_linked_to_company_without_exact_identifier():
    result = get_roszdrav_medical_device_company_check()
    assert result["result"] == "not_applicable"
    assert result["applicable"] is False
    assert result["reason"] == "official_registry_has_no_company_exact_identifier"


def test_registry_declares_all_six_roszdrav_datasets():
    source = build_roszdrav_source_spec()
    specs = build_roszdrav_dataset_specs(10)
    assert source["code"] == "roszdravnadzor"
    assert len(specs) == 6
    assert {item["update_mode"] for item in specs} == {"bulk", "api"}


def test_product_aggregator_adds_source_only_for_definitive_check(monkeypatch):
    monkeypatch.setattr(company_product_aggregator, "get_roszdrav_bulk_license_check_for_inn", lambda inn: {"result": "found"})
    monkeypatch.setattr(company_product_aggregator, "get_cached_roszdrav_unified_license_check", lambda inn: {"result": "unavailable"})
    monkeypatch.setattr(company_product_aggregator, "get_roszdrav_clinical_org_check_for_inn", lambda inn: {"result": "not_found"})
    monkeypatch.setattr(company_product_aggregator, "get_roszdrav_medical_device_company_check", lambda: {"result": "not_applicable"})
    result = company_product_aggregator.enrich_company_with_roszdrav({"inn": "7701234567", "sources_used": []})
    assert result["sources_used"] == ["roszdravnadzor"]


def test_roszdrav_template_shows_all_four_parts():
    template = Environment(loader=FileSystemLoader("templates")).get_template("partials/roszdrav.html")
    html = template.render(company={
        "inn": "7701234567",
        "roszdrav_bulk_license_check": {"result": "not_found", "data_date": date(2026, 9, 13), "interpretation_note": "note", "coverage_note": "coverage"},
        "roszdrav_unified_license_check": {"result": "unavailable", "reason": "not_checked", "data_date": None},
        "roszdrav_clinical_org_check": {"result": "not_found"},
        "roszdrav_medical_device_check": {"interpretation_note": "exact limitation", "coverage_note": "no fuzzy"},
    })
    assert "Открытые реестры лицензий" in html
    assert "Единый реестр лицензий" in html
    assert "клинические исследования" in html
    assert "Государственный реестр медицинских изделий" in html
