from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.database.postgres import get_session
from app.models.fns_sme_support import FnsSmeSupportEntry
from app.models.source import DataSet, IngestionRun
from app.services.check_result import build_check_result


DATASET_CODE = "fns_sme_support"
SOURCE_CODE = "fns_sme_support"
SOURCE_PAGE_URL = "https://www.nalog.gov.ru/opendata/7707329152-rsmppp/"


def normalize_inn(value) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() and len(text) in {10, 12} else ""


def _empty_payload() -> dict:
    return {
        "is_support_recipient": None,
        "record_count": None,
        "records": [],
        "matching_method": "inn_exact",
        "official_url": SOURCE_PAGE_URL,
        "interpretation_note": (
            "Наличие записи означает, что в официальном реестре ФНС опубликован "
            "факт предоставления поддержки. Сам по себе этот факт не является "
            "положительной или отрицательной оценкой надёжности контрагента."
        ),
        "coverage_note": (
            "not_found означает только отсутствие подходящей записи с точным ИНН "
            "в успешно опубликованном snapshot на указанную дату. Физлица на НПД, "
            "которые не являлись ИП, намеренно не публикуются в карточке компании."
        ),
    }


def _serialize(row) -> dict:
    return {
        "source_document_id": row.source_document_id,
        "provider_inn": row.provider_inn,
        "recipient_inn": row.recipient_inn,
        "recipient_ogrn": row.recipient_ogrn,
        "recipient_kind": row.recipient_kind,
        "information_date": row.information_date,
        "support_until": row.support_until,
        "decision_date": row.decision_date,
        "termination_date": row.termination_date,
        "violation_code": row.violation_code,
        "has_violation": (
            True if row.violation_code == "1"
            else False if row.violation_code == "2"
            else None
        ),
        "support_form_code": row.support_form_code,
        "support_form_name": row.support_form_name,
        "support_type_code": row.support_type_code,
        "support_type_name": row.support_type_name,
        "amounts": list(row.amounts or []),
        "violations": list(row.violations or []),
        "regulatory_document_ids": list(row.regulatory_document_ids or []),
    }


def get_fns_sme_support_check_for_inn(inn: str, limit: int = 20) -> dict:
    clean_inn = normalize_inn(inn)
    if not clean_inn:
        return build_check_result(
            checked=False, applicable=True, result="unavailable", data_date=None,
            dataset_code=DATASET_CODE, source=SOURCE_CODE, reason="invalid_inn",
            **_empty_payload(),
        )
    if limit < 1:
        raise ValueError("limit должен быть больше нуля")

    session = None
    try:
        session = get_session()
        dataset = session.execute(
            select(DataSet).where(DataSet.code == DATASET_CODE).limit(1)
        ).scalar_one_or_none()
        if dataset is None:
            return build_check_result(
                checked=False, applicable=True, result="unavailable", data_date=None,
                dataset_code=DATASET_CODE, source=SOURCE_CODE,
                reason="dataset_not_registered", **_empty_payload(),
            )
        if not dataset.enabled:
            return build_check_result(
                checked=False, applicable=True, result="unavailable",
                data_date=dataset.last_data_date, dataset_code=DATASET_CODE,
                source=SOURCE_CODE, reason="dataset_disabled", **_empty_payload(),
            )

        run = session.execute(
            select(IngestionRun).where(
                IngestionRun.dataset_id == dataset.id,
                IngestionRun.status == "success",
            ).order_by(
                IngestionRun.finished_at.desc().nullslast(),
                IngestionRun.id.desc(),
            ).limit(1)
        ).scalar_one_or_none()
        if run is None or not (run.details or {}).get("complete_snapshot"):
            return build_check_result(
                checked=False, applicable=True, result="unavailable",
                data_date=dataset.last_data_date, dataset_code=DATASET_CODE,
                source=SOURCE_CODE, reason="complete_snapshot_not_loaded",
                **_empty_payload(),
            )

        snapshot_count = session.execute(
            select(func.count()).select_from(FnsSmeSupportEntry).where(
                FnsSmeSupportEntry.ingestion_run_id == run.id
            )
        ).scalar_one()
        expected_eligible = int((run.details or {}).get("eligible_records") or 0)
        if snapshot_count != expected_eligible:
            return build_check_result(
                checked=False, applicable=True, result="unavailable", data_date=run.data_date,
                dataset_code=DATASET_CODE, source=SOURCE_CODE,
                reason="snapshot_count_mismatch", **_empty_payload(),
            )

        filters = [
            FnsSmeSupportEntry.ingestion_run_id == run.id,
            FnsSmeSupportEntry.recipient_inn == clean_inn,
        ]
        if len(clean_inn) == 12:
            filters.append(
                FnsSmeSupportEntry.recipient_kind == "individual_entrepreneur_or_kfh"
            )

        total_count = session.execute(
            select(func.count()).select_from(FnsSmeSupportEntry).where(*filters)
        ).scalar_one()
        rows = session.execute(
            select(FnsSmeSupportEntry).where(*filters).order_by(
                FnsSmeSupportEntry.decision_date.desc(),
                FnsSmeSupportEntry.id.desc(),
            ).limit(limit)
        ).scalars().all()
        records = [_serialize(row) for row in rows]
        payload = {
            **_empty_payload(),
            "is_support_recipient": bool(total_count),
            "record_count": total_count,
            "records": records,
            "records_limited": total_count > len(records),
            "source_records": (run.details or {}).get("source_records"),
            "eligible_records": expected_eligible,
            "excluded_npd_records": (run.details or {}).get("excluded_npd_records"),
        }
        return build_check_result(
            checked=True, applicable=True,
            result="found" if total_count else "not_found",
            data_date=run.data_date, dataset_code=DATASET_CODE,
            source=SOURCE_CODE, reason=None, **payload,
        )
    except SQLAlchemyError:
        return build_check_result(
            checked=False, applicable=True, result="unavailable", data_date=None,
            dataset_code=DATASET_CODE, source=SOURCE_CODE, reason="storage_error",
            **_empty_payload(),
        )
    finally:
        if session is not None:
            session.close()
