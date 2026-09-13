from sqlalchemy import (
    func,
    select,
)

from app.database.postgres import get_session
from app.models.tax_offence import CompanyTaxOffence


def get_latest_tax_offence_for_company(
    company_id: int,
):
    """
    Возвращает последний доступный срез
    налоговых правонарушений компании.

    Если на одну дату существует несколько
    документов ФНС, их штрафы суммируются.
    """

    session = get_session()

    try:
        latest_date = (
            session.execute(
                select(
                    func.max(
                        CompanyTaxOffence.data_date
                    )
                )
                .where(
                    CompanyTaxOffence.company_id
                    == company_id
                )
            )
            .scalar_one_or_none()
        )

        if latest_date is None:
            return None

        rows = (
            session.execute(
                select(
                    CompanyTaxOffence
                )
                .where(
                    CompanyTaxOffence.company_id
                    == company_id,
                    CompanyTaxOffence.data_date
                    == latest_date,
                )
                .order_by(
                    CompanyTaxOffence.fine_amount.desc(),
                    CompanyTaxOffence.id,
                )
            )
            .scalars()
            .all()
        )

        if not rows:
            return None

        total_fine = sum(
            (
                row.fine_amount
                for row in rows
            ),
            0,
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
            "data_date": latest_date,
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
            "has_offence": True,
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
    История официальных документов ФНС.

    Позже, когда появятся новые годовые
    наборы, здесь автоматически появятся
    дополнительные периоды.
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