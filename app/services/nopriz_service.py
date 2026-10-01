from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.nostroy import NoprizMemberCheck
from app.providers.nopriz_provider import NoprizMemberProvider, NoprizProviderError
from app.services.check_result import build_check_result
from app.services.nostroy_service import _company_inn
from app.services.sro_registry_service import ensure_sro_datasets


SOURCE_CODE = "nopriz"
DATASET_CODE = "nopriz_sro_members_on_demand"


def _result(row, *, cached=True):
    records = list(row.public_records or [])
    requested_inn = str(getattr(row, "inn", "") or "")
    exact_records_confirmed = bool(records) and all(
        isinstance(record, dict)
        and str(record.get("inn") or "") == requested_inn
        and len(str(record.get("ogrn") or "")) == 13
        and str(record.get("ogrn") or "").isdigit()
        for record in records
    )
    if (
        row.result_status != "success"
        or row.is_found is not True
        or not exact_records_confirmed
    ):
        historical_negative = row.result_status == "success" and row.is_found is False
        reason = (
            "nopriz_negative_semantics_unproven"
            if historical_negative
            else row.error_code
            or (
                "exact_identity_not_confirmed"
                if row.result_status == "success"
                else "source_error"
            )
        )
        message = row.error_message
        if historical_negative and not message:
            message = "Недостаточно доказательств для отрицательного результата НОПРИЗ"
        return build_check_result(checked=False, applicable=True, result="unavailable", data_date=row.request_date, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason=reason, message=message, http_status=row.http_status, cached=cached, matching_method="inn_exact_post_filter", records=[], record_count=None)
    return build_check_result(checked=True, applicable=True, result="found", data_date=row.request_date, dataset_code=DATASET_CODE, source=SOURCE_CODE, matching_method="inn_exact_post_filter", records=records, record_count=len(records), active_record_count=sum(r.get("member_status_code") == "1" for r in records), cached=cached, interpretation_note="Запись подтверждает текущее или историческое членство в СРО изыскателей/проектировщиков; статус проверяется отдельно.", coverage_note="Кандидатная проверка ЕРЧ СРО НОПРИЗ с точной пост-фильтрацией ИНН; отсутствие точной строки не доказывает отсутствие членства.")


def get_cached_nopriz_check(inn, request_date=None, *, applicable=True):
    inn, request_date = _company_inn(inn), request_date or date.today()
    if not inn or applicable is False:
        return build_check_result(checked=True, applicable=False, result="not_applicable", data_date=None, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason="design_sro_context_not_applicable", matching_method=None, records=[], record_count=0)
    session = get_session()
    try:
        row = session.scalar(select(NoprizMemberCheck).where(NoprizMemberCheck.inn == inn, NoprizMemberCheck.request_date == request_date))
        if row is None:
            return build_check_result(checked=False, applicable=True, result="unavailable", data_date=None, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason="not_checked", request_date=request_date, cached=False, matching_method="inn_exact_post_filter", records=[], record_count=None)
        return _result(row)
    finally:
        session.close()


def refresh_nopriz_check(inn, request_date=None, provider=None, *, applicable=True, force_refresh=False):
    inn, request_date = _company_inn(inn), request_date or date.today()
    if not inn:
        raise ValueError("НОПРИЗ в company-продукте проверяется только для 10-значного ИНН")
    if applicable is not True:
        return get_cached_nopriz_check(inn, request_date, applicable=applicable)
    ensure_sro_datasets()
    if provider is None and not force_refresh:
        cached = get_cached_nopriz_check(inn, request_date)
        # Reuse failures for the dated cache window as source backoff. A caller
        # may explicitly force a retry after operational review.
        if cached["reason"] != "not_checked":
            return cached
    provider = provider or NoprizMemberProvider()
    values = {"inn": inn, "request_date": request_date}
    try:
        response = provider.check_inn(inn)
        if response["result_status"] == "success" and response["is_found"] is True:
            values.update(result_status="success", is_found=True, record_count=response["record_count"], public_records=response["public_records"], http_status=response["http_status"], error_code=None, error_message=None)
        else:
            values.update(result_status="unknown", is_found=None, record_count=None, public_records=[], http_status=response["http_status"], error_code=response.get("error_code") or "exact_identity_not_confirmed", error_message=None)
    except NoprizProviderError as error:
        values.update(result_status="error", is_found=None, record_count=None, public_records=None, http_status=error.http_status, error_code=error.kind, error_message=error.message)
    session = get_session()
    try:
        statement = insert(NoprizMemberCheck).values(**values).on_conflict_do_update(index_elements=["inn", "request_date"], set_={key: value for key, value in values.items() if key not in {"inn", "request_date"}}).returning(NoprizMemberCheck)
        row = session.execute(statement).scalar_one(); session.commit(); session.refresh(row)
        return _result(row, cached=False)
    except Exception:
        session.rollback(); raise
    finally:
        session.close()
