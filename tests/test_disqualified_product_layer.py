from datetime import date
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from app.aggregators import company_product_aggregator
from app.services.disqualified_service import (
    _is_active_on_date,
    _serialize_record,
)


def test_disqualification_period_activity():
    snapshot_date = date(2026, 9, 13)

    assert _is_active_on_date(
        date(2024, 1, 1),
        date(2027, 1, 1),
        snapshot_date,
    ) is True

    assert _is_active_on_date(
        date(2024, 1, 1),
        date(2026, 9, 12),
        snapshot_date,
    ) is False

    assert _is_active_on_date(
        None,
        date(2027, 1, 1),
        snapshot_date,
    ) is True


def test_serialize_record_marks_activity():
    row = SimpleNamespace(
        register_number="123",
        full_name="ИВАНОВ ИВАН ИВАНОВИЧ",
        birth_date=date(1980, 1, 1),
        birth_place="МОСКВА",
        organization_name="ООО ТЕСТ",
        organization_inn="7700000000",
        position="ДИРЕКТОР",
        offence_article="СТАТЬЯ",
        protocol_authority="ОРГАН",
        judge_name="СУДЬЯ",
        judge_position="МИРОВОЙ СУДЬЯ",
        disqualification_term="1 г",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )

    result = _serialize_record(
        row=row,
        data_date=date(2026, 9, 13),
    )

    assert result["organization_inn"] == "7700000000"
    assert result["active_on_data_date"] is True


def _patch_later_product_sources(monkeypatch):
    monkeypatch.setattr(
        company_product_aggregator,
        "get_cached_corporate_disclosure_check",
        lambda inn: {
            "result": "unavailable",
        },
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_erknm_check_for_company",
        lambda inn, ogrn: {
            "result": "unavailable",
        },
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_cbr_warning_list_check_for_inn",
        lambda inn: {
            "result": "unavailable",
        },
    )
    monkeypatch.setattr(
        company_product_aggregator,
        "get_cached_cbr_finorg_check_for_inn",
        lambda inn: {
            "result": "unavailable",
        },
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


def test_product_aggregator_adds_disqualified_check(
    monkeypatch,
):
    base_company = {
        "id": 10,
        "inn": "7700000000",
        "name": "ООО ТЕСТ",
        "sources_used": ["excel_import"],
    }

    check = {
        "checked": True,
        "applicable": True,
        "result": "found",
        "data_date": date(2026, 9, 13),
        "dataset_code": "fns_disqualified",
        "source": "fns_disqualified",
        "reason": None,
        "has_records": True,
        "record_count": 1,
        "active_record_count": 1,
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
        lambda inn: check,
    )

    _patch_later_product_sources(monkeypatch)

    result = (
        company_product_aggregator.get_company_for_web(
            "7700000000"
        )
    )

    assert result["disqualified_check"] == check
    assert "fns_disqualified" in result["sources_used"]
    assert base_company["sources_used"] == ["excel_import"]


def test_unavailable_check_is_not_marked_as_used_source(
    monkeypatch,
):
    base_company = {
        "id": 10,
        "inn": "7700000000",
        "name": "ООО ТЕСТ",
        "sources_used": [],
    }

    monkeypatch.setattr(
        company_product_aggregator,
        "get_base_company_for_web",
        lambda inn: base_company,
    )

    monkeypatch.setattr(
        company_product_aggregator,
        "get_disqualified_check_for_inn",
        lambda inn: {
            "result": "unavailable",
        },
    )

    _patch_later_product_sources(monkeypatch)

    result = (
        company_product_aggregator.get_company_for_web(
            "7700000000"
        )
    )

    assert "fns_disqualified" not in result["sources_used"]


def test_company_template_with_disqualified_partial_compiles():
    env = Environment(
        loader=FileSystemLoader("templates"),
    )

    template = env.get_template("company.html")

    assert template is not None
