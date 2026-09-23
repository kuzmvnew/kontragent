from dataclasses import replace
from decimal import Decimal

from sqlalchemy import func, select

from app.database.postgres import get_session
from app.models.company import Company
from app.models.source import DataSet
from app.models.tax_debt import (
    TAX_DEBT_FACT_CODE,
    CompanyTaxDebtItem,
    CompanyTaxDebtSnapshot,
)
from app.services.tax_debt_freshness import (
    STALE_DATA,
    TaxDebtFreshness,
    TaxDebtPublicationContext,
    evaluate_tax_debt_freshness,
    resolve_tax_debt_publication,
    utc_now,
)

# =========================================================
# SETTINGS
# =========================================================


DATASET_CODE = "fns_tax_debt"

SOURCE_CODE = "fns_tax_debt"

ZERO = Decimal("0.00")


def _publication_scope(session, *, dataset, inn):
    """Resolve one company's data date/generation without widening pilot scope."""

    context = resolve_tax_debt_publication(
        session,
        dataset=dataset,
        inn=inn,
    )
    return context.data_as_of, context.publication_generation


def _freshness_fields(
    context: TaxDebtPublicationContext,
    freshness: TaxDebtFreshness,
    *,
    state: str,
) -> dict:
    return {
        "state": state,
        "freshness": "fresh" if freshness.is_fresh else "stale",
        "source_as_of": context.source_as_of,
        "data_as_of": context.data_as_of,
        "retrieved_at": context.retrieved_at,
        "official_actual_until": context.official_actual_until,
        "checked_at": freshness.checked_at,
        "freshness_reason": freshness.reason,
        "freshness_missing_fields": list(freshness.missing_fields),
    }


def _stale_debt_result(
    context: TaxDebtPublicationContext,
    freshness: TaxDebtFreshness,
) -> dict:
    """Keep the existing check contract while exposing authoritative stale state."""

    result = _empty_debt_result(
        checked=False,
        applicable=True,
        result="unavailable",
        data_date=context.data_as_of,
        reason=freshness.reason,
    )
    result["limitation_states"] = list(
        dict.fromkeys(("stale_data", freshness.reason))
    )
    result.update(
        _freshness_fields(
            context,
            freshness,
            state=STALE_DATA,
        )
    )
    return result


# =========================================================
# DATASET
# =========================================================


def _get_dataset_data_date(
    session,
    dataset,
    *,
    publication_generation=0,
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
                == dataset.id,
                CompanyTaxDebtSnapshot.publication_generation
                == publication_generation,
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

    Read-side state:

    FOUND / NOT_FOUND
        Возвращаются только для свежего и полностью
        подтверждённого publication snapshot.

    STALE_DATA
        Истёкший, неполный или несогласованный snapshot.
        Для обратной совместимости result остаётся
        unavailable, поэтому публичный API-контракт
        found/not_found/not_applicable/unavailable не меняется.

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

        publication = resolve_tax_debt_publication(
            session,
            dataset=dataset,
            inn=inn,
        )
        dataset_data_date = publication.data_as_of
        publication_generation = publication.publication_generation
        if dataset_data_date is None:
            dataset_data_date = _get_dataset_data_date(
                session=session,
                dataset=dataset,
                publication_generation=publication_generation,
            )
            publication = replace(
                publication,
                data_as_of=dataset_data_date,
            )

        if dataset_data_date is None:

            return _empty_debt_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=None,
                reason="dataset_not_loaded",
            )

        freshness = evaluate_tax_debt_freshness(
            publication,
            checked_at=utc_now(),
        )
        if not freshness.is_fresh:
            return _stale_debt_result(
                publication,
                freshness,
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
                    CompanyTaxDebtSnapshot.publication_generation
                    == publication_generation,
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

            result = _empty_debt_result(
                checked=True,
                applicable=True,
                result="not_found",
                data_date=dataset_data_date,
                reason=None,
            )
            result.update(
                _freshness_fields(
                    publication,
                    freshness,
                    state="NOT_FOUND",
                )
            )
            return result

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

        result = _snapshot_to_result(
            snapshot=snapshot,
            items=items,
        )
        result.update(
            _freshness_fields(
                publication,
                freshness,
                state="FOUND",
            )
        )
        return result

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

        company = session.get(Company, company_id)
        dataset = session.execute(
            select(DataSet).where(DataSet.code == DATASET_CODE)
        ).scalar_one_or_none()
        if company is None or dataset is None:
            return []
        _, publication_generation = _publication_scope(
            session,
            dataset=dataset,
            inn=str(company.inn or "").strip(),
        )

        rows = (
            session.execute(
                select(
                    CompanyTaxDebtSnapshot
                )
                .where(
                    CompanyTaxDebtSnapshot.company_id
                    == company_id,
                    CompanyTaxDebtSnapshot.dataset_id == dataset.id,
                    CompanyTaxDebtSnapshot.publication_generation
                    == publication_generation,
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
