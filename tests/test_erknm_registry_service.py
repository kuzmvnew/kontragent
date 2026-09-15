from app.services.erknm_registry_service import (
    DATASET_CODE,
    SOURCE_CODE,
    build_erknm_dataset_spec,
    build_erknm_source_spec,
)


def test_erknm_source_spec_is_official_and_enabled():
    spec = build_erknm_source_spec()

    assert SOURCE_CODE == "genproc"
    assert spec["code"] == "genproc"
    assert spec["source_type"] == "official"
    assert spec["enabled"] is True
    assert spec["website_url"] == "https://proverki.gov.ru/"


def test_erknm_dataset_spec_is_bulk_248_fz_xml():
    spec = build_erknm_dataset_spec(source_id=123)

    assert DATASET_CODE == "erknm_inspections"
    assert spec["source_id"] == 123
    assert spec["code"] == "erknm_inspections"
    assert spec["domain"] == "inspections"
    assert spec["update_mode"] == "bulk"
    assert spec["data_format"] == "xml"
    assert spec["refresh_schedule"] == "daily"
    assert spec["enabled"] is True
    assert "248-ФЗ" in spec["description"]
    assert "erknm-opendata" in spec["description"]
