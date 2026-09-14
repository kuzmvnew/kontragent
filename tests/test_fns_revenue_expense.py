from datetime import date
from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree as ET

from app.ingestion.fns_revenue_expense import (
    iter_xml_records,
    parse_date,
    parse_decimal,
    parse_revenue_expense_document,
)


VALID_XML = """
<Документ
    ИдДок="doc-1"
    ДатаДок="25.08.2026"
    ДатаСост="31.12.2025"
>
    <СведНП
        ИННЮЛ="4205406898"
        НаимОрг="ООО ТЕСТ"
    />
    <СведДохРасх
        СумДоход="341864000.00"
        СумРасход="282224000.00"
    />
</Документ>
"""


def test_parse_date_accepts_fns_format():
    assert (
        parse_date(
            "31.12.2025"
        )
        == date(
            2025,
            12,
            31,
        )
    )


def test_parse_decimal_accepts_dot_and_comma():
    assert (
        parse_decimal(
            "100.50"
        )
        == Decimal(
            "100.50"
        )
    )

    assert (
        parse_decimal(
            "100,50"
        )
        == Decimal(
            "100.50"
        )
    )


def test_parse_revenue_expense_document():
    document = ET.fromstring(
        VALID_XML
    )

    result = (
        parse_revenue_expense_document(
            document
        )
    )

    assert result == {
        "inn": "4205406898",
        "company_name": "ООО ТЕСТ",
        "document_id": "doc-1",
        "document_date": date(
            2026,
            8,
            25,
        ),
        "data_date": date(
            2025,
            12,
            31,
        ),
        "data_year": 2025,
        "revenue": Decimal(
            "341864000.00"
        ),
        "expenses": Decimal(
            "282224000.00"
        ),
        "profit_loss": Decimal(
            "59640000.00"
        ),
    }


def test_parser_accepts_zero_values():
    xml = (
        VALID_XML
        .replace(
            "341864000.00",
            "0",
        )
        .replace(
            "282224000.00",
            "0",
        )
    )

    result = (
        parse_revenue_expense_document(
            ET.fromstring(
                xml
            )
        )
    )

    assert (
        result["revenue"]
        == Decimal("0.00")
    )

    assert (
        result["expenses"]
        == Decimal("0.00")
    )

    assert (
        result["profit_loss"]
        == Decimal("0.00")
    )


def test_parser_rejects_invalid_inn():
    xml = VALID_XML.replace(
        "4205406898",
        "123",
    )

    result = (
        parse_revenue_expense_document(
            ET.fromstring(
                xml
            )
        )
    )

    assert result is None


def test_parser_rejects_missing_financial_element():
    xml = VALID_XML.replace(
        (
            '<СведДохРасх\n'
            '        СумДоход="341864000.00"\n'
            '        СумРасход="282224000.00"\n'
            "    />"
        ),
        "",
    )

    result = (
        parse_revenue_expense_document(
            ET.fromstring(
                xml
            )
        )
    )

    assert result is None


def test_iter_xml_records_returns_valid_and_invalid_rows():
    xml = (
        f"<Файл>"
        f"{VALID_XML}"
        f"<Документ ИдДок='bad' />"
        f"</Файл>"
    ).encode()

    records = list(
        iter_xml_records(
            BytesIO(
                xml
            )
        )
    )

    assert len(
        records
    ) == 2

    assert (
        records[0]["inn"]
        == "4205406898"
    )

    assert records[1] is None