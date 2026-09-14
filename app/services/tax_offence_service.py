from decimal import Decimal

from sqlalchemy import func, select

from app.database.postgres import get_session
from app.models.company import Company
from app.models.source import DataSet
from app.models.tax_offence import CompanyTaxOffence


# =========================================================
# SETTINGS
# =========================================================


DATASET_CODE = "fns_tax_offence"

SOURCE_CODE = "fns_tax_offence"

ZERO = Decimal("0.00")


# =========================================================
# DATASET
# =========================================================


def _get_dataset_data_date(
    session,
    dataset,
):
    """
    Определяет дату актуального загруженного
    набора taxoffence.

    Основной источник:
        DataSet.last_data_date

    Резерв:
        максимальная фактическая data_date
        среди импортированных документов.
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
                    CompanyTaxOffence.data_date
                )
            )
            .where(
                CompanyTaxOffence.dataset_id
                == dataset.id
            )
        )
        .scalar_one_or_none()
    )


# =========================================================
# EMPTY RESULT
# =========================================================


def _empty_offence_result(
    *,
    checked,
    applicable,
    result,
    data_date=None,
    reason=None,
):
    """
    Создаёт пустой результат проверки
    налоговых правонарушений.

    Возможные состояния:

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

        "reason": reason,

        "has_offence": False,

        "document_date": None,

        "fine_amount": ZERO,

        "document_count": 0,

        "documents": [],
    }


# =========================================================
# FOUND RESULT
# =========================================================


def _rows_to_found_result(
    rows,
    dataset_data_date,
):
    """
    Преобразует найденные документы
    налоговых правонарушений
    в единый check-result.
    """

    total_fine = sum(
        (
            row.fine_amount
            if row.fine_amount
            is not None
            else ZERO
            for row in rows
        ),
        ZERO,
    )

    document_dates = [
        row.document_date
        for row in rows
        if row.document_date
        is not None
    ]

    document_date = (
        max(
            document_dates
        )
        if document_dates
        else None
    )

    documents = [
        {
            "source_document_id": (
                row.source_document_id
            ),

            "document_date": (
                row.document_date
            ),

            "data_date": (
                row.data_date
            ),

            "fine_amount": (
                row.fine_amount
                if row.fine_amount
                is not None
                else ZERO
            ),
        }
        for row in rows
    ]

    return {
        "checked": True,

        "applicable": True,

        "result": "found",

        "data_date": (
            dataset_data_date
        ),

        "dataset_code": (
            DATASET_CODE
        ),

        "source": (
            SOURCE_CODE
        ),

        "reason": None,

        "has_offence": True,

        "document_date": (
            document_date
        ),

        "fine_amount": (
            total_fine
        ),

        "document_count": (
            len(
                rows
            )
        ),

        "documents": (
            documents
        ),
    }


# =========================================================
# STANDARD CHECK CONTRACT
# =========================================================


def get_tax_offence_check_for_company(
    company_id: int,
):
    """
    Стандартизированная проверка компании
    по актуальному загруженному набору
    taxoffence ФНС.

    Возможные result:

    found
        В актуальном опубликованном
        dataset есть сведения
        о налоговом правонарушении.

    not_found
        Dataset успешно загружен
        и применим к компании,
        но запись компании
        в актуальном наборе отсутствует.

    not_applicable
        Текущий dataset не применяется
        к данному типу сущности.

    unavailable
        Проверку нельзя корректно
        выполнить.

    ВАЖНО:

    not_found НЕ означает:

        "у компании никогда не было
         налоговых правонарушений".

    Это означает только:

        "в текущем опубликованном
         наборе ФНС запись не найдена".
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

            return _empty_offence_result(
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

        # Текущий taxoffence dataset
        # содержит ИНН юридических лиц.
        #
        # Для ИП нельзя возвращать
        # ложное "правонарушения не найдены".
        if (
            len(
                inn
            )
            != 10
            or not inn.isdigit()
        ):

            return _empty_offence_result(
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

            return _empty_offence_result(
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

            return _empty_offence_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=None,
                reason="dataset_not_loaded",
            )

        # =================================================
        # 4. CURRENT DATASET
        # =================================================

        rows = (
            session.execute(
                select(
                    CompanyTaxOffence
                )
                .where(
                    CompanyTaxOffence.company_id
                    == company_id,
                    CompanyTaxOffence.dataset_id
                    == dataset.id,
                    CompanyTaxOffence.data_date
                    == dataset_data_date,
                )
                .order_by(
                    CompanyTaxOffence.fine_amount.desc(),
                    CompanyTaxOffence.id,
                )
            )
            .scalars()
            .all()
        )

        # =================================================
        # 5. NOT FOUND
        # =================================================

        if not rows:

            return _empty_offence_result(
                checked=True,
                applicable=True,
                result="not_found",
                data_date=(
                    dataset_data_date
                ),
                reason=None,
            )

        # =================================================
        # 6. FOUND
        # =================================================

        return _rows_to_found_result(
            rows=rows,
            dataset_data_date=(
                dataset_data_date
            ),
        )

    finally:

        session.close()


# =========================================================
# LEGACY / CURRENT WEB INTERFACE
# =========================================================


def get_latest_tax_offence_for_company(
    company_id: int,
):
    """
    Совместимый интерфейс для текущего
    Aggregator и текущего company.html.

    Пока UI окончательно не переведён
    на новый Data Contract:

    found
        возвращаем объект проверки.

    not_found
        тоже возвращаем объект проверки,
        потому что текущий UI уже умеет
        показывать отсутствие сведений
        для юридического лица.

    not_applicable
        возвращаем None.

    unavailable
        возвращаем None.

    Благодаря этому текущая карточка
    не начинает ошибочно показывать ИП:

        "сведения не найдены".

    После подключения tax_offence_check
    в Aggregator и HTML legacy-логику
    можно будет удалить.
    """

    result = (
        get_tax_offence_check_for_company(
            company_id=(
                company_id
            )
        )
    )

    if result is None:
        return None

    if (
        result.get(
            "result"
        )
        not in {
            "found",
            "not_found",
        }
    ):
        return None

    return result


# =========================================================
# HISTORY
# =========================================================


def get_tax_offence_history(
    company_id: int,
    limit: int = 20,
):
    """
    История документов taxoffence.

    Здесь хранятся предыдущие периоды,
    даже если в самом свежем наборе
    компания уже отсутствует.

    История не заменяет результат
    проверки актуального dataset.
    """

    session = get_session()

    try:

        rows = (
            session.execute(
                select(
                    CompanyTaxOffence
                )
                .where(
                    CompanyTaxOffence.company_id
                    == company_id
                )
                .order_by(
                    CompanyTaxOffence.data_date.desc(),
                    CompanyTaxOffence.fine_amount.desc(),
                    CompanyTaxOffence.id.desc(),
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
                "data_date": (
                    row.data_date
                ),

                "document_date": (
                    row.document_date
                ),

                "source_document_id": (
                    row.source_document_id
                ),

                "fine_amount": (
                    row.fine_amount
                    if row.fine_amount
                    is not None
                    else ZERO
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