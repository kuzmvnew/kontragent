from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.npd import NpdStatusCheck
from app.providers.fns_npd_provider import (
    FnsNpdProvider,
    FnsNpdProviderError,
)
from app.services.check_result import build_check_result


DATASET_CODE = "fns_npd"
SOURCE_CODE = "fns_npd"


def normalize_inn(value) -> str:
    return "".join(
        symbol
        for symbol in str(value or "")
        if symbol.isdigit()
    )


def _not_applicable_result(inn, request_date):
    return build_check_result(
        checked=True,
        applicable=False,
        result="not_applicable",
        data_date=request_date,
        dataset_code=DATASET_CODE,
        source=SOURCE_CODE,
        reason="legal_entity",
        inn=inn,
        request_date=request_date,
        is_npd=False,
        message=(
            "Статус НПД применяется к физическим лицам "
            "и индивидуальным предпринимателям"
        ),
        checked_at=None,
        cached=False,
    )


def _unavailable_result(
    *,
    inn,
    request_date,
    reason,
    message=None,
    checked_at=None,
    http_status=None,
    error_code=None,
    cached=False,
):
    return build_check_result(
        checked=False,
        applicable=True,
        result="unavailable",
        data_date=request_date,
        dataset_code=DATASET_CODE,
        source=SOURCE_CODE,
        reason=reason,
        inn=inn,
        request_date=request_date,
        is_npd=None,
        message=message,
        checked_at=checked_at,
        http_status=http_status,
        error_code=error_code,
        cached=cached,
    )


def _success_result(row, *, cached):
    result = (
        "found"
        if row.is_npd is True
        else "not_found"
    )

    return build_check_result(
        checked=True,
        applicable=True,
        result=result,
        data_date=row.request_date,
        dataset_code=DATASET_CODE,
        source=SOURCE_CODE,
        reason=None,
        inn=row.inn,
        request_date=row.request_date,
        is_npd=bool(row.is_npd),
        message=row.message,
        checked_at=row.checked_at,
        http_status=row.http_status,
        error_code=None,
        cached=cached,
    )


def _error_row_result(row, *, cached):
    return _unavailable_result(
        inn=row.inn,
        request_date=row.request_date,
        reason=(
            row.error_code
            or "source_error"
        ),
        message=row.message,
        checked_at=row.checked_at,
        http_status=row.http_status,
        error_code=row.error_code,
        cached=cached,
    )


def _get_row(
    session,
    *,
    inn,
    request_date,
):
    return (
        session.execute(
            select(NpdStatusCheck)
            .where(
                NpdStatusCheck.inn == inn,
                NpdStatusCheck.request_date
                == request_date,
            )
            .limit(1)
        )
        .scalar_one_or_none()
    )


def _save_attempt(
    *,
    inn,
    request_date,
    result_status,
    is_npd,
    message,
    http_status,
    error_code,
):
    session = get_session()

    try:
        now = datetime.now(timezone.utc)

        statement = insert(
            NpdStatusCheck
        ).values(
            inn=inn,
            request_date=request_date,
            result_status=result_status,
            is_npd=is_npd,
            message=message,
            http_status=http_status,
            error_code=error_code,
            checked_at=now,
        )

        statement = statement.on_conflict_do_update(
            constraint=(
                "uq_npd_status_inn_request_date"
            ),
            set_={
                "result_status": result_status,
                "is_npd": is_npd,
                "message": message,
                "http_status": http_status,
                "error_code": error_code,
                "checked_at": now,
                "updated_at": now,
            },
        )

        session.execute(statement)
        session.commit()

        return _get_row(
            session,
            inn=inn,
            request_date=request_date,
        )

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def get_cached_npd_check_for_inn(
    inn,
    request_date=None,
):
    inn = normalize_inn(inn)
    request_date = request_date or date.today()

    if len(inn) == 10:
        return _not_applicable_result(
            inn,
            request_date,
        )

    if len(inn) != 12:
        return _unavailable_result(
            inn=inn,
            request_date=request_date,
            reason="invalid_inn",
            message="Для проверки НПД нужен ИНН из 12 цифр",
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
                    "Статус НПД ещё не проверялся "
                    "через публичный сервис ФНС"
                ),
            )

        if row.result_status == "success":
            return _success_result(
                row,
                cached=True,
            )

        return _error_row_result(
            row,
            cached=True,
        )

    finally:
        session.close()


def refresh_npd_check_for_inn(
    inn,
    request_date=None,
    provider=None,
):
    inn = normalize_inn(inn)
    request_date = request_date or date.today()

    if len(inn) == 10:
        return _not_applicable_result(
            inn,
            request_date,
        )

    if len(inn) != 12:
        return _unavailable_result(
            inn=inn,
            request_date=request_date,
            reason="invalid_inn",
            message="Для проверки НПД нужен ИНН из 12 цифр",
        )

    provider = provider or FnsNpdProvider()

    try:
        response = provider.check_status(
            inn=inn,
            request_date=request_date,
        )

    except FnsNpdProviderError as error:
        row = _save_attempt(
            inn=inn,
            request_date=request_date,
            result_status="error",
            is_npd=None,
            message=error.message,
            http_status=error.http_status,
            error_code=(
                error.code
                or error.kind
            ),
        )

        return _error_row_result(
            row,
            cached=False,
        )

    row = _save_attempt(
        inn=inn,
        request_date=request_date,
        result_status="success",
        is_npd=response["is_npd"],
        message=response.get("message"),
        http_status=response.get("http_status"),
        error_code=None,
    )

    return _success_result(
        row,
        cached=False,
    )
