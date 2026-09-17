from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest

from app.ingestion.fns_tax_debt import (
    SOURCE_FILE_BASE_URL,
    SOURCE_PAGE_URL,
    parse_tax_debt_document,
)


def document_xml(*items: str) -> ET.Element:
    return ET.fromstring(
        """
        <Документ ИдДок="DEBT-1" ДатаДок="25.08.2026" ДатаСост="01.08.2026">
          <СведНП ИННЮЛ="7722858778" НаимОрг="ООО ТЕСТ" />
          {items}
        </Документ>
        """.format(items="".join(items))
    )


def debt_item(
    *,
    name: str = "Налог на прибыль",
    arrears: str = "100.00",
    penalties: str = "20.00",
    fines: str = "5.00",
    total: str = "125.00",
) -> str:
    return (
        f'<СведНедоим НаимНалог="{name}" '
        f'СумНедНалог="{arrears}" СумПени="{penalties}" '
        f'СумШтраф="{fines}" ОбщСумНедоим="{total}" />'
    )


def test_tax_debt_parser_preserves_components_and_dates():
    result = parse_tax_debt_document(document_xml(debt_item()))

    assert result is not None
    assert result["inn"] == "7722858778"
    assert result["document_id"] == "DEBT-1"
    assert result["data_date"].isoformat() == "2026-08-01"
    assert result["document_date"].isoformat() == "2026-08-25"
    assert result["total_arrears"] == Decimal("100.00")
    assert result["total_penalties"] == Decimal("20.00")
    assert result["total_fines"] == Decimal("5.00")
    assert result["total_debt"] == Decimal("125.00")
    assert result["item_count"] == 1


def test_tax_debt_parser_aggregates_repeated_tax_names():
    result = parse_tax_debt_document(
        document_xml(
            debt_item(),
            debt_item(
                arrears="10.00",
                penalties="2.00",
                fines="1.00",
                total="13.00",
            ),
        )
    )

    assert result is not None
    assert result["item_count"] == 1
    assert result["total_debt"] == Decimal("138.00")
    assert result["items"][0] == {
        "tax_name": "Налог на прибыль",
        "arrears": Decimal("110.00"),
        "penalties": Decimal("22.00"),
        "fines": Decimal("6.00"),
        "total": Decimal("138.00"),
    }


def test_tax_debt_parser_rejects_invalid_money_instead_of_zeroing_it():
    with pytest.raises(ValueError, match="Некорректное денежное значение"):
        parse_tax_debt_document(
            document_xml(debt_item(arrears="not-a-number"))
        )


def test_tax_debt_parser_rejects_inconsistent_item_total():
    with pytest.raises(ValueError, match="ОбщСумНедоим не равна"):
        parse_tax_debt_document(
            document_xml(debt_item(total="124.99"))
        )


def test_tax_debt_uses_official_fns_open_data_urls():
    assert SOURCE_PAGE_URL == (
        "https://www.nalog.gov.ru/opendata/7707329152-debtam/"
    )
    assert SOURCE_FILE_BASE_URL == (
        "https://file.nalog.ru/opendata/7707329152-debtam/"
    )
