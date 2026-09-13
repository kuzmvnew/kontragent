from sqlalchemy import select

from app.database.postgres import get_session
from app.models.headcount import CompanyHeadcount
from app.models.source import DataSet


def get_latest_headcount_for_company(
    company_id: int,
):
    """
    Возвращает последнюю известную
    среднесписочную численность компании.

    Источник сейчас:
    fns_headcount.

    В будущем здесь сможет быть
    несколько datasets с разными
    приоритетами.
    """

    session = get_session()

    try:
        statement = (
            select(
                CompanyHeadcount,
                DataSet,
            )
            .join(
                DataSet,
                CompanyHeadcount.dataset_id
                == DataSet.id,
            )
            .where(
                CompanyHeadcount.company_id
                == company_id
            )
            .order_by(
                CompanyHeadcount.year.desc(),
                CompanyHeadcount.id.desc(),
            )
            .limit(1)
        )

        row = (
            session.execute(
                statement
            )
            .first()
        )

        if row is None:
            return None

        headcount, dataset = row

        return {
            "employee_count": (
                headcount.employee_count
            ),
            "year": (
                headcount.year
            ),
            "dataset_id": (
                dataset.id
            ),
            "dataset_code": (
                dataset.code
            ),
            "priority": (
                dataset.priority
            ),
            "source_document_id": (
                headcount.source_document_id
            ),
            "source_document_date": (
                headcount.source_document_date
            ),
        }

    finally:
        session.close()