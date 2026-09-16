from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.cbr_finorg import CbrFinorgCheck
from app.models.source import DataSet


DATASET_CODE = "cbr_finorg"


def _parse_date(value):
    if value is None:
        return None
    if isinstance(value, date):
        return value

    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _get_dataset_id(session) -> int:
    dataset_id = (
        session.execute(
            select(DataSet.id)
            .where(DataSet.code == DATASET_CODE)
            .limit(1)
        )
        .scalar_one_or_none()
    )

    if dataset_id is None:
        raise RuntimeError(
            "Dataset cbr_finorg не зарегистрирован"
        )

    return dataset_id


def save_cbr_finorg_attempt(
    *,
    inn: str,
    request_date: date,
    result_status: str,
    is_participant: bool | None,
    participant: dict | None,
    raw_payload: dict | None,
    http_status: int | None,
    error_code: str | None,
    error_message: str | None,
):
    session = get_session()

    try:
        dataset_id = _get_dataset_id(session)
        now = datetime.now(timezone.utc)
        participant = participant or {}

        values = {
            "dataset_id": dataset_id,
            "inn": inn,
            "request_date": request_date,
            "result_status": result_status,
            "is_participant": is_participant,
            "cbr_id": participant.get("cbr_id"),
            "ogrn": participant.get("ogrn"),
            "short_name": participant.get("short_name"),
            "name": participant.get("name"),
            "status": participant.get("status"),
            "fo_types": participant.get("fo_types") or [],
            "licenses": participant.get("licenses") or [],
            "payment_systems": (
                participant.get("payment_systems") or []
            ),
            "mfo_history": participant.get("mfo_history") or [],
            "websites": participant.get("websites") or [],
            "address": participant.get("address"),
            "phones": participant.get("phones"),
            "email": participant.get("email"),
            "region": participant.get("region"),
            "regnum": participant.get("regnum"),
            "bic": participant.get("bic"),
            "is_sro_member": participant.get("is_sro_member"),
            "has_branches": participant.get("has_branches"),
            "registration_date": _parse_date(
                participant.get("registration_date")
            ),
            "http_status": http_status,
            "error_code": error_code,
            "error_message": error_message,
            "raw_payload": raw_payload,
            "checked_at": now,
            "updated_at": now,
        }

        statement = insert(
            CbrFinorgCheck
        ).values(**values)

        statement = statement.on_conflict_do_update(
            constraint=(
                "uq_cbr_finorg_dataset_inn_request_date"
            ),
            set_={
                key: value
                for key, value in values.items()
                if key not in {
                    "dataset_id",
                    "inn",
                    "request_date",
                }
            },
        )

        session.execute(statement)
        session.commit()

        return (
            session.execute(
                select(CbrFinorgCheck)
                .where(
                    CbrFinorgCheck.dataset_id == dataset_id,
                    CbrFinorgCheck.inn == inn,
                    CbrFinorgCheck.request_date == request_date,
                )
                .limit(1)
            )
            .scalar_one()
        )

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
