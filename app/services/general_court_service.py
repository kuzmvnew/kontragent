from __future__ import annotations

from datetime import date
import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.company import Company
from app.models.stage15_checks import GeneralCourtCheck
from app.providers.general_court_provider import (
    GeneralCourtProviderError,
    GeneralCourtRouter,
    MOSCOW_SEARCH_URL,
)
from app.services.check_result import build_check_result
from app.services.stage15_registry_service import ensure_stage15_dataset


DATASET_CODE = "moscow_general_court_cases"
SOURCE_CODE = "moscow_courts_official"


def _clean_inn(value):
    value = re.sub(r"\D", "", str(value or ""))
    return value if len(value) in {10, 12} else None


def _serialize(row: GeneralCourtCheck, *, cached=True):
    if row.result_status != "success":
        return build_check_result(checked=False, applicable=True, result="unavailable", data_date=row.request_date, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason=row.error_code or "source_error", message=row.error_message, cached=cached, cases=[], record_count=None, coverage=row.coverage or {}, source_url=row.source_url, checked_at=row.checked_at)
    cases = list(row.cases or [])
    return build_check_result(checked=True, applicable=True, result="found" if cases else "not_found", data_date=row.request_date, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason=None, cached=cached, cases=cases, record_count=len(cases), coverage=dict(row.coverage or {}), source_url=row.source_url, checked_at=row.checked_at, interpretation_note="Совпадение подтверждено точным полным наименованием на официальном портале, но без ИНН в выдаче; это средняя, а не максимальная уверенность.")


def get_cached_general_court_check(inn, request_date=None):
    inn = _clean_inn(inn)
    request_date = request_date or date.today()
    if not inn:
        return build_check_result(checked=True, applicable=False, result="not_applicable", data_date=None, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason="invalid_inn", cases=[], record_count=0)
    session = get_session()
    try:
        row = session.scalar(select(GeneralCourtCheck).where(GeneralCourtCheck.inn == inn, GeneralCourtCheck.request_date == request_date))
        if row is None:
            return build_check_result(checked=False, applicable=True, result="unavailable", data_date=None, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason="not_checked", cached=False, cases=[], record_count=None, coverage={"coverage_label": "NOT CHECKED"}, source_url=MOSCOW_SEARCH_URL, checked_at=None)
        return _serialize(row, cached=True)
    finally:
        session.close()


def refresh_general_court_check(inn, request_date=None, provider=None, *, force_refresh=False):
    inn = _clean_inn(inn)
    if not inn:
        raise ValueError("Некорректный ИНН")
    request_date = request_date or date.today()
    dataset_id = ensure_stage15_dataset(SOURCE_CODE)
    if provider is None and not force_refresh:
        cached = get_cached_general_court_check(inn, request_date)
        if cached["result"] in {"found", "not_found"}:
            return cached
    session = get_session()
    try:
        company = session.scalar(select(Company).where(Company.inn == inn))
    finally:
        session.close()
    if company is None:
        raise ValueError("Компания отсутствует в master registry")
    full_name = (company.full_name or company.name or "").strip()
    route = None
    if provider is None:
        route, provider = GeneralCourtRouter().route(company.region_code)
    source_url = route.portal_url if route else getattr(provider, "source_url", MOSCOW_SEARCH_URL)
    values = {"company_id": company.id, "dataset_id": dataset_id, "inn": inn, "request_date": request_date, "provider_code": provider.code, "source_url": source_url}
    try:
        if not hasattr(provider, "search_company"):
            raise GeneralCourtProviderError(
                kind="access_pending",
                message="Для региона не настроен проверяемый official path",
            )
        response = provider.search_company(inn=inn, ogrn=company.ogrn, full_name=full_name)
        values.update(result_status="success", cases=response["cases"], coverage=response["coverage"], source_url=response["source_url"], http_status=response["http_status"], error_code=None, error_message=None)
    except GeneralCourtProviderError as error:
        values.update(result_status="error", cases=[], coverage={"coverage_label": "CHECK FAILED; NO NEGATIVE INFERENCE"}, http_status=error.http_status, error_code=error.kind, error_message=error.message)
    session = get_session()
    try:
        session.execute(insert(GeneralCourtCheck).values(**values).on_conflict_do_update(constraint="uq_general_court_check", set_={k: v for k, v in values.items() if k not in {"company_id", "dataset_id", "inn", "request_date"}}))
        session.commit()
        row = session.scalar(select(GeneralCourtCheck).where(GeneralCourtCheck.dataset_id == dataset_id, GeneralCourtCheck.inn == inn, GeneralCourtCheck.request_date == request_date))
        return _serialize(row, cached=False)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
