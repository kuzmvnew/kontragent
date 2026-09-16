from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.ingestion.cbr_warning_list import normalize_inn as strict_normalize_inn

from app.database.postgres import get_session
from app.models.cbr_warning_list import CbrWarningListEntry
from app.models.source import DataSet
from app.services.check_result import build_check_result


DATASET_CODE = "cbr_warning_list"
SOURCE_CODE = "cbr_warning_list"
DETAIL_URL_TEMPLATE = (
    "https://www.cbr.ru/inside/warning-list/detail/?id={cbr_id}"
)


def normalize_inn(value) -> str:
    return strict_normalize_inn(value) or ""


def _serialize_named_list(values) -> list[str]:
    result = []

    for item in values or []:
        if isinstance(item, dict):
            value = item.get("name") or item.get("id")
        else:
            value = item

        if value is None:
            continue

        text = str(value).strip()
        if text and text not in result:
            result.append(text)

    return result


def _serialize_entry(row) -> dict:
    detail_url = None

    if row.cbr_id and not str(row.cbr_id).startswith("sha256:"):
        detail_url = DETAIL_URL_TEMPLATE.format(
            cbr_id=row.cbr_id
        )

    return {
        "cbr_id": row.cbr_id,
        "name": row.name,
        "inn": row.inn,
        "entry_date": row.entry_date,
        "update_date": row.update_date,
        "address": row.address,
        "sites": list(row.sites or []),
        "signs": _serialize_named_list(row.signs),
        "regions": _serialize_named_list(row.regions),
        "additional_info": row.additional_info,
        "liquidation_status": row.liquidation_status,
        "comment": row.comment,
        "org_type": row.org_type,
        "detail_url": detail_url,
    }


def _empty_payload():
    return {
        "is_listed": None,
        "record_count": None,
        "records": [],
        "matching_method": "inn_exact",
        "interpretation_note": (
            "Включение в предупредительный список означает, что "
            "Банк России выявил признаки нелегальной деятельности "
            "на финансовом рынке. Это не является судебным приговором; "
            "организация или ИП вправе обжаловать включение."
        ),
        "coverage_note": (
            "Отсутствие записи в этом списке не подтверждает наличие "
            "лицензии Банка России и само по себе не доказывает законность "
            "всей финансовой деятельности компании или ИП. Записи без ИНН "
            "не могут быть сопоставлены с этой карточкой по точному ИНН."
        ),
    }


def get_cbr_warning_list_check_for_inn(
    inn: str,
    limit: int = 20,
):
    clean_inn = normalize_inn(inn)

    if len(clean_inn) not in {10, 12}:
        return build_check_result(
            checked=False,
            applicable=True,
            result="unavailable",
            data_date=None,
            dataset_code=DATASET_CODE,
            source=SOURCE_CODE,
            reason="invalid_inn",
            **_empty_payload(),
        )

    if limit < 1:
        raise ValueError("limit должен быть больше нуля")

    session = None

    try:
        session = get_session()
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
                reason="dataset_not_registered",
                **_empty_payload(),
            )

        if not getattr(dataset, "enabled", True):
            return build_check_result(
                checked=False, applicable=True, result="unavailable",
                data_date=dataset.last_data_date, dataset_code=DATASET_CODE,
                source=SOURCE_CODE, reason="dataset_disabled", **_empty_payload(),
            )

        data_date = dataset.last_data_date

        if data_date is None:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=None,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="dataset_not_loaded",
                **_empty_payload(),
            )

        snapshot_count = (
            session.execute(
                select(func.count())
                .select_from(CbrWarningListEntry)
                .where(
                    CbrWarningListEntry.dataset_id == dataset.id,
                    CbrWarningListEntry.data_date == data_date,
                )
            )
            .scalar_one()
        )

        if snapshot_count == 0:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=data_date,
                dataset_code=DATASET_CODE,
                source=SOURCE_CODE,
                reason="dataset_snapshot_missing",
                **_empty_payload(),
            )

        total_count = (
            session.execute(
                select(func.count())
                .select_from(CbrWarningListEntry)
                .where(
                    CbrWarningListEntry.dataset_id == dataset.id,
                    CbrWarningListEntry.data_date == data_date,
                    CbrWarningListEntry.inn == clean_inn,
                )
            )
            .scalar_one()
        )

        rows = (
            session.execute(
                select(CbrWarningListEntry)
                .where(
                    CbrWarningListEntry.dataset_id == dataset.id,
                    CbrWarningListEntry.data_date == data_date,
                    CbrWarningListEntry.inn == clean_inn,
                )
                .order_by(
                    CbrWarningListEntry.entry_date.desc().nullslast(),
                    CbrWarningListEntry.id.desc(),
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )

        records = [
            _serialize_entry(row)
            for row in rows
        ]

        common = {
            **_empty_payload(),
            "is_listed": bool(total_count),
            "record_count": total_count,
            "records": records,
            "records_limited": total_count > len(records),
        }

        return build_check_result(
            checked=True,
            applicable=True,
            result="found" if total_count else "not_found",
            data_date=data_date,
            dataset_code=DATASET_CODE,
            source=SOURCE_CODE,
            reason=None,
            **common,
        )

    except SQLAlchemyError:
        return build_check_result(
            checked=False, applicable=True, result="unavailable",
            data_date=None, dataset_code=DATASET_CODE, source=SOURCE_CODE,
            reason="storage_error", **_empty_payload(),
        )
    finally:
        if session is not None:
            session.close()
