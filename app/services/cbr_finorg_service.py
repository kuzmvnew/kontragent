from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.database.postgres import get_session
from app.ingestion.cbr_finorg import (
    save_cbr_finorg_attempt,
)
from app.ingestion.cbr_finorg_worker import enqueue_cbr_finorg_check
from app.models.cbr_finorg import CbrFinorgCheck
from app.providers.cbr_finorg_provider import (
    CbrFinorgProvider,
    CbrFinorgProviderError,
    normalize_inn,
)
from app.services.cbr_finorg_registry_service import (
    ensure_cbr_finorg_dataset,
)
from app.services.check_result import build_check_result


DATASET_CODE = "cbr_finorg"
SOURCE_CODE = "cbr_finorg"


def request_cbr_finorg_check_for_inn(
    inn,
    *,
    raw_root: Path,
    request_date=None,
    now=None,
):
    """Queue one durable, idempotent official CBR check.

    CBR FINORG intentionally remains on-demand.  Ordinary companies and
    entrepreneurs are not classified as NOT_APPLICABLE before the official
    service answers for their exact INN.
    """

    clean_inn = normalize_inn(inn)
    request_date = request_date or date.today()
    now = now or datetime.now(timezone.utc)
    if len(clean_inn) not in {10, 12}:
        return _unavailable_result(
            inn=clean_inn,
            request_date=request_date,
            reason="invalid_inn",
            message="Для проверки Банка России нужен ИНН из 10 или 12 цифр",
        )

    session = get_session()
    try:
        creation = enqueue_cbr_finorg_check(
            session,
            inn=clean_inn,
            request_date=request_date,
            raw_root=raw_root,
            now=now,
        )
        session.commit()
        return {
            "checked": False,
            "applicable": True,
            "result": "queued",
            "dataset_code": DATASET_CODE,
            "source": SOURCE_CODE,
            "inn": clean_inn,
            "request_date": request_date,
            "job_id": str(creation.job.id),
            "created": creation.created,
            "matching_method": "inn_exact_with_ogrn_consistency",
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _empty_payload():
    return {
        "is_participant": False,
        "participant_status": None,
        "participant_types": [],
        "licenses": [],
        "active_license_count": 0,
        "payment_systems": [],
        "mfo_history": [],
        "cbr_id": None,
        "ogrn": None,
        "name": None,
        "short_name": None,
        "regnum": None,
        "bic": None,
        "registration_date": None,
        "checked_at": None,
        "cached": False,
        "matching_method": "inn_exact",
        "interpretation_note": (
            "Наличие записи подтверждает, что Банк России "
            "публикует сведения об этом участнике финансового рынка. "
            "Статус и лицензии показываются ровно в объеме "
            "официального веб-сервиса."
        ),
        "coverage_note": (
            "Отсутствие записи не означает нарушение: не всякая "
            "хозяйственная деятельность требует допуска или лицензии "
            "Банка России."
        ),
    }


def _parse_date(value):
    if value is None:
        return None
    if isinstance(value, date):
        return value

    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _license_is_active(license_info, request_date):
    start_date = _parse_date(
        license_info.get("start_date")
    )
    end_date = _parse_date(
        license_info.get("end_date")
    )

    if start_date is None and end_date is None:
        return False

    if start_date and start_date > request_date:
        return False

    if end_date and end_date < request_date:
        return False

    return True


def _serialize_success(row, *, cached):
    licenses = list(row.licenses or [])
    active_license_count = sum(
        1
        for item in licenses
        if _license_is_active(
            item,
            row.request_date,
        )
    )

    common = {
        **_empty_payload(),
        "is_participant": bool(row.is_participant),
        "participant_status": row.status,
        "participant_types": list(row.fo_types or []),
        "licenses": licenses,
        "active_license_count": active_license_count,
        "payment_systems": list(row.payment_systems or []),
        "mfo_history": list(row.mfo_history or []),
        "cbr_id": row.cbr_id,
        "ogrn": row.ogrn,
        "name": row.name,
        "short_name": row.short_name,
        "regnum": row.regnum,
        "bic": row.bic,
        "registration_date": row.registration_date,
        "checked_at": row.checked_at,
        "cached": cached,
    }

    is_participant = bool(row.is_participant)
    return build_check_result(
        checked=True,
        applicable=is_participant,
        result=("found" if is_participant else "not_applicable"),
        data_date=row.request_date,
        dataset_code=DATASET_CODE,
        source=SOURCE_CODE,
        reason=(None if is_participant else "official_exact_inn_no_participant"),
        **common,
    )


def _unavailable_result(
    *,
    inn,
    request_date,
    reason,
    message=None,
    checked_at=None,
    http_status=None,
    cached=False,
):
    common = {
        **_empty_payload(),
        "checked_at": checked_at,
        "cached": cached,
    }

    return build_check_result(
        checked=False,
        applicable=True,
        result="unavailable",
        data_date=request_date,
        dataset_code=DATASET_CODE,
        source=SOURCE_CODE,
        reason=reason,
        inn=inn,
        message=message,
        http_status=http_status,
        **common,
    )


def _get_row(session, *, inn, request_date):
    return (
        session.execute(
            select(CbrFinorgCheck)
            .where(
                CbrFinorgCheck.inn == inn,
                CbrFinorgCheck.request_date == request_date,
            )
            .order_by(CbrFinorgCheck.id.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )


def get_cached_cbr_finorg_check_for_inn(
    inn,
    request_date=None,
):
    inn = normalize_inn(inn)
    request_date = request_date or date.today()

    if len(inn) not in {10, 12}:
        return _unavailable_result(
            inn=inn,
            request_date=request_date,
            reason="invalid_inn",
            message=(
                "Для проверки Банка России нужен ИНН "
                "из 10 или 12 цифр"
            ),
        )

    session = get_session()

    try:
        row = _get_row(
            session,
            inn=inn,
            request_date=request_date,
        )

        if row is None:
            return _unavailable_result(
                inn=inn,
                request_date=request_date,
                reason="not_checked",
                message=(
                    "Сведения об участнике финансового рынка "
                    "ещё не запрашивались у Банка России"
                ),
            )

        if row.result_status == "success":
            return _serialize_success(
                row,
                cached=True,
            )

        return _unavailable_result(
            inn=inn,
            request_date=request_date,
            reason=(
                row.error_code
                or "source_error"
            ),
            message=row.error_message,
            checked_at=row.checked_at,
            http_status=row.http_status,
            cached=True,
        )

    finally:
        session.close()


def refresh_cbr_finorg_check_for_inn(
    inn,
    request_date=None,
    provider=None,
):
    inn = normalize_inn(inn)
    request_date = request_date or date.today()

    if len(inn) not in {10, 12}:
        return _unavailable_result(
            inn=inn,
            request_date=request_date,
            reason="invalid_inn",
            message=(
                "Для проверки Банка России нужен ИНН "
                "из 10 или 12 цифр"
            ),
        )

    ensure_cbr_finorg_dataset()
    provider = provider or CbrFinorgProvider()

    try:
        response = provider.check_inn(inn)

    except CbrFinorgProviderError as error:
        row = save_cbr_finorg_attempt(
            inn=inn,
            request_date=request_date,
            result_status="error",
            is_participant=None,
            participant=None,
            raw_payload=None,
            http_status=error.http_status,
            error_code=error.kind,
            error_message=error.message,
        )

        return _unavailable_result(
            inn=inn,
            request_date=request_date,
            reason=error.kind,
            message=error.message,
            checked_at=row.checked_at,
            http_status=error.http_status,
            cached=False,
        )

    row = save_cbr_finorg_attempt(
        inn=inn,
        request_date=request_date,
        result_status="success",
        is_participant=response["found"],
        participant=response.get("participant"),
        raw_payload=response,
        http_status=response.get("http_status"),
        error_code=None,
        error_message=None,
    )

    return _serialize_success(
        row,
        cached=False,
    )
