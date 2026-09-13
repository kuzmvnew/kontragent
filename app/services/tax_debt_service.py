from sqlalchemy import select

from app.database.postgres import get_session
from app.models.tax_debt import (
    CompanyTaxDebtItem,
    CompanyTaxDebtSnapshot,
)


def get_latest_tax_debt_for_company(
    company_id: int,
    include_items: bool = True,
):
    """
    Возвращает последний доступный
    снимок налоговой задолженности компании.

    История при этом остаётся в БД.
    """

    session = get_session()

    try:
        snapshot = (
            session.execute(
                select(
                    CompanyTaxDebtSnapshot
                )
                .where(
                    CompanyTaxDebtSnapshot.company_id
                    == company_id
                )
                .order_by(
                    CompanyTaxDebtSnapshot.data_date.desc(),
                    CompanyTaxDebtSnapshot.id.desc(),
                )
                .limit(1)
            )
            .scalar_one_or_none()
        )

        if snapshot is None:
            return None

        items = []

        if include_items:
            item_rows = (
                session.execute(
                    select(
                        CompanyTaxDebtItem
                    )
                    .where(
                        CompanyTaxDebtItem.snapshot_id
                        == snapshot.id
                    )
                    .order_by(
                        CompanyTaxDebtItem.total.desc(),
                        CompanyTaxDebtItem.tax_name,
                    )
                )
                .scalars()
                .all()
            )

            items = [
                {
                    "tax_name": item.tax_name,
                    "arrears": item.arrears,
                    "penalties": item.penalties,
                    "fines": item.fines,
                    "total": item.total,
                }
                for item in item_rows
            ]

        return {
            "snapshot_id": snapshot.id,
            "data_date": snapshot.data_date,
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
                snapshot.total_debt
            ),
            "item_count": (
                snapshot.item_count
            ),
            "items": items,
            "source": "fns_tax_debt",
        }

    finally:
        session.close()


def get_tax_debt_history(
    company_id: int,
    limit: int = 24,
):
    """
    Возвращает историю задолженности
    от нового среза к старому.
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
                    == company_id
                )
                .order_by(
                    CompanyTaxDebtSnapshot.data_date.desc(),
                    CompanyTaxDebtSnapshot.id.desc(),
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )

        return [
            {
                "snapshot_id": row.id,
                "data_date": row.data_date,
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
            }
            for row in rows
        ]

    finally:
        session.close()