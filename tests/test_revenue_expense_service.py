from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import (
    revenue_expense_service,
)


DATA_DATE = date(
    2025,
    12,
    31,
)


class FakeResult:

    def __init__(
        self,
        *,
        mapping=None,
        scalar=None,
    ):
        self.mapping = mapping
        self.scalar = scalar

    def mappings(
        self,
    ):
        return self

    def one_or_none(
        self,
    ):
        return self.mapping

    def scalar_one_or_none(
        self,
    ):
        return self.scalar


class FakeSession:

    def __init__(
        self,
        *results,
    ):
        self.results = list(
            results
        )

        self.closed = False

    def execute(
        self,
        _statement,
    ):
        return self.results.pop(
            0
        )

    def close(
        self,
    ):
        self.closed = True


def install_session(
    monkeypatch,
    *results,
):
    session = FakeSession(
        *results
    )

    monkeypatch.setattr(
        revenue_expense_service,
        "get_session",
        lambda: session,
    )

    return session


def company(
    inn="4205406898",
):
    return FakeResult(
        mapping={
            "id": 101,
            "inn": inn,
            "entity_type": (
                "legal"
                if len(inn) == 10
                else (
                    "individual_entrepreneur"
                )
            ),
        }
    )


def dataset(
    *,
    loaded=True,
    operational_status="current",
    actual_until=date(2099, 12, 31),
):
    return FakeResult(
        scalar=SimpleNamespace(
            id=202,
            last_data_date=(
                DATA_DATE
                if loaded
                else None
            ),
            operational_status=operational_status,
            official_actual_until=actual_until,
        )
    )


def snapshot(
    *,
    revenue="1000.00",
    expenses="700.00",
):
    revenue_value = Decimal(
        revenue
    )

    expenses_value = Decimal(
        expenses
    )

    return FakeResult(
        scalar=SimpleNamespace(
            id=303,
            data_date=(
                DATA_DATE
            ),
            data_year=2025,
            document_date=date(
                2026,
                8,
                25,
            ),
            source_document_id=(
                "doc-1"
            ),
            source_company_name=(
                "ООО ТЕСТ"
            ),
            revenue=(
                revenue_value
            ),
            expenses=(
                expenses_value
            ),
            profit_loss=(
                revenue_value
                - expenses_value
            ),
        )
    )


def test_found_standard_values_can_be_used_for_assessment(
    monkeypatch,
):
    session = install_session(
        monkeypatch,
        company(),
        dataset(),
        snapshot(),
    )

    result = (
        revenue_expense_service
        .get_revenue_expense_check_for_company(
            101
        )
    )

    assert (
        result["result"]
        == "found"
    )

    assert (
        result["revenue"]
        == Decimal("1000.00")
    )

    assert (
        result["expenses"]
        == Decimal("700.00")
    )

    assert (
        result[
            "calculated_difference"
        ]
        == Decimal("300.00")
    )

    assert (
        result[
            "has_unusual_values"
        ]
        is False
    )

    assert (
        result[
            "can_use_for_assessment"
        ]
        is True
    )

    assert session.closed is True


def test_negative_expenses_are_preserved_and_flagged(
    monkeypatch,
):
    install_session(
        monkeypatch,
        company(),
        dataset(),
        snapshot(
            revenue="1000.00",
            expenses="-700.00",
        ),
    )

    result = (
        revenue_expense_service
        .get_revenue_expense_check_for_company(
            101
        )
    )

    assert (
        result["expenses"]
        == Decimal("-700.00")
    )

    assert (
        result[
            "calculated_difference"
        ]
        == Decimal("1700.00")
    )

    assert (
        result[
            "quality_flags"
        ]
        == [
            "negative_expenses"
        ]
    )

    assert (
        result[
            "can_use_for_assessment"
        ]
        is False
    )


def test_individual_entrepreneur_is_not_applicable(
    monkeypatch,
):
    install_session(
        monkeypatch,
        company(
            "123456789012"
        ),
    )

    result = (
        revenue_expense_service
        .get_revenue_expense_check_for_company(
            101
        )
    )

    assert (
        result["result"]
        == "not_applicable"
    )

    assert (
        result["checked"]
        is True
    )

    assert (
        result["applicable"]
        is False
    )


def test_not_found_means_absent_from_loaded_dataset(
    monkeypatch,
):
    install_session(
        monkeypatch,
        company(),
        dataset(),
        FakeResult(
            scalar=None
        ),
    )

    result = (
        revenue_expense_service
        .get_revenue_expense_check_for_company(
            101
        )
    )

    assert (
        result["result"]
        == "not_found"
    )

    assert (
        result["data_date"]
        == DATA_DATE
    )

    assert (
        result["data_year"]
        == 2025
    )

    assert (
        result[
            "can_use_for_assessment"
        ]
        is False
    )


@pytest.mark.parametrize(
    ("dataset_kwargs", "reason"),
    (
        ({"actual_until": date(2000, 1, 1)}, "dataset_stale"),
        ({"operational_status": "unavailable"}, "dataset_unavailable"),
        ({"actual_until": None}, "dataset_freshness_unavailable"),
    ),
)
def test_stale_or_unavailable_dataset_never_proves_clean_negative(
    monkeypatch,
    dataset_kwargs,
    reason,
):
    install_session(
        monkeypatch,
        company(),
        dataset(**dataset_kwargs),
        FakeResult(scalar=None),
    )

    result = revenue_expense_service.get_revenue_expense_check_for_company(101)

    assert result["result"] == "unavailable"
    assert result["checked"] is False
    assert result["reason"] == reason


def test_dataset_not_loaded_is_unavailable(
    monkeypatch,
):
    install_session(
        monkeypatch,
        company(),
        dataset(
            loaded=False
        ),
        FakeResult(
            scalar=None
        ),
    )

    result = (
        revenue_expense_service
        .get_revenue_expense_check_for_company(
            101
        )
    )

    assert (
        result["result"]
        == "unavailable"
    )

    assert (
        result["reason"]
        == "dataset_not_loaded"
    )
