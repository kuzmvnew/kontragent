from decimal import Decimal

from sqlalchemy import exists, func, or_, select

from app.database.postgres import get_session
from app.models.company import Company
from app.models.source import DataSet
from app.models.tax_debt import (
    CompanyTaxDebtItem,
    CompanyTaxDebtSnapshot,
    FnsTaxDebtPilotState,
    TAX_DEBT_FACT_CODE,
)


# =========================================================
# SETTINGS
# =========================================================


DATASET_CODE = "fns_tax_debt"

SOURCE_CODE = "fns_tax_debt"

ZERO = Decimal("0.00")


def _active_generation_condition():
    pilot_exists = exists(
        select(FnsTaxDebtPilotState.source_id).where(
            FnsTaxDebtPilotState.source_id == "S02",
            FnsTaxDebtPilotState.query_generation > 0,
        )
    )
    active_generation = (
        select(FnsTaxDebtPilotState.query_generation)
        .where(FnsTaxDebtPilotState.source_id == "S02")
        .scalar_subquery()
    )
    return or_(
        ~pilot_exists,
        CompanyTaxDebtSnapshot.publication_generation == active_generation,
    )


# =========================================================
# DATASET
# =========================================================


def _get_dataset_data_date(
    session,
    dataset,
):
    """
    Определяет дату актуального загруженного
    набора налоговой задолженности.

    Основной источник:
        DataSet.last_data_date

    Резерв:
        максимальная фактическая data_date
        среди импортированных snapshots.
    """

    if (
        dataset is not None
        and dataset.last_data_date
        is not None
    ):
        return dataset.last_data_date

    if dataset is None:
        return None

    return (
        session.execute(
            select(
                func.max(
                    CompanyTaxDebtSnapshot.data_date
                )
            )
            .where(
                CompanyTaxDebtSnapshot.dataset_id
                == dataset.id
            )
        )
        .scalar_one_or_none()
    )


# =========================================================
# EMPTY RESULT
# =========================================================


def _empty_debt_result(
    *,
    checked,
    applicable,
    result,
    data_date=None,
    reason=None,
):
    """
    Создаёт пустой результат проверки
    задолженности в едином формате.

    Используется для:

    not_found
    not_applicable
    unavailable
    """

    return {
        "checked": checked,
        "applicable": applicable,
        "result": result,

        "data_date": data_date,

        "dataset_code": (
            DATASET_CODE
        ),

        "source": (
            SOURCE_CODE
        ),

        "fact_code": TAX_DEBT_FACT_CODE,

        "source_reference": None,

        "provenance": {},

        "limitation_states": [],

        "retrieved_at": None,

        "reason": reason,

        "has_debt": False,

        "snapshot_id": None,

        "document_date": None,

        "source_document_id": None,

        "total_arrears": ZERO,

        "total_penalties": ZERO,

        "total_fines": ZERO,

        "total_debt": ZERO,

        "item_count": 0,

        "items": [],
    }


# =========================================================
# ITEMS
# =========================================================


def _load_snapshot_items(
    session,
    snapshot_id,
):
    """
    Загружает детализацию задолженности
    для одного snapshot.
    """

    item_rows = (
        session.execute(
            select(
                CompanyTaxDebtItem
            )
            .where(
                CompanyTaxDebtItem.snapshot_id
                == snapshot_id
            )
            .order_by(
                CompanyTaxDebtItem.total.desc(),
                CompanyTaxDebtItem.tax_name,
            )
        )
        .scalars()
        .all()
    )

    return [
        {
            "tax_name": (
                item.tax_name
            ),

            "arrears": (
                item.arrears
            ),

            "penalties": (
                item.penalties
            ),

            "fines": (
                item.fines
            ),

            "total": (
                item.total
            ),
        }
        for item in item_rows
    ]


# =========================================================
# SNAPSHOT NORMALIZATION
# =========================================================


def _snapshot_to_result(
    *,
    snapshot,
    items,
):
    """
    Преобразует snapshot задолженности
    в единый check-result.
    """

    total_debt = (
        snapshot.total_debt
        if snapshot.total_debt is not None
        else ZERO
    )

    return {
        "checked": True,

        "applicable": True,

        "result": "found",

        "data_date": (
            snapshot.data_date
        ),

        "dataset_code": (
            DATASET_CODE
        ),

        "source": (
            SOURCE_CODE
        ),

        "fact_code": getattr(
            snapshot,
            "fact_code",
            TAX_DEBT_FACT_CODE,
        ),

        "source_reference": getattr(
            snapshot,
            "source_reference",
            None,
        ),

        "provenance": dict(
            getattr(snapshot, "provenance", None)
            or {}
        ),

        "limitation_states": list(
            getattr(snapshot, "limitation_states", None)
            or []
        ),

        "retrieved_at": getattr(
            snapshot,
            "retrieved_at",
            None,
        ),

        "reason": None,

        # Важно:
        # result=found означает, что запись
        # компании присутствует в dataset.
        #
        # has_debt отдельно показывает,
        # является ли сумма положительной.
        "has_debt": (
            total_debt > ZERO
        ),

        "snapshot_id": (
            snapshot.id
        ),

        "document_date": (
            snapshot.document_date
        ),

        "source_document_id": (
            snapshot.source_document_id
        ),

        "total_arrears": (
            snapshot.total_arrears
        ),

        "total_penalties": (
            snapshot.total_penalties
        ),

        "total_fines": (
            snapshot.total_fines
        ),

        "total_debt": (
            total_debt
        ),

        "item_count": (
            snapshot.item_count
        ),

        "items": (
            items
        ),
    }


# =========================================================
# STANDARD CHECK CONTRACT
# =========================================================


def get_tax_debt_check_for_company(
    company_id: int,
    include_items: bool = True,
):
    """
    Стандартизированная проверка
    налоговой задолженности.

    Возможные result:

    found
        В актуальном dataset есть
        запись компании.

    not_found
        Dataset успешно загружен,
        применим к компании,
        но запись компании
        в актуальном срезе не найдена.

    not_applicable
        Текущий dataset не применяется
        к этому типу сущности.

    unavailable
        Проверку нельзя корректно
        выполнить.

    ВАЖНО:

    not_found НЕ означает автоматически:

        "у компании никогда не было
         налоговой задолженности".

    Это означает только:

        "в текущем загруженном
         опубликованном наборе
         запись не найдена".
    """

    session = get_session()

    try:

        # =================================================
        # 1. COMPANY
        # =================================================

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

            return _empty_debt_result(
                checked=False,
                applicable=None,
                result="unavailable",
                data_date=None,
                reason="company_not_found",
            )

        inn = str(
            company["inn"]
            or ""
        ).strip()

        # =================================================
        # 2. APPLICABILITY
        # =================================================

        # В текущей архитектуре debtam
        # сопоставляется с юридическими лицами
        # по 10-значному ИНН.
        #
        # Для ИП не создаём ложный
        # результат "задолженность не найдена".
        if (
            len(inn) != 10
            or not inn.isdigit()
        ):

            return _empty_debt_result(
                checked=True,
                applicable=False,
                result="not_applicable",
                data_date=None,
                reason="legal_entities_only",
            )

        # =================================================
        # 3. DATASET
        # =================================================

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

            return _empty_debt_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=None,
                reason="dataset_not_registered",
            )

        dataset_data_date = (
            _get_dataset_data_date(
                session=session,
                dataset=dataset,
            )
        )

        if dataset_data_date is None:

            return _empty_debt_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=None,
                reason="dataset_not_loaded",
            )

        # =================================================
        # 4. CURRENT SNAPSHOT
        # =================================================

        snapshot = (
            session.execute(
                select(
                    CompanyTaxDebtSnapshot
                )
                .where(
                    CompanyTaxDebtSnapshot.company_id
                    == company_id,
                    CompanyTaxDebtSnapshot.dataset_id
                    == dataset.id,
                    CompanyTaxDebtSnapshot.data_date
                    == dataset_data_date,
                    _active_generation_condition(),
                )
                .order_by(
                    CompanyTaxDebtSnapshot.id.desc()
                )
                .limit(1)
            )
            .scalar_one_or_none()
        )

        # =================================================
        # 5. NOT FOUND
        # =================================================

        if snapshot is None:

            return _empty_debt_result(
                checked=True,
                applicable=True,
                result="not_found",
                data_date=dataset_data_date,
                reason=None,
            )

        # =================================================
        # 6. FOUND
        # =================================================

        items = []

        if include_items:

            items = (
                _load_snapshot_items(
                    session=session,
                    snapshot_id=(
                        snapshot.id
                    ),
                )
            )

        return _snapshot_to_result(
            snapshot=snapshot,
            items=items,
        )

    finally:

        session.close()


# =========================================================
# LEGACY / CURRENT WEB INTERFACE
# =========================================================


def get_latest_tax_debt_for_company(
    company_id: int,
    include_items: bool = True,
):
    """
    Совместимый интерфейс для текущего
    Aggregator и текущего company.html.

    Пока Data Contract Cleanup
    не подключён к веб-слою:

    found
        возвращаем объект задолженности.

    not_found
    not_applicable
    unavailable
        возвращаем None.

    Благодаря этому текущая карточка
    сайта не меняет поведение во время
    первого этапа миграции.

    После проверки нового контракта
    Aggregator будет переведён на
    get_tax_debt_check_for_company().
    """

    result = (
        get_tax_debt_check_for_company(
            company_id=company_id,
            include_items=include_items,
        )
    )

    if (
        result is None
        or result.get("result")
        != "found"
    ):
        return None

    return result


def prepare_tax_debt_public_projection(check: dict) -> dict:
    """Prepare public-card input without changing routes, templates, or wording."""

    return {
        "fact_code": check.get("fact_code") or TAX_DEBT_FACT_CODE,
        "result": check.get("result"),
        "applicable": check.get("applicable"),
        "has_debt": bool(check.get("has_debt")),
        "amount": check.get("total_debt"),
        "amount_as_of_date": check.get("data_date"),
        "source": check.get("source") or SOURCE_CODE,
        "source_reference": check.get("source_reference"),
        "provenance": dict(check.get("provenance") or {}),
        "limitation_states": list(check.get("limitation_states") or []),
        "retrieved_at": check.get("retrieved_at"),
    }


# =========================================================
# HISTORY
# =========================================================


def get_tax_debt_history(
    company_id: int,
    limit: int = 24,
):
    """
    Возвращает историю задолженности
    от нового среза к старому.

    История является отдельным объектом
    и не заменяет результат проверки
    актуального dataset.
    """

    session = get_session()

    try:

        rows = (
            session.execute(
                select(
                    CompanyTaxDebtSnapshot
                )
                .where(
                    CompanyTaxDebtSnapshot.company_id
                    == company_id,
                    _active_generation_condition(),
                )
                .order_by(
                    CompanyTaxDebtSnapshot.data_date.desc(),
                    CompanyTaxDebtSnapshot.id.desc(),
                )
                .limit(
                    limit
                )
            )
            .scalars()
            .all()
        )

        return [
            {
                "snapshot_id": (
                    row.id
                ),

                "data_date": (
                    row.data_date
                ),

                "document_date": (
                    row.document_date
                ),

                "source_document_id": (
                    row.source_document_id
                ),

                "total_arrears": (
                    row.total_arrears
                ),

                "total_penalties": (
                    row.total_penalties
                ),

                "total_fines": (
                    row.total_fines
                ),

                "total_debt": (
                    row.total_debt
                ),

                "item_count": (
                    row.item_count
                ),

                "dataset_code": (
                    DATASET_CODE
                ),

                "source": (
                    SOURCE_CODE
                ),
            }
            for row in rows
        ]

    finally:

        session.close()
