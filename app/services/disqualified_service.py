from datetime import date, datetime

from sqlalchemy import func, select

from app.database.postgres import get_session
from app.models.company import Company, CompanyManager
from app.models.disqualified_person import DisqualifiedPersonSnapshot
from app.models.source import DataSet
from app.services.check_result import build_check_result
from app.services.data_readiness_service import clean_negative_blocker


DATASET_CODE = "fns_disqualified"
SOURCE_CODE = "fns_disqualified"
MATCHING_METHOD = "current_manager_full_name_exact_and_company_inn"


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
    """Return only the evidence needed to support the current-manager match."""

    return {
        "register_number": row.register_number,
        "full_name": row.full_name,
        "organization_inn": row.organization_inn,
        "position": row.position,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "active_on_data_date": _is_active_on_date(
            row.start_date,
            row.end_date,
            data_date,
        ),
        "matching_state": "MATCHED",
        "name_only_matching_used": False,
    }


def _normalize_name(value: str | None) -> str:
    """Normalize a public full name without introducing fuzzy matching."""

    return " ".join(str(value or "").upper().split())


def _base_evidence(*, matching_state: str, reason: str | None = None) -> dict:
    return {
        "reason": reason,
        "has_records": False,
        "record_count": 0,
        "active_record_count": 0,
        "records": [],
        "records_limited": False,
        "matching_method": MATCHING_METHOD,
        "matching_state": matching_state,
        "name_only_matching_used": False,
    }


def get_disqualified_check_for_inn(
    inn: str,
    limit: int = 50,
    *,
    now: datetime | None = None,
):
    """
    Проверяет только двойное точное совпадение:
    ИНН организации + нормализованное ФИО текущего руководителя.

    Запись, связанную с организацией только по ИНН, нельзя проецировать
    в карточку или Risk как факт о текущем руководстве.
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
            matching_method=MATCHING_METHOD,
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
            matching_method=MATCHING_METHOD,
            matching_state="UNAVAILABLE",
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
                matching_method=MATCHING_METHOD,
                matching_state="UNAVAILABLE",
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
                matching_method=MATCHING_METHOD,
                matching_state="UNAVAILABLE",
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
                matching_method=MATCHING_METHOD,
                matching_state=("STALE" if blocker == "dataset_stale" else "UNAVAILABLE"),
                name_only_matching_used=False,
            )

        company = session.scalar(
            select(Company).where(Company.inn == clean_inn).limit(1)
        )
        if company is None:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=data_date,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                **_base_evidence(
                    matching_state="UNAVAILABLE",
                    reason="company_identity_unavailable",
                ),
            )

        manager_names = {
            normalized
            for full_name in session.scalars(
                select(CompanyManager.full_name).where(
                    CompanyManager.company_id == company.id,
                    CompanyManager.is_current.is_(True),
                )
            )
            if (normalized := _normalize_name(full_name))
        }
        if not manager_names:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=data_date,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                **_base_evidence(
                    matching_state="UNAVAILABLE",
                    reason="current_manager_identity_unavailable",
                ),
            )
        if len(manager_names) != 1:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=data_date,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                **_base_evidence(
                    matching_state="AMBIGUOUS",
                    reason="current_manager_identity_ambiguous",
                ),
            )

        current_manager_name = next(iter(manager_names))

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
            )
            .scalars()
            .all()
        )

        active_rows = [
            row
            for row in rows
            if _is_active_on_date(row.start_date, row.end_date, data_date)
        ]
        matched_rows = [
            row
            for row in active_rows
            if _normalize_name(row.full_name) == current_manager_name
        ]

        records = [
            _serialize_record(
                row=row,
                data_date=data_date,
            )
            for row in matched_rows[:limit]
        ]

        common = {
            "has_records": bool(matched_rows),
            "record_count": len(matched_rows),
            "active_record_count": len(matched_rows),
            "records": records,
            "records_limited": len(matched_rows) > len(records),
            "matching_method": MATCHING_METHOD,
            "matching_state": "MATCHED" if matched_rows else "NOT_FOUND",
            "name_only_matching_used": False,
            "coverage_note": (
                "Проверяются только записи с точным ИНН организации "
                "и точным нормализованным ФИО текущего руководителя."
            ),
            "relationship_note": (
                "Положительный результат требует одновременно точного "
                "ИНН, точного ФИО текущего руководителя и действующего "
                "на дату данных периода дисквалификации."
            ),
        }

        if matched_rows:
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

        if active_rows:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=data_date,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="organization_record_conflicts_with_current_manager",
                **{
                    **common,
                    "matching_state": "CONFLICTING",
                    "organization_candidate_count": len(active_rows),
                },
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
