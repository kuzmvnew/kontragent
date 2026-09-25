from datetime import datetime

from sqlalchemy import select

from app.database.postgres import get_session
from app.models.company import Company
from app.models.msp import CompanyMspProfile
from app.models.source import DataSet
from app.services.check_result import build_check_result
from app.services.data_readiness_service import clean_negative_blocker


DATASET_CODE = "fns_msp"
SOURCE_CODE = DATASET_CODE


MSP_CATEGORY_NAMES = {
    "1": "Микропредприятие",
    "2": "Малое предприятие",
    "3": "Среднее предприятие",
}


def get_msp_category_name(category_code):
    if category_code is None:
        return None

    code = str(category_code).strip()

    return (
        MSP_CATEGORY_NAMES.get(code)
        or f"Код {code}"
    )


def _empty_payload():
    return {
        "inclusion_date": None,
        "subject_type_code": None,
        "category_code": None,
        "category_name": None,
        "is_new_code": None,
        "social_enterprise_code": None,
        "employee_count": None,
        "source_document_id": None,
    }


def get_msp_check_for_company(
    company_id: int,
    *,
    now: datetime | None = None,
):
    """Return a freshness-aware MSP registry check for a legal entity or IP."""

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
        has_supported_type = (
            entity_type == "legal" and len(inn) == 10
        ) or (
            entity_type == "individual_entrepreneur" and len(inn) == 12
        ) or (
            entity_type is None and len(inn) in {10, 12}
        )
        if (
            not has_supported_type
            or len(inn) not in {10, 12}
            or not inn.isdigit()
        ):
            return build_check_result(
                checked=True,
                applicable=False,
                result="not_applicable",
                data_date=None,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="legal_or_individual_entrepreneur_only",
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

        profile = session.scalar(
            select(CompanyMspProfile)
            .where(
                CompanyMspProfile.company_id == company_id,
                CompanyMspProfile.dataset_id == dataset.id,
            )
            .order_by(
                CompanyMspProfile.data_date.desc(),
                CompanyMspProfile.id.desc(),
            )
            .limit(1)
        )
        if profile is None:
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
                    "в текущем официальном реестре МСП."
                ),
                **_empty_payload(),
            )

        return build_check_result(
            checked=True,
            applicable=True,
            result="found",
            data_date=profile.data_date,
            dataset_code=dataset.code,
            source=SOURCE_CODE,
            reason=None,
            source_data_date=dataset.last_data_date,
            dataset_id=dataset.id,
            priority=dataset.priority,
            inclusion_date=profile.inclusion_date,
            subject_type_code=profile.subject_type_code,
            category_code=profile.category_code,
            category_name=get_msp_category_name(profile.category_code),
            is_new_code=profile.is_new_code,
            social_enterprise_code=profile.social_enterprise_code,
            employee_count=profile.employee_count,
            source_document_id=profile.source_document_id,
        )
    finally:
        session.close()


def get_msp_profile_for_company(
    company_id: int,
    *,
    now: datetime | None = None,
):
    """
    Возвращает актуальный профиль компании
    из Единого реестра субъектов МСП ФНС.

    Если профиля нет — возвращает None.
    """

    check = get_msp_check_for_company(company_id, now=now)
    if check["result"] != "found":
        return None
    return {
        key: check[key]
        for key in (
            "dataset_id",
            "dataset_code",
            "priority",
            "data_date",
            "inclusion_date",
            "subject_type_code",
            "category_code",
            "category_name",
            "is_new_code",
            "social_enterprise_code",
            "employee_count",
            "source_document_id",
        )
    }
