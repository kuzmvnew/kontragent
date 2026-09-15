from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from app.ingestion.fns_tax_regime import (
    IP_CODE_TO_REGIME,
    IP_DATASET_CODE,
    LEGAL_DATASET_CODE,
    parse_fns_date,
    parse_ip_document,
    parse_legal_document,
)
from app.models.tax_regime import (
    CompanyTaxRegimeSnapshot,
)


def test_parse_fns_date():
    assert parse_fns_date(
        "01.08.2026"
    ) == date(
        2026,
        8,
        1,
    )

    assert parse_fns_date(
        "2026-08-01"
    ) == date(
        2026,
        8,
        1,
    )

    assert parse_fns_date(
        "wrong"
    ) is None


def test_parse_legal_document():
    document = ET.fromstring(
        """
        <Документ
            ИдДок="LEGAL-1"
            ДатаДок="25.08.2026"
            ДатаСост="01.08.2026"
        >
            <СведНП
                ИННЮЛ="7714914147"
            />
            <СведСНР
                ПризнЕСХН="0"
                ПризнУСН="1"
                ПризнАУСН="0"
                ПризнСРП="1"
            />
        </Документ>
        """
    )

    result = parse_legal_document(
        document
    )

    assert result is not None

    assert result["inn"] == "7714914147"

    assert result[
        "dataset_code"
    ] == LEGAL_DATASET_CODE

    assert result[
        "regime_codes"
    ] == [
        "usn",
        "srp",
    ]

    assert result[
        "data_date"
    ] == date(
        2026,
        8,
        1,
    )


def test_parse_legal_invalid_inn():
    document = ET.fromstring(
        """
        <Документ
            ДатаСост="01.08.2026"
        >
            <СведНП
                ИННЮЛ="123"
            />
            <СведСНР
                ПризнУСН="1"
            />
        </Документ>
        """
    )

    assert (
        parse_legal_document(
            document
        )
        is None
    )


def test_ip_code_mapping():
    assert IP_CODE_TO_REGIME == {
        "1": "usn",
        "2": "ausn",
        "3": "eshn",
        "4": "psn",
        "5": "npd",
    }


def test_parse_ip_multiple_regimes():
    document = ET.fromstring(
        """
        <Документ
            ИдДок="IP-1"
            ДатаДок="25.08.2026"
            ДатаСост="01.08.2026"
        >
            <СведНП
                ИННФЛ="345907922962"
                ОГРНИП="324940100014240"
            />

            <СведСНР
                ПризнСНР="1"
            />

            <СведСНР
                ПризнСНР="4"
            />

            <СведСНР
                ПризнСНР="5"
            />
        </Документ>
        """
    )

    result = parse_ip_document(
        document
    )

    assert result is not None

    assert result[
        "dataset_code"
    ] == IP_DATASET_CODE

    assert result[
        "entity_type"
    ] == (
        "individual_entrepreneur"
    )

    assert result[
        "regime_codes"
    ] == [
        "usn",
        "psn",
        "npd",
    ]


def test_ip_unknown_code_is_preserved():
    document = ET.fromstring(
        """
        <Документ
            ДатаСост="01.08.2026"
        >
            <СведНП
                ИННФЛ="345907922962"
            />

            <СведСНР
                ПризнСНР="1"
            />

            <СведСНР
                ПризнСНР="9"
            />
        </Документ>
        """
    )

    result = parse_ip_document(
        document
    )

    assert result[
        "regime_codes"
    ] == [
        "usn",
    ]

    assert result[
        "unknown_codes"
    ] == [
        "9",
    ]


def test_tax_regime_model():
    assert (
        CompanyTaxRegimeSnapshot
        .__tablename__
        == (
            "company_tax_regime_snapshots"
        )
    )


def test_dataset_registry():
    text = Path(
        "app/services/source_service.py"
    ).read_text(
        encoding="utf-8"
    )

    assert (
        '"code": "fns_snr"'
        in text
    )

    assert (
        '"code": "fns_snrip"'
        in text
    )

    assert (
        '"domain": "tax_regime"'
        in text
    )



def test_legal_zip_iterator_reads_records(tmp_path):
    from zipfile import ZipFile

    from app.ingestion.fns_tax_regime import (
        iter_legal_records_from_zip,
    )

    zip_path = tmp_path / "snr.zip"

    xml = """
    <Файл>
        <Документ
            ИдДок="DOC-1"
            ДатаДок="25.08.2026"
            ДатаСост="01.08.2026"
        >
            <СведНП ИННЮЛ="7714914147" />
            <СведСНР
                ПризнУСН="1"
                ПризнАУСН="0"
                ПризнЕСХН="0"
                ПризнСРП="0"
            />
        </Документ>

        <Документ
            ИдДок="DOC-2"
            ДатаДок="25.08.2026"
            ДатаСост="01.08.2026"
        >
            <СведНП ИННЮЛ="3906293351" />
            <СведСНР
                ПризнУСН="0"
                ПризнАУСН="1"
                ПризнЕСХН="0"
                ПризнСРП="0"
            />
        </Документ>
    </Файл>
    """

    with ZipFile(
        zip_path,
        "w",
    ) as archive:
        archive.writestr(
            "data.xml",
            xml,
        )

    records = list(
        iter_legal_records_from_zip(
            zip_path
        )
    )

    assert len(records) == 2

    assert records[0][
        "regime_codes"
    ] == ["usn"]

    assert records[1][
        "regime_codes"
    ] == ["ausn"]


def test_legal_zip_iterator_limit(tmp_path):
    from zipfile import ZipFile

    from app.ingestion.fns_tax_regime import (
        iter_legal_records_from_zip,
    )

    zip_path = tmp_path / "snr.zip"

    xml = """
    <Файл>
        <Документ
            ДатаСост="01.08.2026"
        >
            <СведНП ИННЮЛ="7714914147" />
            <СведСНР
                ПризнУСН="1"
                ПризнАУСН="0"
                ПризнЕСХН="0"
                ПризнСРП="0"
            />
        </Документ>

        <Документ
            ДатаСост="01.08.2026"
        >
            <СведНП ИННЮЛ="3906293351" />
            <СведСНР
                ПризнУСН="1"
                ПризнАУСН="0"
                ПризнЕСХН="0"
                ПризнСРП="0"
            />
        </Документ>
    </Файл>
    """

    with ZipFile(
        zip_path,
        "w",
    ) as archive:
        archive.writestr(
            "data.xml",
            xml,
        )

    records = list(
        iter_legal_records_from_zip(
            zip_path,
            limit=1,
        )
    )

    assert len(records) == 1

    assert (
        records[0]["inn"]
        == "7714914147"
    )
