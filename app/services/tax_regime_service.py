from sqlalchemy import select

from app.database.postgres import get_session
from app.models.source import DataSet
from app.models.tax_regime import (
    CompanyTaxRegimeSnapshot,
)


REGIME_NAMES = {
    "usn": "Упрощённая система налогообложения (УСН)",
    "ausn": (
        "Автоматизированная упрощённая "
        "система налогообложения (АУСН)"
    ),
    "eshn": (
        "Единый сельскохозяйственный налог (ЕСХН)"
    ),
    "psn": "Патентная система налогообложения (ПСН)",
    "npd": "Налог на профессиональный доход (НПД)",
    "srp": (
        "Система налогообложения при выполнении "
        "соглашения о разделе продукции (СРП)"
    ),
}


def get_regime_name(
    regime_code,
):
    if regime_code is None:
        return None

    return REGIME_NAMES.get(
        str(regime_code).strip()
    )


def get_tax_regime_profile_for_company(
    company_id,
):
    """
    Возвращает последний доступный snapshot
    специальных налоговых режимов компании.
    """

    session = get_session()

    try:
        row = (
            session.execute(
                select(
                    CompanyTaxRegimeSnapshot,
                    DataSet.code,
                )
                .join(
                    DataSet,
                    DataSet.id
                    == CompanyTaxRegimeSnapshot.dataset_id,
                )
                .where(
                    CompanyTaxRegimeSnapshot.company_id
                    == company_id
                )
                .order_by(
                    CompanyTaxRegimeSnapshot.data_date.desc(),
                    CompanyTaxRegimeSnapshot.id.desc(),
                )
                .limit(1)
            )
            .first()
        )

        if row is None:
            return None

        snapshot = row[0]
        dataset_code = row[1]

        regime_codes = list(
            snapshot.regime_codes or []
        )

        regimes = []

        for code in regime_codes:
            regimes.append(
                {
                    "code": code,
                    "name": (
                        get_regime_name(code)
                        or code
                    ),
                }
            )

        return {
            "company_id": snapshot.company_id,
            "entity_type": snapshot.entity_type,
            "data_date": snapshot.data_date,
            "regime_codes": regime_codes,
            "regimes": regimes,
            "dataset_id": snapshot.dataset_id,
            "dataset_code": dataset_code,
            "source_document_id": (
                snapshot.source_document_id
            ),
            "source_document_date": (
                snapshot.source_document_date
            ),
        }

    finally:
        session.close()
