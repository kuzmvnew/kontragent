from decimal import Decimal

from sqlalchemy import func, select

from app.database.postgres import get_session
from app.models.company import Company
from app.models.source import DataSet
from app.models.tax_offence import CompanyTaxOffence


DATASET_CODE = "fns_tax_offence"

ZERO = Decimal("0.00")


def _get_dataset_data_date(
    session,
    dataset,
):
    """
    Определяет дату актуального загруженного
    набора taxoffence.

    Основной источник — DataSet.last_data_date.

    Если она почему-то не заполнена,
    используем максимальную дату среди
    реально импортированных документов.
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


def get_latest_tax_offence_for_company(
    company_id: int,
):
    """
    Возвращает результат проверки компании
    по актуальному загруженному набору
    taxoffence ФНС.

    Для юридического лица возможны два
    корректных результата:

    1. has_offence = True
       В актуальном наборе ФНС есть запись.

    2. has_offence = False
       Набор проверен, но запись компании
       в актуальном опубликованном срезе
       не найдена.

    Для ИП возвращается None, потому что
    текущий набор содержит ИННЮЛ.
    """

    session = get_session()

    try:
        # -------------------------------------------------
        # COMPANY
        # -------------------------------------------------

        company = (
            session.execute(
                select(
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
            return None

        inn = str(
            company["inn"]
            or ""
        ).strip()

        # taxoffence содержит юридические лица.
        if len(inn) != 10:
            return None

        # -------------------------------------------------
        # DATASET
        # -------------------------------------------------

        dataset = (
            session.execute(
                select(DataSet)
                .where(
                    DataSet.code
                    == DATASET_CODE
                )
            )
            .scalar_one_or_none()
        )

        if dataset is None:
            return None

        dataset_data_date = (
            _get_dataset_data_date(
                session=session,
                dataset=dataset,
            )
        )

        # Если мы вообще не знаем дату набора,
        # нельзя говорить, что проверка выполнена.
        if dataset_data_date is None:
            return None

        # -------------------------------------------------
        # CURRENT DATASET
        # -------------------------------------------------

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

        # -------------------------------------------------
        # NO OFFENCE IN CURRENT DATASET
        # -------------------------------------------------

        if not rows:
            return {
                "checked": True,
                "applicable": True,
                "has_offence": False,
                "result": "not_found",
                "data_date": (
                    dataset_data_date
                ),
                "document_date": None,
                "fine_amount": ZERO,
                "document_count": 0,
                "documents": [],
                "dataset_code": (
                    DATASET_CODE
                ),
                "source": (
                    "fns_tax_offence"
                ),
            }

        # -------------------------------------------------
        # OFFENCE FOUND
        # -------------------------------------------------

        total_fine = sum(
            (
                row.fine_amount
                for row in rows
            ),
            ZERO,
        )

        document_dates = [
            row.document_date
            for row in rows
            if row.document_date is not None
        ]

        document_date = (
            max(document_dates)
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
                ),
            }
            for row in rows
        ]

        return {
            "checked": True,
            "applicable": True,
            "has_offence": True,
            "result": "found",
            "data_date": (
                dataset_data_date
            ),
            "document_date": (
                document_date
            ),
            "fine_amount": (
                total_fine
            ),
            "document_count": (
                len(rows)
            ),
            "documents": (
                documents
            ),
            "dataset_code": (
                DATASET_CODE
            ),
            "source": (
                "fns_tax_offence"
            ),
        }

    finally:
        session.close()


def get_tax_offence_history(
    company_id: int,
    limit: int = 20,
):
    """
    История документов taxoffence.

    Здесь хранятся предыдущие периоды,
    даже если в самом свежем наборе
    компания уже отсутствует.
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
                .limit(limit)
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
                ),
            }
            for row in rows
        ]

    finally:
        session.close()