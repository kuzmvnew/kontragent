from datetime import date, datetime

from sqlalchemy import func, select

from app.database.postgres import get_session
from app.models.disqualified_person import (
    DisqualifiedPersonSnapshot,
)
from app.models.source import DataSet
from app.services.check_result import build_check_result
from app.services.data_readiness_service import clean_negative_blocker


DATASET_CODE = "fns_disqualified"
SOURCE_CODE = "fns_disqualified"


def _is_active_on_date(
    start_date: date | None,
    end_date: date | None,
    on_date: date,
) -> bool:
    if start_date is not None and start_date > on_date:
        return False

    if end_date is not None and end_date < on_date:
        return False

    return True


def _get_dataset_data_date(
    session,
    dataset,
):
    if dataset is None:
        return None

    if dataset.last_data_date is not None:
        return dataset.last_data_date

    return (
        session.execute(
            select(
                func.max(
                    DisqualifiedPersonSnapshot.data_date
                )
            )
            .where(
                DisqualifiedPersonSnapshot.dataset_id
                == dataset.id
            )
        )
        .scalar_one_or_none()
    )


def _serialize_record(
    row,
    data_date: date,
):
    return {
        "register_number": row.register_number,
        "full_name": row.full_name,
        "organization_name": row.organization_name,
        "organization_inn": row.organization_inn,
        "position": row.position,
        "offence_article": row.offence_article,
        "disqualification_term": row.disqualification_term,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "active_on_data_date": _is_active_on_date(
            row.start_date,
            row.end_date,
            data_date,
        ),
        "matching_state": "organization_inn_exact",
        "name_only_matching_used": False,
    }


def get_disqualified_check_for_inn(
    inn: str,
    limit: int = 50,
    *,
    now: datetime | None = None,
):
    """
    Проверяет только точное совпадение по ИНН организации.

    Важно: organization_inn в реестре ФНС — организация,
    связанная с записью о правонарушении. Совпадение не означает,
    что дисквалифицированное лицо является текущим руководителем.
    """

    clean_inn = str(inn or "").strip()

    if len(clean_inn) == 12 and clean_inn.isdigit():
        return build_check_result(
            checked=True,
            applicable=False,
            result="not_applicable",
            data_date=None,
            dataset_code=DATASET_CODE,
            source=SOURCE_CODE,
            reason="legal_entities_only",
            has_records=False,
            record_count=0,
            active_record_count=0,
            records=[],
            matching_method="organization_inn_exact",
            matching_state="not_applicable_entity_type",
            name_only_matching_used=False,
        )

    if len(clean_inn) != 10 or not clean_inn.isdigit():
        return build_check_result(
            checked=False,
            applicable=True,
            result="unavailable",
            data_date=None,
            dataset_code=DATASET_CODE,
            source=SOURCE_CODE,
            reason="Не удалось определить корректный ИНН компании.",
            has_records=False,
            record_count=0,
            active_record_count=0,
            records=[],
            matching_method="organization_inn_exact",
            matching_state="invalid_organization_inn",
            name_only_matching_used=False,
        )

    session = get_session()

    try:
        dataset = (
            session.execute(
                select(DataSet)
                .where(
                    DataSet.code
                    == DATASET_CODE
                )
                .limit(1)
            )
            .scalar_one_or_none()
        )

        data_date = _get_dataset_data_date(
            session=session,
            dataset=dataset,
        )

        if dataset is None or data_date is None:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=None,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason=(
                    "Актуальный snapshot реестра "
                    "дисквалифицированных лиц не загружен."
                ),
                has_records=False,
                record_count=0,
                active_record_count=0,
                records=[],
                matching_method="organization_inn_exact",
                matching_state="dataset_unavailable",
                name_only_matching_used=False,
            )

        if not dataset.enabled:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=data_date,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="dataset_disabled",
                has_records=False,
                record_count=0,
                active_record_count=0,
                records=[],
                matching_method="organization_inn_exact",
                matching_state="dataset_unavailable",
                name_only_matching_used=False,
            )

        blocker = clean_negative_blocker(dataset, now=now)
        if blocker is not None:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=data_date,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason=blocker,
                has_records=False,
                record_count=0,
                active_record_count=0,
                records=[],
                matching_method="organization_inn_exact",
                matching_state="dataset_unavailable",
                name_only_matching_used=False,
            )

        base_query = (
            select(DisqualifiedPersonSnapshot)
            .where(
                DisqualifiedPersonSnapshot.dataset_id
                == dataset.id,
                DisqualifiedPersonSnapshot.data_date
                == data_date,
                DisqualifiedPersonSnapshot.organization_inn
                == clean_inn,
            )
        )

        rows = (
            session.execute(
                base_query
                .order_by(
                    DisqualifiedPersonSnapshot.end_date.desc(),
                    DisqualifiedPersonSnapshot.start_date.desc(),
                    DisqualifiedPersonSnapshot.id.desc(),
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )

        total_count = (
            session.execute(
                select(func.count())
                .select_from(DisqualifiedPersonSnapshot)
                .where(
                    DisqualifiedPersonSnapshot.dataset_id
                    == dataset.id,
                    DisqualifiedPersonSnapshot.data_date
                    == data_date,
                    DisqualifiedPersonSnapshot.organization_inn
                    == clean_inn,
                )
            )
            .scalar_one()
        )

        records = [
            _serialize_record(
                row=row,
                data_date=data_date,
            )
            for row in rows
        ]

        active_record_count = sum(
            1
            for record in records
            if record["active_on_data_date"]
        )

        common = {
            "has_records": bool(total_count),
            "record_count": total_count,
            "active_record_count": active_record_count,
            "records": records,
            "records_limited": total_count > len(records),
            "matching_method": "organization_inn_exact",
            "matching_state": (
                "organization_inn_exact"
                if total_count
                else "organization_inn_exact_not_found"
            ),
            "name_only_matching_used": False,
            "coverage_note": (
                "В части записей реестра ФНС ИНН организации "
                "не заполнен. Отсутствие совпадения по ИНН не "
                "подтверждает отсутствие дисквалифицированных лиц "
                "среди текущего руководства."
            ),
            "relationship_note": (
                "Совпадение по ИНН означает только связь записи "
                "ФНС с этой организацией и не доказывает, что лицо "
                "является её текущим руководителем."
            ),
        }

        if total_count:
            return build_check_result(
                checked=True,
                applicable=True,
                result="found",
                data_date=data_date,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason=None,
                **common,
            )

        return build_check_result(
            checked=True,
            applicable=True,
            result="not_found",
            data_date=data_date,
            dataset_code=DATASET_CODE,
            source=SOURCE_CODE,
            reason=None,
            **common,
        )

    finally:
        session.close()
