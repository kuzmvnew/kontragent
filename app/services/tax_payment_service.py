from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.database.postgres import get_session
from app.models.company import Company
from app.models.source import DataSet
from app.models.tax_payment import (
    CompanyTaxPaymentSnapshot,
)
from app.services.check_result import (
    build_check_result,
)


DATASET_CODE = "fns_tax_paid"
SOURCE_CODE = "fns_tax_paid"

ZERO = Decimal("0.00")


def _get_dataset_data_date(
    session,
    dataset,
):
    """
    Возвращает дату актуального загруженного
    набора PAYTAX.

    Основной источник:
        DataSet.last_data_date

    Резерв:
        максимальная фактическая data_date
        в таблице snapshots.
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
                    CompanyTaxPaymentSnapshot.data_date
                )
            )
            .where(
                CompanyTaxPaymentSnapshot.dataset_id
                == dataset.id
            )
        )
        .scalar_one_or_none()
    )


def _empty_payment_payload():
    return {
        "has_data": False,
        "data_year": None,
        "document_date": None,
        "source_document_id": None,
        "source_company_name": None,
        "total_amount": ZERO,
        "tax_amount": ZERO,
        "insurance_amount": ZERO,
        "penalty_amount": ZERO,
        "non_tax_amount": ZERO,
        "other_amount": ZERO,
        "source_item_count": 0,
        "stored_item_count": 0,
        "items": [],
    }


def _item_to_dict(
    item,
):
    """
    Преобразует одну платежную позицию
    в простой словарь.
    """

    return {
        "tax_name": item.tax_name,
        "payment_type": item.payment_type,
        "amount": item.amount,
    }


def _snapshot_to_dict(
    snapshot,
):
    """
    Преобразует PAYTAX snapshot
    в единый check-result для Aggregator/UI.
    """

    items = sorted(
        (
            _item_to_dict(item)
            for item in snapshot.items
        ),
        key=lambda item: item["amount"],
        reverse=True,
    )

    return build_check_result(
        checked=True,
        applicable=True,
        result="found",
        data_date=snapshot.data_date,
        dataset_code=DATASET_CODE,
        source=SOURCE_CODE,
        reason=None,
        has_data=True,
        data_year=snapshot.data_year,
        document_date=snapshot.document_date,
        source_document_id=snapshot.source_document_id,
        source_company_name=snapshot.source_company_name,
        total_amount=snapshot.total_amount,
        tax_amount=snapshot.tax_amount,
        insurance_amount=snapshot.insurance_amount,
        penalty_amount=snapshot.penalty_amount,
        non_tax_amount=snapshot.non_tax_amount,
        other_amount=snapshot.other_amount,
        source_item_count=snapshot.source_item_count,
        stored_item_count=snapshot.stored_item_count,
        items=items,
    )


def get_latest_tax_payment_for_company(
    company_id: int,
):
    """
    Возвращает PAYTAX в едином контракте.

    result:
        found
        not_found
        not_applicable
        unavailable

    ВАЖНО:

    not_found не означает, что организация
    не платила налоги. Это означает только,
    что в актуальном опубликованном наборе
    PAYTAX запись не найдена.
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
                    Company.id == company_id
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
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="company_not_found",
                **_empty_payment_payload(),
            )

        inn = str(
            company["inn"] or ""
        ).strip()

        # Текущий PAYTAX содержит ИННЮЛ.
        if (
            len(inn) != 10
            or not inn.isdigit()
        ):
            return build_check_result(
                checked=True,
                applicable=False,
                result="not_applicable",
                data_date=None,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="legal_entities_only",
                **_empty_payment_payload(),
            )

        dataset = (
            session.execute(
                select(DataSet)
                .where(
                    DataSet.code == DATASET_CODE
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
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="dataset_not_registered",
                **_empty_payment_payload(),
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
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="dataset_not_loaded",
                **_empty_payment_payload(),
            )

        snapshot = (
            session.execute(
                select(
                    CompanyTaxPaymentSnapshot
                )
                .options(
                    selectinload(
                        CompanyTaxPaymentSnapshot.items
                    )
                )
                .where(
                    CompanyTaxPaymentSnapshot.company_id
                    == company_id,
                    CompanyTaxPaymentSnapshot.dataset_id
                    == dataset.id,
                    CompanyTaxPaymentSnapshot.data_date
                    == dataset_data_date,
                )
            )
            .scalar_one_or_none()
        )

        if snapshot is None:
            empty = _empty_payment_payload()
            empty["data_year"] = dataset_data_date.year

            return build_check_result(
                checked=True,
                applicable=True,
                result="not_found",
                data_date=dataset_data_date,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason=None,
                **empty,
            )

        return _snapshot_to_dict(snapshot)

    finally:
        session.close()


def get_tax_payment_history(
    company_id: int,
    limit: int = 10,
):
    """
    Возвращает историю PAYTAX компании
    по годам.
    """

    session = get_session()

    try:
        snapshots = (
            session.execute(
                select(
                    CompanyTaxPaymentSnapshot
                )
                .options(
                    selectinload(
                        CompanyTaxPaymentSnapshot.items
                    )
                )
                .where(
                    CompanyTaxPaymentSnapshot.company_id
                    == company_id
                )
                .order_by(
                    CompanyTaxPaymentSnapshot.data_date.desc(),
                    CompanyTaxPaymentSnapshot.id.desc(),
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )

        return [
            _snapshot_to_dict(snapshot)
            for snapshot in snapshots
        ]

    finally:
        session.close()
