from datetime import datetime

from sqlalchemy import select

from app.database.postgres import get_session
from app.models.company import Company
from app.models.headcount import CompanyHeadcount
from app.models.source import DataSet
from app.services.check_result import build_check_result
from app.services.data_readiness_service import clean_negative_blocker


DATASET_CODE = "fns_headcount"
SOURCE_CODE = DATASET_CODE


def _empty_payload():
    return {
        "employee_count": None,
        "year": None,
        "source_document_id": None,
        "source_document_date": None,
    }


def get_headcount_check_for_company(
    company_id: int,
    *,
    now: datetime | None = None,
):
    """Return a freshness-aware, exact-INN headcount check contract."""

    session = get_session()
    try:
        company = session.execute(
            select(Company.id, Company.inn, Company.entity_type).where(
                Company.id == company_id
            )
        ).mappings().one_or_none()
        if company is None:
            return build_check_result(
                checked=False,
                applicable=None,
                result="unavailable",
                data_date=None,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="company_not_found",
                source_data_date=None,
                **_empty_payload(),
            )

        inn = str(company["inn"] or "").strip()
        entity_type = company["entity_type"]
        is_legal = entity_type == "legal" or (
            entity_type is None and len(inn) == 10
        )
        if not is_legal or len(inn) != 10 or not inn.isdigit():
            return build_check_result(
                checked=True,
                applicable=False,
                result="not_applicable",
                data_date=None,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="legal_entities_only",
                source_data_date=None,
                **_empty_payload(),
            )

        dataset = session.scalar(
            select(DataSet).where(DataSet.code == DATASET_CODE)
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
                source_data_date=None,
                **_empty_payload(),
            )

        blocker = clean_negative_blocker(dataset, now=now)
        if blocker is not None:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=dataset.last_data_date,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason=blocker,
                source_data_date=dataset.last_data_date,
                **_empty_payload(),
            )

        headcount = session.scalar(
            select(CompanyHeadcount)
            .where(
                CompanyHeadcount.company_id == company_id,
                CompanyHeadcount.dataset_id == dataset.id,
            )
            .order_by(
                CompanyHeadcount.year.desc(),
                CompanyHeadcount.id.desc(),
            )
            .limit(1)
        )
        if headcount is None:
            return build_check_result(
                checked=True,
                applicable=True,
                result="not_found",
                data_date=dataset.last_data_date,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason=None,
                source_data_date=dataset.last_data_date,
                limitation=(
                    "Отсутствие означает только, что ИНН не найден "
                    "в текущем официальном наборе."
                ),
                **_empty_payload(),
            )

        return build_check_result(
            checked=True,
            applicable=True,
            result="found",
            data_date=dataset.last_data_date,
            dataset_code=dataset.code,
            source=SOURCE_CODE,
            reason=None,
            source_data_date=dataset.last_data_date,
            employee_count=headcount.employee_count,
            year=headcount.year,
            dataset_id=dataset.id,
            priority=dataset.priority,
            source_document_id=headcount.source_document_id,
            source_document_date=headcount.source_document_date,
        )
    finally:
        session.close()


def get_latest_headcount_for_company(
    company_id: int,
    *,
    now: datetime | None = None,
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

    check = get_headcount_check_for_company(company_id, now=now)
    if check["result"] != "found":
        return None
    return {
        key: check[key]
        for key in (
            "employee_count",
            "year",
            "dataset_id",
            "dataset_code",
            "priority",
            "source_document_id",
            "source_document_date",
        )
    }
