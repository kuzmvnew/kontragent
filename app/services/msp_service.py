from sqlalchemy import select

from app.database.postgres import get_session
from app.models.msp import CompanyMspProfile
from app.models.source import DataSet


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


def get_msp_profile_for_company(
    company_id: int,
):
    """
    Возвращает актуальный профиль компании
    из Единого реестра субъектов МСП ФНС.

    Если профиля нет — возвращает None.
    """

    session = get_session()

    try:
        statement = (
            select(
                CompanyMspProfile,
                DataSet,
            )
            .join(
                DataSet,
                CompanyMspProfile.dataset_id
                == DataSet.id,
            )
            .where(
                CompanyMspProfile.company_id
                == company_id
            )
            .order_by(
                DataSet.priority.asc(),
                CompanyMspProfile.data_date.desc(),
                CompanyMspProfile.id.desc(),
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

        profile, dataset = row

        return {
            "dataset_id": dataset.id,
            "dataset_code": dataset.code,
            "priority": dataset.priority,

            "data_date": profile.data_date,
            "inclusion_date": (
                profile.inclusion_date
            ),

            "subject_type_code": (
                profile.subject_type_code
            ),

            "category_code": (
                profile.category_code
            ),

            "category_name": (
                get_msp_category_name(
                    profile.category_code
                )
            ),

            "is_new_code": (
                profile.is_new_code
            ),

            "social_enterprise_code": (
                profile.social_enterprise_code
            ),

            "employee_count": (
                profile.employee_count
            ),

            "source_document_id": (
                profile.source_document_id
            ),
        }

    finally:
        session.close()
