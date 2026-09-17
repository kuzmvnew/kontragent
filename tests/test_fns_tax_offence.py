from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest

from app.ingestion.fns_tax_offence import (
    SOURCE_FILE_BASE_URL,
    SOURCE_PAGE_URL,
    parse_tax_offence_document,
)


def document_xml(*offences: str) -> ET.Element:
    return ET.fromstring(
        """
        <Документ ИдДок="OFFENCE-1" ДатаДок="02.12.2025" ДатаСост="31.12.2024">
          <СведНП ИННЮЛ="1215214540" НаимОрг="ООО ТЕСТ" />
          {offences}
        </Документ>
        """.format(offences="".join(offences))
    )


def offence(amount: str) -> str:
    return f'<СведНаруш СумШтраф="{amount}" />'


def test_tax_offence_parser_preserves_amount_and_dates():
    result = parse_tax_offence_document(document_xml(offence("125.50")))

    assert result is not None
    assert result["inn"] == "1215214540"
    assert result["document_id"] == "OFFENCE-1"
    assert result["data_date"].isoformat() == "2024-12-31"
    assert result["document_date"].isoformat() == "2025-12-02"
    assert result["fine_amount"] == Decimal("125.50")


def test_tax_offence_parser_sums_multiple_offence_elements():
    result = parse_tax_offence_document(
        document_xml(offence("100.00"), offence("25.50"))
    )

    assert result is not None
    assert result["fine_amount"] == Decimal("125.50")


def test_tax_offence_parser_rejects_invalid_money_instead_of_zeroing_it():
    with pytest.raises(ValueError, match="Некорректное денежное значение"):
        parse_tax_offence_document(document_xml(offence("not-a-number")))


def test_tax_offence_parser_rejects_document_without_offence_details():
    assert parse_tax_offence_document(document_xml()) is None


def test_tax_offence_uses_official_fns_open_data_urls():
    assert SOURCE_PAGE_URL == (
        "https://www.nalog.gov.ru/opendata/7707329152-taxoffence/"
    )
    assert SOURCE_FILE_BASE_URL == (
        "https://data.nalog.ru/opendata/7707329152-taxoffence/"
    )
