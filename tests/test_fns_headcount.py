from datetime import date
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from app.aggregators import company_aggregator
from app.ingestion.fns_headcount import (
    extract_document,
    parse_fns_date,
)
from app.services import headcount_service


# =========================================================
# DATE PARSING
# =========================================================


def test_parse_fns_date_supported_formats():
    assert parse_fns_date(
        "31.12.2025"
    ) == date(
        2025,
        12,
        31,
    )

    assert parse_fns_date(
        "2025-12-31"
    ) == date(
        2025,
        12,
        31,
    )


def test_parse_fns_date_invalid_or_empty():
    assert parse_fns_date(
        None
    ) is None

    assert parse_fns_date(
        ""
    ) is None

    assert parse_fns_date(
        "not-a-date"
    ) is None


# =========================================================
# XML NORMALIZATION
# =========================================================


def test_extract_document_returns_normalized_headcount():
    document = ET.fromstring(
        """
        <Документ
            ИдДок="DOC-001"
            ДатаДок="31.12.2025"
        >
            <СведНП
                ИННЮЛ="7707083893"
                НаимОрг="Тестовая компания"
            />
            <СведССЧР
                КолРаб="42"
            />
        </Документ>
        """
    )

    result = extract_document(
        document
    )

    assert result == {
        "inn": "7707083893",
        "employee_count": 42,
        "document_id": "DOC-001",
        "document_date": date(
            2025,
            12,
            31,
        ),
    }


@pytest.mark.parametrize(
    (
        "inn",
        "employee_count",
    ),
    [
        (
            "",
            "42",
        ),
        (
            "123456789012",
            "42",
        ),
        (
            "7707083893",
            "not-number",
        ),
    ],
)
def test_extract_document_rejects_invalid_data(
    inn,
    employee_count,
):
    document = ET.fromstring(
        f"""
        <Документ
            ИдДок="DOC-BAD"
            ДатаДок="31.12.2025"
        >
            <СведНП
                ИННЮЛ="{inn}"
            />
            <СведССЧР
                КолРаб="{employee_count}"
            />
        </Документ>
        """
    )

    assert extract_document(
        document
    ) is None


# =========================================================
# HEADCOUNT SERVICE
# =========================================================


class FakeResult:
    def __init__(
        self,
        row,
    ):
        self.row = row

    def first(
        self,
    ):
        return self.row


class FakeSession:
    def __init__(
        self,
        row,
    ):
        self.row = row
        self.closed = False

    def execute(
        self,
        statement,
    ):
        return FakeResult(
            self.row
        )

    def close(
        self,
    ):
        self.closed = True


def test_headcount_service_returns_latest_value(
    monkeypatch,
):
    headcount = SimpleNamespace(
        employee_count=125,
        year=2025,
        source_document_id="DOC-125",
        source_document_date=date(
            2025,
            12,
            31,
        ),
    )

    dataset = SimpleNamespace(
        id=7,
        code="fns_headcount",
        priority=10,
    )

    session = FakeSession(
        (
            headcount,
            dataset,
        )
    )

    monkeypatch.setattr(
        headcount_service,
        "get_session",
        lambda: session,
    )

    result = (
        headcount_service
        .get_latest_headcount_for_company(
            company_id=123,
        )
    )

    assert result == {
        "employee_count": 125,
        "year": 2025,
        "dataset_id": 7,
        "dataset_code": (
            "fns_headcount"
        ),
        "priority": 10,
        "source_document_id": (
            "DOC-125"
        ),
        "source_document_date": date(
            2025,
            12,
            31,
        ),
    }

    assert session.closed is True


def test_headcount_service_returns_none_when_missing(
    monkeypatch,
):
    session = FakeSession(
        None
    )

    monkeypatch.setattr(
        headcount_service,
        "get_session",
        lambda: session,
    )

    result = (
        headcount_service
        .get_latest_headcount_for_company(
            company_id=999,
        )
    )

    assert result is None
    assert session.closed is True


# =========================================================
# AGGREGATOR
# =========================================================


def test_load_domain_candidates_adds_headcount(
    monkeypatch,
):
    monkeypatch.setattr(
        company_aggregator,
        "get_latest_headcount_for_company",
        lambda company_id: {
            "employee_count": 77,
            "year": 2025,
            "dataset_id": 5,
            "dataset_code": (
                "fns_headcount"
            ),
            "priority": 10,
            "source_document_id": (
                "DOC-77"
            ),
            "source_document_date": (
                date(
                    2025,
                    12,
                    31,
                )
            ),
        },
    )

    candidates = (
        company_aggregator
        .load_domain_candidates(
            company_id=100,
        )
    )

    assert candidates == [
        {
            "source": (
                "fns_headcount"
            ),
            "priority": 10,
            "payload": {
                "employee_count": 77,
                "employee_count_year": (
                    2025
                ),
            },
            "cached": True,
        }
    ]


def test_load_domain_candidates_skips_missing_headcount(
    monkeypatch,
):
    monkeypatch.setattr(
        company_aggregator,
        "get_latest_headcount_for_company",
        lambda company_id: None,
    )

    candidates = (
        company_aggregator
        .load_domain_candidates(
            company_id=100,
        )
    )

    assert candidates == []


def test_merge_candidates_keeps_zero_employee_count():
    result = (
        company_aggregator
        .merge_candidates(
            [
                {
                    "source": (
                        "fns_headcount"
                    ),
                    "priority": 10,
                    "payload": {
                        "employee_count": 0,
                        "employee_count_year": (
                            2025
                        ),
                    },
                }
            ]
        )
    )

    assert (
        result[
            "employee_count"
        ]
        == 0
    )

    assert (
        result[
            "employee_count_year"
        ]
        == 2025
    )

    assert (
        result[
            "field_sources"
        ][
            "employee_count"
        ]
        == "fns_headcount"
    )

    assert (
        "fns_headcount"
        in result[
            "sources_used"
        ]
    )
