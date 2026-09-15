from sqlalchemy import and_, func, or_, select

from app.database.postgres import get_session
from app.models.erknm import ErknmInspection
from app.models.source import DataSet
from app.services.check_result import build_check_result


DATASET_CODE = "erknm_inspections"
SOURCE_CODE = "erknm_inspections"


def _clean_digits(value, lengths):
    text = "".join(
        character
        for character in str(value or "")
        if character.isdigit()
    )

    if len(text) not in lengths:
        return None

    return text


def _serialize_record(row):
    return {
        "erpid": row.erpid,
        "data_date": row.data_date,
        "period_year": row.period_year,
        "period_month": row.period_month,
        "classification": row.classification,
        "status": row.status,
        "status_key": row.status_key,
        "supervision_name": row.supervision_name,
        "kind_control": row.kind_control,
        "kind_knm": row.kind_knm,
        "kno_organization": row.kno_organization,
        "prosecutor_office": row.prosecutor_office,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "subject_inn": row.subject_inn,
        "subject_ogrn": row.subject_ogrn,
        "subject_name": row.subject_name,
        "subject_type": row.subject_type,
        "place": row.place,
        "object_address": row.object_address,
        "object_type": row.object_type,
        "object_kind": row.object_kind,
        "risk_category": row.risk_category,
        "reason_text": row.reason_text,
        "warning_caption": row.warning_caption,
        "result_text": row.result_text,
        "has_warning": bool(row.warning_caption),
        "has_result": bool(row.result_text),
    }


def _matching_condition(clean_inn, clean_ogrn):
    """
    Основной ключ — точный ИНН.

    ОГРН используется только как fallback для записей ЕРКНМ,
    где ИНН отсутствует. Это не позволяет записи с противоречащим
    ИНН привязаться к компании только по ОГРН.
    """

    conditions = []

    if clean_inn:
        conditions.append(
            ErknmInspection.subject_inn == clean_inn
        )

    if clean_ogrn:
        conditions.append(
            and_(
                ErknmInspection.subject_inn.is_(None),
                ErknmInspection.subject_ogrn == clean_ogrn,
            )
        )

    if not conditions:
        return None

    return or_(*conditions)


def get_erknm_check_for_company(
    inn: str,
    ogrn: str | None = None,
    limit: int = 30,
):
    clean_inn = _clean_digits(inn, {10, 12})
    clean_ogrn = _clean_digits(ogrn, {13, 15})

    matching_condition = _matching_condition(
        clean_inn,
        clean_ogrn,
    )

    if matching_condition is None:
        return build_check_result(
            checked=False,
            applicable=True,
            result="unavailable",
            data_date=None,
            dataset_code=DATASET_CODE,
            source=SOURCE_CODE,
            reason=(
                "Не удалось определить корректный ИНН/ОГРН компании."
            ),
            has_records=False,
            record_count=0,
            records=[],
            matching_method="inn_exact_ogrn_fallback_exact",
        )

    if limit < 1:
        raise ValueError("limit должен быть больше нуля")

    session = get_session()

    try:
        dataset = (
            session.execute(
                select(DataSet)
                .where(DataSet.code == DATASET_CODE)
                .limit(1)
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
                reason="Dataset ЕРКНМ не зарегистрирован.",
                has_records=False,
                record_count=0,
                records=[],
                matching_method="inn_exact_ogrn_fallback_exact",
            )

        data_date = (
            session.execute(
                select(func.max(ErknmInspection.data_date))
                .where(ErknmInspection.dataset_id == dataset.id)
            )
            .scalar_one_or_none()
        )

        if data_date is None:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=None,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="Данные ЕРКНМ ещё не загружены.",
                has_records=False,
                record_count=0,
                records=[],
                matching_method="inn_exact_ogrn_fallback_exact",
            )

        periods = (
            session.execute(
                select(
                    ErknmInspection.period_year,
                    ErknmInspection.period_month,
                )
                .where(
                    ErknmInspection.dataset_id == dataset.id
                )
                .distinct()
                .order_by(
                    ErknmInspection.period_year,
                    ErknmInspection.period_month,
                )
            )
            .all()
        )

        coverage_periods = [
            f"{year:04d}-{month:02d}"
            for year, month in periods
        ]

        common_filters = (
            ErknmInspection.dataset_id == dataset.id,
            matching_condition,
        )

        total_count = (
            session.execute(
                select(func.count())
                .select_from(ErknmInspection)
                .where(*common_filters)
            )
            .scalar_one()
        )

        rows = (
            session.execute(
                select(ErknmInspection)
                .where(*common_filters)
                .order_by(
                    ErknmInspection.start_date.desc().nullslast(),
                    ErknmInspection.id.desc(),
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )

        summary_rows = (
            session.execute(
                select(
                    ErknmInspection.status,
                    ErknmInspection.kind_knm,
                    func.count(),
                )
                .where(*common_filters)
                .group_by(
                    ErknmInspection.status,
                    ErknmInspection.kind_knm,
                )
            )
            .all()
        )

        result_count = (
            session.execute(
                select(func.count())
                .select_from(ErknmInspection)
                .where(
                    *common_filters,
                    ErknmInspection.result_text.is_not(None),
                )
            )
            .scalar_one()
        )

        warning_count = (
            session.execute(
                select(func.count())
                .select_from(ErknmInspection)
                .where(
                    *common_filters,
                    ErknmInspection.warning_caption.is_not(None),
                )
            )
            .scalar_one()
        )

        status_counts = {}
        kind_counts = {}

        for status, kind_knm, count in summary_rows:
            status_key = status or "Не указан"
            kind_key = kind_knm or "Не указан"
            status_counts[status_key] = (
                status_counts.get(status_key, 0) + count
            )
            kind_counts[kind_key] = (
                kind_counts.get(kind_key, 0) + count
            )

        records = [
            _serialize_record(row)
            for row in rows
        ]

        common = {
            "has_records": bool(total_count),
            "record_count": total_count,
            "records": records,
            "records_limited": total_count > len(records),
            "result_count": result_count,
            "warning_count": warning_count,
            "status_counts": status_counts,
            "kind_counts": kind_counts,
            "coverage_periods": coverage_periods,
            "matching_method": "inn_exact_ogrn_fallback_exact",
            "coverage_note": (
                "Отсутствие записи означает только отсутствие совпадения "
                "в уже загруженных периодах ЕРКНМ."
            ),
            "interpretation_note": (
                "Сам факт контрольного или профилактического мероприятия "
                "не является негативным сигналом. Оценивать нужно вид, "
                "статус, результат, нарушения и контекст мероприятия."
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
