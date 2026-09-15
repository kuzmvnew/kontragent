import pytest

from scripts.sync_erknm_open_data import (
    ERKNM_BASE_URL,
    build_metadata_url,
    parse_erknm_metadata,
    parse_months,
)


def test_build_metadata_url():
    assert build_metadata_url(2026, 1) == (
        "https://proverki.gov.ru/blob/erknm-opendata/"
        "7710146102-inspection-2026-1.xml"
    )


def test_parse_months():
    assert parse_months("1-3,7,9-10") == [1, 2, 3, 7, 9, 10]


def test_parse_months_rejects_invalid():
    with pytest.raises(ValueError):
        parse_months("0,1")

    with pytest.raises(ValueError):
        parse_months("7-3")


def test_parse_erknm_metadata_selects_latest_official_version():
    xml = b"""<?xml version='1.0' encoding='UTF-8'?>
    <meta>
      <identifier>7710146102-inspection-2026-1</identifier>
      <title>\xd0\x9f\xd1\x80\xd0\xbe\xd0\xb2\xd0\xb5\xd1\x80\xd0\xba\xd0\xb8</title>
      <subject>\xd0\x9f\xd1\x80\xd0\xbe\xd0\xb2\xd0\xb5\xd1\x80\xd0\xba\xd0\xb8 \xd0\xbd\xd0\xb0 2026 \xd0\xb3\xd0\xbe\xd0\xb4 \xd0\xbf\xd0\xbe 248 \xd0\xa4\xd0\x97</subject>
      <erknmStructure>
        <structureversion>https://proverki.gov.ru/blob/erknm-opendata/structure-20220125.xsd</structureversion>
      </erknmStructure>
      <data>
        <dataversion>
          <source>https://proverki.gov.ru/blob/erknm-opendata/2026/1/data-20260914-structure-20220125.zip</source>
          <created>20260914</created>
          <structure>20220125</structure>
        </dataversion>
        <dataversion>
          <source>https://proverki.gov.ru/blob/erknm-opendata/2026/1/data-20260915-structure-20220125.zip</source>
          <created>20260915</created>
          <structure>20220125</structure>
        </dataversion>
      </data>
    </meta>"""

    result = parse_erknm_metadata(xml)

    assert result["identifier"] == "7710146102-inspection-2026-1"
    assert result["version_count"] == 2
    assert result["latest"]["created"] == "20260915"
    assert result["latest"]["source_url"].endswith(
        "data-20260915-structure-20220125.zip"
    )
    assert result["structure_url"].startswith(ERKNM_BASE_URL)


def test_parse_erknm_metadata_rejects_old_erp_294():
    xml = b"""<?xml version='1.0' encoding='UTF-8'?>
    <meta>
      <identifier>7710146102-inspection-2026-1</identifier>
      <subject>\xd0\x9f\xd1\x80\xd0\xbe\xd0\xb2\xd0\xb5\xd1\x80\xd0\xba\xd0\xb8 \xd0\xbd\xd0\xb0 2026 \xd0\xb3\xd0\xbe\xd0\xb4 \xd0\xbf\xd0\xbe 294 \xd0\xa4\xd0\x97</subject>
      <structure>
        <structureversion>https://proverki.gov.ru/blob/opendata/structure-20210222.xsd</structureversion>
      </structure>
      <data>
        <dataversion>
          <source>https://proverki.gov.ru/blob/opendata/2026/1/data.zip</source>
          <created>20260730</created>
        </dataversion>
      </data>
    </meta>"""

    with pytest.raises(ValueError, match="248"):
        parse_erknm_metadata(xml)
