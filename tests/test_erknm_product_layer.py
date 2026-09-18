from datetime import date
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from app.aggregators import company_product_aggregator
from app.services.erknm_service import (
    _clean_digits,
    _serialize_record,
)
from app.services.company_check_orchestrator import _normalize_legacy_company
from app.contracts.source_architecture import NormalizedResultStatus
from datetime import datetime, timezone


def test_erknm_identifier_normalization():
    assert _clean_digits("77 013 642 31", {10, 12}) == "7701364231"
    assert _clean_digits("1137746586251", {13, 15}) == "1137746586251"
    assert _clean_digits("123", {10, 12}) is None


def test_erknm_record_serialization():
    row = SimpleNamespace(
        erpid="77260061000120428959",
        data_date=date(2026, 9, 15),
        period_year=2026,
        period_month=1,
        classification="ПМ",
        status="Предостережение объявлено",
        status_key="REMARK",
        supervision_name="Федеральный государственный пожарный надзор",
        kind_control="Федеральный государственный пожарный надзор",
        kind_knm="Объявление предостережения",
        kno_organization="ГУ МЧС",
        prosecutor_office="Прокуратура города Москвы",
        start_date=date(2026, 1, 4),
        end_date=None,
        subject_inn="7701364231",
        subject_ogrn="1137746586251",
        subject_name="ООО ТЕСТ",
        subject_type="ЮЛ",
        place=None,
        object_address="Москва",
        object_type="Производственные объекты",
        object_kind="Здание",
        risk_category="значительный риск",
        reason_text="ФЗ №248-ФЗ",
        warning_caption="Предостережение",
        result_text=None,
    )

    result = _serialize_record(row)

    assert result["erpid"] == "77260061000120428959"
    assert result["subject_inn"] == "7701364231"
    assert result["kind_knm"] == "Объявление предостережения"
    assert result["has_warning"] is True
    assert result["has_result"] is False


def test_product_aggregator_adds_erknm_check(monkeypatch):
    base_company = {
        "id": 10,
        "inn": "7701364231",
        "ogrn": "1137746586251",
        "name": "ООО ТЕСТ",
        "sources_used": ["excel_import"],
    }

    erknm_check = {
        "checked": True,
        "applicable": True,
        "result": "found",
        "data_date": date(2026, 9, 15),
        "dataset_code": "erknm_inspections",
        "source": "erknm_inspections",
        "record_count": 2,
        "records": [],
    }

    monkeypatch.setattr(
        company_product_aggregator,
        "get_base_company_for_web",
        lambda inn: base_company,
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_disqualified_check_for_inn",
        lambda inn: {"result": "unavailable"},
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_cached_npd_check_for_inn",
        lambda inn: {"result": "not_applicable"},
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_erknm_check_for_company",
        lambda inn, ogrn: erknm_check,
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_cbr_warning_list_check_for_inn",
        lambda inn: {"result": "unavailable"},
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_cached_cbr_finorg_check_for_inn",
        lambda inn: {"result": "unavailable"},
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "enrich_company_with_roszdrav",
        lambda company: company,
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "enrich_company_with_roskomnadzor",
        lambda company: company,
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "enrich_company_with_sro",
        lambda company: company,
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_cached_corporate_disclosure_check",
        lambda inn: {"result": "unavailable"},
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "enrich_company_with_stage15_on_demand_checks",
        lambda company: company,
    )

    result = company_product_aggregator.get_company_for_web(
        "7701364231"
    )

    assert result["erknm_check"] == erknm_check
    assert "erknm_inspections" in result["sources_used"]
    assert base_company["sources_used"] == ["excel_import"]


def test_erknm_unavailable_not_added_to_sources(monkeypatch):
    company = {
        "inn": "7701364231",
        "ogrn": "1137746586251",
        "sources_used": [],
    }

    monkeypatch.setattr(
        company_product_aggregator,
        "get_erknm_check_for_company",
        lambda inn, ogrn: {"result": "unavailable"},
    )

    result = company_product_aggregator.enrich_company_with_erknm(
        company
    )

    assert "erknm_inspections" not in result["sources_used"]


def test_erknm_existing_product_check_enters_v3_normalization():
    company = {
        "inn": "7701364231",
        "status": "ACTIVE",
        "erknm_check": {
            "result": "not_found",
            "source": "erknm_inspections",
            "dataset_code": "erknm_inspections",
            "data_date": date(2026, 9, 15),
            "record_count": 0,
            "records": [],
        },
    }
    normalized = _normalize_legacy_company(
        company, datetime(2026, 9, 18, tzinfo=timezone.utc),
    )
    erknm = next(item for item in normalized if item.check_code == "regulatory_inspections")
    assert erknm.result == NormalizedResultStatus.NOT_FOUND
    assert erknm.coverage == 1


def test_non_applicable_sector_sources_do_not_call_storage(monkeypatch):
    ordinary = {"inn": "7700000000", "okved": "62.01", "sources_used": []}
    monkeypatch.setattr(
        company_product_aggregator,
        "get_cached_cbr_finorg_check_for_inn",
        lambda _inn: (_ for _ in ()).throw(AssertionError("CBR storage called")),
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_roszdrav_bulk_license_check_for_inn",
        lambda _inn: (_ for _ in ()).throw(AssertionError("Roszdrav storage called")),
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_cached_nostroy_check",
        lambda _inn, *, applicable: {"result": "not_applicable"} if not applicable else (_ for _ in ()).throw(AssertionError("Nostroy storage called")),
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_cached_nopriz_check",
        lambda _inn, *, applicable: {"result": "not_applicable"} if not applicable else (_ for _ in ()).throw(AssertionError("Nopriz storage called")),
    )
    assert company_product_aggregator.enrich_company_with_cbr_finorg(ordinary)["cbr_finorg_check"]["result"] == "not_applicable"
    assert company_product_aggregator.enrich_company_with_roszdrav(ordinary)["roszdrav_bulk_license_check"]["result"] == "not_applicable"
    assert all(check["result"] == "not_applicable" for check in company_product_aggregator.enrich_company_with_sro(ordinary)["sro_checks"].values())


def test_company_template_with_erknm_partial_compiles():
    env = Environment(
        loader=FileSystemLoader("templates"),
    )

    template = env.get_template("company.html")

    assert template is not None
