from __future__ import annotations

from datetime import date
import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.company import Company
from app.models.corporate_disclosure import CorporateDisclosureCheck
from app.models.source import DataSet
from app.providers.prime_disclosure_provider import PrimeDisclosureProvider, PrimeDisclosureProviderError, COMPANY_URL
from app.services.check_result import build_check_result
from app.services.company_fact_service import sync_disclosure_profile_facts
from app.services.corporate_disclosure_registry_service import DATASET_CODE, SOURCE_CODE, ensure_corporate_disclosure_dataset


def _company_inn(value):
    value = re.sub(r"\D", "", str(value or ""))
    return value if len(value) == 10 else None


def _serialize(row: CorporateDisclosureCheck, *, cached=True):
    if row.result_status != "success":
        return build_check_result(
            checked=False, applicable=True, result="unavailable", data_date=row.request_date,
            dataset_code=DATASET_CODE, source=SOURCE_CODE, reason=row.error_code or "source_error",
            message=row.error_message, http_status=row.http_status, cached=cached,
            matching_method="inn_exact", records=[], record_count=None, source_url=row.source_url,
        )
    profile = dict(row.profile or {}) if row.is_found else None
    documents = list(row.documents or [])
    return build_check_result(
        checked=True, applicable=True, result="found" if row.is_found else "not_found",
        data_date=row.request_date, dataset_code=DATASET_CODE, source=SOURCE_CODE,
        reason=None, http_status=row.http_status, cached=cached, matching_method="inn_exact",
        profile=profile, documents=documents, record_count=row.document_count or 0,
        source_url=row.source_url,
        interpretation_note="Это публичное раскрытие эмитента, а не обязательный профиль для всех российских компаний.",
        coverage_note="Отсутствие страницы означает только отсутствие данных у выбранного распространителя; это не негативный признак.",
    )


def get_cached_corporate_disclosure_check(inn, request_date=None):
    inn = _company_inn(inn)
    request_date = request_date or date.today()
    if not inn:
        return build_check_result(checked=True, applicable=False, result="not_applicable", data_date=None, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason="requires_legal_entity_inn10", matching_method=None, records=[], record_count=0)
    session = get_session()
    try:
        row = session.scalar(select(CorporateDisclosureCheck).where(CorporateDisclosureCheck.inn == inn, CorporateDisclosureCheck.request_date == request_date))
        if row is None:
            return build_check_result(checked=False, applicable=True, result="unavailable", data_date=None, dataset_code=DATASET_CODE, source=SOURCE_CODE, reason="not_checked", request_date=request_date, cached=False, matching_method="inn_exact", records=[], record_count=None, source_url=COMPANY_URL.format(inn=inn))
        return _serialize(row, cached=True)
    finally:
        session.close()


def _save(*, company_id, dataset_id, inn, request_date, source_url, result_status, is_found=None, profile=None, documents=None, document_count=None, http_status=None, error_code=None, error_message=None):
    values = {
        "company_id": company_id, "dataset_id": dataset_id, "inn": inn, "request_date": request_date,
        "source_url": source_url, "result_status": result_status, "is_found": is_found,
        "profile": profile, "documents": documents, "document_count": document_count,
        "http_status": http_status, "error_code": error_code, "error_message": error_message,
    }
    session = get_session()
    try:
        session.execute(insert(CorporateDisclosureCheck).values(**values).on_conflict_do_update(
            constraint="uq_corporate_disclosure_check",
            set_={k: v for k, v in values.items() if k not in {"company_id", "dataset_id", "inn", "request_date"}},
        ))
        session.commit()
        return session.scalar(select(CorporateDisclosureCheck).where(CorporateDisclosureCheck.dataset_id == dataset_id, CorporateDisclosureCheck.inn == inn, CorporateDisclosureCheck.request_date == request_date))
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def refresh_corporate_disclosure_check(inn, request_date=None, provider=None, *, force_refresh=False):
    inn = _company_inn(inn)
    request_date = request_date or date.today()
    if not inn:
        raise ValueError("Корпоративное раскрытие проверяется только для 10-значного ИНН юрлица")
    ensure_corporate_disclosure_dataset()
    if provider is None and not force_refresh:
        cached = get_cached_corporate_disclosure_check(inn, request_date)
        if cached["result"] in {"found", "not_found"}:
            return cached
    session = get_session()
    try:
        company_id = session.scalar(select(Company.id).where(Company.inn == inn))
        dataset_id = session.scalar(select(DataSet.id).where(DataSet.code == DATASET_CODE))
    finally:
        session.close()
    if company_id is None:
        raise ValueError("Компания отсутствует в master registry")
    source_url = COMPANY_URL.format(inn=inn)
    provider = provider or PrimeDisclosureProvider()
    try:
        response = provider.check_inn(inn)
        row = _save(
            company_id=company_id, dataset_id=dataset_id, inn=inn, request_date=request_date,
            source_url=response["source_url"], result_status="success", is_found=response["found"],
            profile=response["profile"], documents=response["documents"], document_count=response["document_count"],
            http_status=response["http_status"],
        )
        if response["found"] and response["profile"]:
            sync_disclosure_profile_facts(
                company_id=company_id, dataset_id=dataset_id, inn=inn,
                profile=response["profile"], source_url=response["source_url"],
            )
    except PrimeDisclosureProviderError as error:
        row = _save(
            company_id=company_id, dataset_id=dataset_id, inn=inn, request_date=request_date,
            source_url=source_url, result_status="error", http_status=error.http_status,
            error_code=error.kind, error_message=error.message,
        )
    return _serialize(row, cached=False)
