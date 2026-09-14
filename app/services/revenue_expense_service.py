from decimal import Decimal

from sqlalchemy import (
    func,
    select,
)

from app.database.postgres import (
    get_session,
)
from app.models.company import Company
from app.models.revenue_expense import (
    CompanyRevenueExpenseSnapshot,
)
from app.models.source import DataSet
from app.services.check_result import (
    build_check_result,
)


DATASET_CODE = (
    "fns_revenue_expenses"
)

SOURCE_CODE = (
    "fns_revenue_expenses"
)

ZERO = Decimal("0.00")


def _get_dataset_data_date(
    session,
    dataset,
):
    """
    Возвращает дату актуального
    загруженного набора REVEXP.

    Основной источник:
    DataSet.last_data_date.

    Резервный источник:
    максимальная дата в таблице снимков.
    """

    if (
        dataset is not None
        and dataset.last_data_date
        is not None
    ):
        return (
            dataset.last_data_date
        )

    if dataset is None:
        return None

    return (
        session.execute(
            select(
                func.max(
                    CompanyRevenueExpenseSnapshot
                    .data_date
                )
            )
            .where(
                CompanyRevenueExpenseSnapshot
                .dataset_id
                == dataset.id
            )
        )
        .scalar_one_or_none()
    )


def _empty_payload():
    """
    Единый пустой объект для состояний:

    not_found
    not_applicable
    unavailable
    """

    return {
        "has_data": False,
        "data_year": None,
        "document_date": None,
        "source_document_id": None,
        "source_company_name": None,
        "revenue": ZERO,
        "expenses": ZERO,
        "calculated_difference": ZERO,
        "has_unusual_values": False,
        "quality_flags": [],
        "can_use_for_assessment": False,
    }


def _snapshot_to_check(
    snapshot,
):
    """
    Превращает запись PostgreSQL
    в контракт для агрегатора и карточки.

    Отрицательные значения сохраняются,
    но отмечаются предупреждением.
    """

    quality_flags = []

    if snapshot.revenue < ZERO:

        quality_flags.append(
            "negative_revenue"
        )

    if snapshot.expenses < ZERO:

        quality_flags.append(
            "negative_expenses"
        )

    has_unusual_values = bool(
        quality_flags
    )

    return build_check_result(
        checked=True,
        applicable=True,
        result="found",
        data_date=(
            snapshot.data_date
        ),
        dataset_code=(
            DATASET_CODE
        ),
        source=(
            SOURCE_CODE
        ),
        reason=None,
        has_data=True,
        data_year=(
            snapshot.data_year
        ),
        document_date=(
            snapshot.document_date
        ),
        source_document_id=(
            snapshot.source_document_id
        ),
        source_company_name=(
            snapshot.source_company_name
        ),
        revenue=(
            snapshot.revenue
        ),
        expenses=(
            snapshot.expenses
        ),
        calculated_difference=(
            snapshot.profit_loss
        ),
        has_unusual_values=(
            has_unusual_values
        ),
        quality_flags=(
            quality_flags
        ),
        can_use_for_assessment=(
            not has_unusual_values
        ),
    )


def get_revenue_expense_check_for_company(
    company_id: int,
):
    """
    Возвращает последний снимок REVEXP
    в общем четырёхсоставном контракте.

    Возможные результаты:

    found
        сведения найдены;

    not_found
        актуальный набор проверен,
        но запись компании не найдена;

    not_applicable
        набор не применяется к ИП;

    unavailable
        набор не зарегистрирован
        или не загружен.
    """

    session = get_session()

    try:
        company = (
            session.execute(
                select(
                    Company.id,
                    Company.inn,
                    Company.entity_type,
                )
                .where(
                    Company.id
                    == company_id
                )
            )
            .mappings()
            .one_or_none()
        )

        if company is None:

            return build_check_result(
                checked=False,
                applicable=None,
                result="unavailable",
                data_date=None,
                dataset_code=(
                    DATASET_CODE
                ),
                source=(
                    SOURCE_CODE
                ),
                reason=(
                    "company_not_found"
                ),
                **_empty_payload(),
            )

        inn = str(
            company["inn"]
            or ""
        ).strip()

        # REVEXP относится только
        # к юридическим лицам.
        if (
            len(inn) != 10
            or not inn.isdigit()
        ):

            return build_check_result(
                checked=True,
                applicable=False,
                result="not_applicable",
                data_date=None,
                dataset_code=(
                    DATASET_CODE
                ),
                source=(
                    SOURCE_CODE
                ),
                reason=(
                    "legal_entities_only"
                ),
                **_empty_payload(),
            )

        dataset = (
            session.execute(
                select(
                    DataSet
                )
                .where(
                    DataSet.code
                    == DATASET_CODE
                )
            )
            .scalar_one_or_none()
        )

        if dataset is None:

            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=None,
                dataset_code=(
                    DATASET_CODE
                ),
                source=(
                    SOURCE_CODE
                ),
                reason=(
                    "dataset_not_registered"
                ),
                **_empty_payload(),
            )

        dataset_data_date = (
            _get_dataset_data_date(
                session=session,
                dataset=dataset,
            )
        )

        if dataset_data_date is None:

            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=None,
                dataset_code=(
                    DATASET_CODE
                ),
                source=(
                    SOURCE_CODE
                ),
                reason=(
                    "dataset_not_loaded"
                ),
                **_empty_payload(),
            )

        snapshot = (
            session.execute(
                select(
                    CompanyRevenueExpenseSnapshot
                )
                .where(
                    CompanyRevenueExpenseSnapshot
                    .company_id
                    == company_id,

                    CompanyRevenueExpenseSnapshot
                    .dataset_id
                    == dataset.id,

                    CompanyRevenueExpenseSnapshot
                    .data_date
                    == dataset_data_date,
                )
            )
            .scalar_one_or_none()
        )

        if snapshot is None:

            empty = (
                _empty_payload()
            )

            empty[
                "data_year"
            ] = (
                dataset_data_date.year
            )

            return build_check_result(
                checked=True,
                applicable=True,
                result="not_found",
                data_date=(
                    dataset_data_date
                ),
                dataset_code=(
                    DATASET_CODE
                ),
                source=(
                    SOURCE_CODE
                ),
                reason=None,
                **empty,
            )

        return (
            _snapshot_to_check(
                snapshot
            )
        )

    finally:
        session.close()