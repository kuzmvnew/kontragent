from __future__ import annotations

from calendar import monthrange
from datetime import date
import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.company import Company
from app.models.stage15_checks import ArbitrationCourtCheck
from app.providers.arbitration_court_provider import CHECKO_URL, ArbitrationCourtProviderError, CheckoArbitrationProvider
from app.services.check_result import build_check_result
from app.services.stage15_registry_service import ensure_stage15_dataset


DATASET_CODE = "checko_arbitration_cases"
SOURCE_CODE = "checko_legal_cases"


def _year_before(value: date) -> date:
    return value.replace(year=value.year - 1, day=min(value.day, monthrange(value.year - 1, value.month)[1]))


def _period(request_date: date):
    return _year_before(request_date), request_date


def _serialize(row, *, cached=True):
    if row.result_status != "success":
        return build_check_result(checked=False, applicable=True, result="unavailable", data_date=row.date_to, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason=row.error_code or "source_error", message=row.error_message, cached=cached, cases=list(row.cases or []), loaded_pages=row.loaded_pages, total_pages=row.total_pages, total_count=row.total_count, is_full_period_loaded=False, source_url=row.source_url, checked_at=row.checked_at)
    cases = list(row.cases or [])
    return build_check_result(checked=True, applicable=True, result="found" if cases else "not_found", data_date=row.date_to, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason=None, cached=cached, cases=cases, loaded_pages=row.loaded_pages, total_pages=row.total_pages, total_count=row.total_count, loaded_count=len(cases), is_full_period_loaded=(row.total_pages or 0) <= row.loaded_pages, source_url=row.source_url, checked_at=row.checked_at, last_error=row.error_code, last_error_message=row.error_message, coverage_note="Показан загруженный sample; полный период подтверждён только когда загружены все страницы.")


def get_cached_arbitration_court_check(inn, request_date=None):
    inn = re.sub(r"\D", "", str(inn or ""))
    request_date = request_date or date.today()
    date_from, date_to = _period(request_date)
    session = get_session()
    try:
        row = session.scalar(select(ArbitrationCourtCheck).where(ArbitrationCourtCheck.inn == inn, ArbitrationCourtCheck.date_from == date_from, ArbitrationCourtCheck.date_to == date_to))
        if row is None:
            return build_check_result(checked=False, applicable=True, result="unavailable", data_date=None, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason="not_checked", cached=False, cases=[], loaded_pages=0, total_pages=None, total_count=None, is_full_period_loaded=False, source_url=CHECKO_URL, checked_at=None)
        return _serialize(row, cached=True)
    finally:
        session.close()


def refresh_arbitration_court_check(inn, request_date=None, provider=None, *, deepen=False):
    inn = re.sub(r"\D", "", str(inn or ""))
    if len(inn) not in {10, 12}:
        raise ValueError("Некорректный ИНН")
    request_date = request_date or date.today()
    date_from, date_to = _period(request_date)
    dataset_id = ensure_stage15_dataset("checko")
    session = get_session()
    try:
        company = session.scalar(select(Company).where(Company.inn == inn))
        existing = session.scalar(select(ArbitrationCourtCheck).where(ArbitrationCourtCheck.dataset_id == dataset_id, ArbitrationCourtCheck.inn == inn, ArbitrationCourtCheck.date_from == date_from, ArbitrationCourtCheck.date_to == date_to))
    finally:
        session.close()
    if company is None:
        raise ValueError("Компания отсутствует в master registry")
    if existing and not deepen and existing.result_status == "success":
        return _serialize(existing, cached=True)
    if deepen and (existing is None or existing.result_status != "success"):
        raise ValueError("Сначала загрузите первую страницу")
    if deepen and existing.total_pages is not None and existing.loaded_pages >= existing.total_pages:
        return _serialize(existing, cached=True)
    page = existing.loaded_pages + 1 if deepen else 1
    provider = provider or CheckoArbitrationProvider()
    values = {"company_id": company.id, "dataset_id": dataset_id, "inn": inn, "date_from": date_from, "date_to": date_to, "source_url": CHECKO_URL}
    try:
        response = provider.search_company(inn=inn, date_from=date_from, date_to=date_to, page=page, limit=100)
        prior = list(existing.cases or []) if deepen and existing else []
        values.update(result_status="success", loaded_pages=page, total_pages=response["total_pages"], total_count=response["total_count"], cases=prior + response["cases"], source_url=response["source_url"], error_code=None, error_message=None)
    except ArbitrationCourtProviderError as error:
        values.update(result_status="success" if deepen and existing and existing.result_status == "success" else "error", loaded_pages=existing.loaded_pages if existing else 0, total_pages=existing.total_pages if existing else None, total_count=existing.total_count if existing else None, cases=list(existing.cases or []) if existing else [], error_code=error.kind, error_message=error.message)
    session = get_session()
    try:
        session.execute(insert(ArbitrationCourtCheck).values(**values).on_conflict_do_update(constraint="uq_arbitration_court_check", set_={k: v for k, v in values.items() if k not in {"company_id", "dataset_id", "inn", "date_from", "date_to"}}))
        session.commit()
        row = session.scalar(select(ArbitrationCourtCheck).where(ArbitrationCourtCheck.dataset_id == dataset_id, ArbitrationCourtCheck.inn == inn, ArbitrationCourtCheck.date_from == date_from, ArbitrationCourtCheck.date_to == date_to))
        return _serialize(row, cached=False)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
