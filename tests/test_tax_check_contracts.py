from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.aggregators.company_aggregator import check_used_source
from app.services import tax_debt_service, tax_offence_service, tax_payment_service
from app.services.check_result import build_check_result

MISSING = object()
DATA_DATE = date(2026, 8, 1)


class FakeResult:
    def __init__(
        self,
        *,
        mapping=MISSING,
        scalar=MISSING,
        rows=MISSING,
    ):
        self.mapping = mapping
        self.scalar = scalar
        self.rows = rows

    def mappings(self):
        return self

    def one_or_none(self):
        if self.mapping is MISSING:
            raise AssertionError(
                "Не настроен mapping-результат"
            )

        return self.mapping

    def scalar_one_or_none(self):
        if self.scalar is MISSING:
            raise AssertionError(
                "Не настроен scalar-результат"
            )

        return self.scalar

    def scalars(self):
        return self

    def all(self):
        if self.rows is MISSING:
            raise AssertionError(
                "Не настроен список строк"
            )

        return self.rows


class FakeSession:
    def __init__(
        self,
        *results,
    ):
        self.results = list(results)
        self.closed = False
        self.execute_count = 0

    def execute(
        self,
        _statement,
    ):
        self.execute_count += 1

        if not self.results:
            raise AssertionError(
                "Сервис выполнил больше SQL-запросов, "
                "чем ожидалось"
            )

        return self.results.pop(0)

    def close(self):
        self.closed = True


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    module: object
    call: object
    dataset_code: str
    empty_flag: str
    result_kind: str


SERVICE_SPECS = [
    ServiceSpec(
        name="payment",
        module=tax_payment_service,
        call=lambda: (
            tax_payment_service
            .get_latest_tax_payment_for_company(
                101
            )
        ),
        dataset_code="fns_tax_paid",
        empty_flag="has_data",
        result_kind="scalar",
    ),
    ServiceSpec(
        name="debt",
        module=tax_debt_service,
        call=lambda: (
            tax_debt_service
            .get_tax_debt_check_for_company(
                101,
                include_items=False,
            )
        ),
        dataset_code="fns_tax_debt",
        empty_flag="has_debt",
        result_kind="scalar",
    ),
    ServiceSpec(
        name="offence",
        module=tax_offence_service,
        call=lambda: (
            tax_offence_service
            .get_tax_offence_check_for_company(
                101
            )
        ),
        dataset_code="fns_tax_offence",
        empty_flag="has_offence",
        result_kind="rows",
    ),
]


def company_result(
    inn,
):
    return FakeResult(
        mapping={
            "id": 101,
            "inn": inn,
            "entity_type": (
                "legal"
                if len(inn) == 10
                else "individual_entrepreneur"
            ),
        }
    )


def dataset_result(
    *,
    loaded=True,
):
    return FakeResult(
        scalar=SimpleNamespace(
            id=202,
            last_data_date=(
                DATA_DATE
                if loaded
                else None
            ),
            source_as_of=datetime(2026, 8, 2, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            coverage={"official_actual_until": "2099-12-31"},
        )
    )


def empty_source_result(
    spec,
):
    if spec.result_kind == "rows":
        return FakeResult(
            rows=[]
        )

    return FakeResult(
        scalar=None
    )


def install_fake_session(
    monkeypatch,
    module,
    *results,
):
    session = FakeSession(
        *results
    )

    monkeypatch.setattr(
        module,
        "get_session",
        lambda: session,
    )

    return session


def assert_common_contract(
    result,
    *,
    expected_result,
    checked,
    applicable,
    dataset_code,
):
    assert (
        result["result"]
        == expected_result
    )

    assert (
        result["checked"]
        is checked
    )

    assert (
        result["applicable"]
        is applicable
    )

    assert (
        result["dataset_code"]
        == dataset_code
    )

    assert (
        result["source"]
        == dataset_code
    )

    assert "data_date" in result
    assert "reason" in result


@pytest.mark.parametrize(
    (
        "result_name",
        "checked",
        "applicable",
    ),
    [
        (
            "found",
            True,
            True,
        ),
        (
            "not_found",
            True,
            True,
        ),
        (
            "not_applicable",
            True,
            False,
        ),
        (
            "unavailable",
            False,
            None,
        ),
    ],
)
def test_build_check_result_accepts_valid_states(
    result_name,
    checked,
    applicable,
):
    result = build_check_result(
        checked=checked,
        applicable=applicable,
        result=result_name,
        data_date=DATA_DATE,
        dataset_code="test_dataset",
        source="test_source",
    )

    assert (
        result["result"]
        == result_name
    )

    assert (
        result["checked"]
        is checked
    )

    assert (
        result["applicable"]
        is applicable
    )


@pytest.mark.parametrize(
    (
        "result_name",
        "checked",
        "applicable",
    ),
    [
        (
            "found",
            False,
            True,
        ),
        (
            "not_found",
            True,
            False,
        ),
        (
            "not_applicable",
            True,
            True,
        ),
        (
            "unavailable",
            True,
            None,
        ),
    ],
)
def test_build_check_result_rejects_invalid_state_flags(
    result_name,
    checked,
    applicable,
):
    with pytest.raises(
        ValueError
    ):
        build_check_result(
            checked=checked,
            applicable=applicable,
            result=result_name,
            data_date=None,
            dataset_code="test_dataset",
            source="test_source",
        )


def test_build_check_result_rejects_unknown_state():
    with pytest.raises(
        ValueError
    ):
        build_check_result(
            checked=True,
            applicable=True,
            result="unknown",
            data_date=None,
            dataset_code="test_dataset",
            source="test_source",
        )


@pytest.mark.parametrize(
    (
        "result_name",
        "expected",
    ),
    [
        (
            "found",
            True,
        ),
        (
            "not_found",
            True,
        ),
        (
            "not_applicable",
            False,
        ),
        (
            "unavailable",
            False,
        ),
    ],
)
def test_aggregator_marks_only_completed_checks_as_used(
    result_name,
    expected,
):
    assert (
        check_used_source(
            {
                "result": result_name,
            }
        )
        is expected
    )


@pytest.mark.parametrize(
    "spec",
    SERVICE_SPECS,
    ids=lambda spec: spec.name,
)
@pytest.mark.parametrize(
    "state",
    [
        "not_found",
        "not_applicable",
        "dataset_not_registered",
        "dataset_not_loaded",
    ],
)
def test_tax_check_non_found_states(
    monkeypatch,
    spec,
    state,
):
    if state == "not_found":
        results = (
            company_result(
                "7701234567"
            ),
            dataset_result(),
            empty_source_result(
                spec
            ),
        )

        expected = (
            "not_found",
            True,
            True,
            None,
        )

    elif state == "not_applicable":
        results = (
            company_result(
                "770123456789"
            ),
        )

        expected = (
            "not_applicable",
            True,
            False,
            "legal_entities_only",
        )

    elif state == "dataset_not_registered":
        results = (
            company_result(
                "7701234567"
            ),
            FakeResult(
                scalar=None
            ),
        )

        expected = (
            "unavailable",
            False,
            True,
            "dataset_not_registered",
        )

    else:
        results = (
            company_result(
                "7701234567"
            ),
            dataset_result(
                loaded=False
            ),
            FakeResult(
                scalar=None
            ),
        )

        expected = (
            "unavailable",
            False,
            True,
            "dataset_not_loaded",
        )

    session = install_fake_session(
        monkeypatch,
        spec.module,
        *results,
    )

    result = spec.call()

    assert_common_contract(
        result,
        expected_result=expected[0],
        checked=expected[1],
        applicable=expected[2],
        dataset_code=spec.dataset_code,
    )

    assert (
        result["reason"]
        == expected[3]
    )

    assert (
        result[
            spec.empty_flag
        ]
        is False
    )

    assert (
        session.closed
        is True
    )


def test_payment_check_found(
    monkeypatch,
):
    snapshot = SimpleNamespace(
        data_date=DATA_DATE,
        data_year=2025,
        document_date=date(
            2026,
            7,
            25,
        ),
        source_document_id="PAY-1",
        source_company_name="ООО ТЕСТ",
        total_amount=Decimal(
            "150.00"
        ),
        tax_amount=Decimal(
            "100.00"
        ),
        insurance_amount=Decimal(
            "50.00"
        ),
        penalty_amount=Decimal(
            "0.00"
        ),
        non_tax_amount=Decimal(
            "0.00"
        ),
        other_amount=Decimal(
            "0.00"
        ),
        source_item_count=2,
        stored_item_count=2,
        items=[
            SimpleNamespace(
                tax_name=(
                    "Страховые взносы"
                ),
                payment_type=(
                    "insurance"
                ),
                amount=Decimal(
                    "50.00"
                ),
            ),
            SimpleNamespace(
                tax_name=(
                    "Налог на прибыль"
                ),
                payment_type="tax",
                amount=Decimal(
                    "100.00"
                ),
            ),
        ],
    )

    session = install_fake_session(
        monkeypatch,
        tax_payment_service,
        company_result(
            "7701234567"
        ),
        dataset_result(),
        FakeResult(
            scalar=snapshot
        ),
    )

    result = (
        tax_payment_service
        .get_latest_tax_payment_for_company(
            101
        )
    )

    assert_common_contract(
        result,
        expected_result="found",
        checked=True,
        applicable=True,
        dataset_code="fns_tax_paid",
    )

    assert (
        result["has_data"]
        is True
    )

    assert (
        result["total_amount"]
        == Decimal("150.00")
    )

    assert (
        result["items"][0]["amount"]
        == Decimal("100.00")
    )

    assert (
        session.closed
        is True
    )


def test_debt_check_found(
    monkeypatch,
):
    snapshot = SimpleNamespace(
        id=303,
        data_date=DATA_DATE,
        document_date=date(
            2026,
            8,
            5,
        ),
        source_document_id="DEBT-1",
        total_arrears=Decimal(
            "100.00"
        ),
        total_penalties=Decimal(
            "20.00"
        ),
        total_fines=Decimal(
            "5.00"
        ),
        total_debt=Decimal(
            "125.00"
        ),
        item_count=1,
    )

    session = install_fake_session(
        monkeypatch,
        tax_debt_service,
        company_result(
            "7701234567"
        ),
        dataset_result(),
        FakeResult(
            scalar=snapshot
        ),
    )

    result = (
        tax_debt_service
        .get_tax_debt_check_for_company(
            101,
            include_items=False,
        )
    )

    assert_common_contract(
        result,
        expected_result="found",
        checked=True,
        applicable=True,
        dataset_code="fns_tax_debt",
    )

    assert (
        result["has_debt"]
        is True
    )

    assert (
        result["total_debt"]
        == Decimal("125.00")
    )

    assert (
        session.closed
        is True
    )


def test_offence_check_found(
    monkeypatch,
):
    rows = [
        SimpleNamespace(
            id=1,
            source_document_id=(
                "OFFENCE-1"
            ),
            document_date=date(
                2026,
                7,
                20,
            ),
            data_date=DATA_DATE,
            fine_amount=Decimal(
                "100.00"
            ),
        ),
        SimpleNamespace(
            id=2,
            source_document_id=(
                "OFFENCE-2"
            ),
            document_date=date(
                2026,
                7,
                25,
            ),
            data_date=DATA_DATE,
            fine_amount=Decimal(
                "25.00"
            ),
        ),
    ]

    session = install_fake_session(
        monkeypatch,
        tax_offence_service,
        company_result(
            "7701234567"
        ),
        dataset_result(),
        FakeResult(
            rows=rows
        ),
    )

    result = (
        tax_offence_service
        .get_tax_offence_check_for_company(
            101
        )
    )

    assert_common_contract(
        result,
        expected_result="found",
        checked=True,
        applicable=True,
        dataset_code="fns_tax_offence",
    )

    assert (
        result["has_offence"]
        is True
    )

    assert (
        result["fine_amount"]
        == Decimal("125.00")
    )

    assert (
        result["document_count"]
        == 2
    )

    assert (
        result["document_date"]
        == date(
            2026,
            7,
            25,
        )
    )

    assert (
        session.closed
        is True
    )


@pytest.mark.parametrize(
    "service_module",
    [
        tax_payment_service,
        tax_debt_service,
        tax_offence_service,
    ],
)
def test_dataset_date_falls_back_to_latest_stored_date(
    service_module,
):
    session = FakeSession(
        FakeResult(
            scalar=DATA_DATE
        )
    )

    dataset = SimpleNamespace(
        id=202,
        last_data_date=None,
    )

    result = (
        service_module
        ._get_dataset_data_date(
            session=session,
            dataset=dataset,
        )
    )

    assert (
        result
        == DATA_DATE
    )

    assert (
        session.execute_count
        == 1
    )
