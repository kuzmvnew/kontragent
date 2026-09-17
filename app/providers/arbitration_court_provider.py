from __future__ import annotations

from datetime import date
import os
import re
from typing import Protocol

import httpx


CHECKO_URL = "https://api.checko.ru/v2/legal-cases"


class ArbitrationCourtProvider(Protocol):
    code: str

    def search_company(self, *, inn: str, date_from: date, date_to: date, page: int, limit: int = 100) -> dict: ...


class ArbitrationCourtProviderError(Exception):
    def __init__(self, *, kind: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status


def parse_checko_legal_cases(payload: dict, *, page: int, limit: int) -> dict:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw_cases = data.get("Записи") or data.get("Дела") or data.get("cases") or []
    cases = []
    for item in raw_cases:
        cases.append(
            {
                "case_number": item.get("Номер") or item.get("НомерДела") or item.get("number"),
                "source_url": item.get("Ссылка") or item.get("СсылкаКАД") or item.get("url"),
                "filing_date": item.get("Дата") or item.get("ДатаПоступления") or item.get("filing_date"),
                "court": item.get("Суд") or item.get("court"),
                "claimants": item.get("Истцы") or item.get("claimants") or [],
                "defendants": item.get("Ответчики") or item.get("defendants") or [],
                "claim_amount": item.get("СуммаИска") or item.get("claim_amount"),
                "stage": item.get("Статус") or item.get("stage"),
                "matching_method": "inn_exact",
                "confidence": "high",
            }
        )
    total = int(data.get("ЗапВсего") or data.get("total") or len(cases))
    total_pages = int(data.get("СтрВсего") or data.get("total_pages") or ((total + limit - 1) // limit if total else 0))
    current_page = int(data.get("СтрТекущ") or data.get("page") or page)
    return {
        "cases": cases,
        "total_count": total,
        "total_pages": total_pages,
        "current_page": current_page,
        "loaded_count": len(cases),
        "is_full_period_loaded": total_pages == 0 or current_page >= total_pages,
    }


class CheckoArbitrationProvider:
    code = "checko_legal_cases"

    def __init__(self, api_key: str | None = None, client=None):
        self.api_key = api_key or os.getenv("CHECKO_API_KEY")
        self.client = client or httpx.Client(timeout=30, follow_redirects=True, http2=False)

    def search_company(self, *, inn: str, date_from: date, date_to: date, page: int, limit: int = 100) -> dict:
        if not re.fullmatch(r"\d{10}|\d{12}", str(inn or "")):
            raise ValueError("Некорректный ИНН")
        if not self.api_key:
            raise ArbitrationCourtProviderError(kind="access_pending", message="CHECKO_API_KEY не настроен; бесплатная регистрация не выполнена")
        if page < 1 or limit != 100:
            raise ValueError("Court v1 загружает ровно одну страницу по 100 записей")
        params = {
            "key": self.api_key,
            "inn": inn,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "limit": 100,
            "page": page,
        }
        try:
            response = self.client.get(CHECKO_URL, params=params)
        except httpx.TimeoutException as error:
            raise ArbitrationCourtProviderError(kind="timeout", message="Превышено время ожидания Checko") from error
        except httpx.RequestError as error:
            raise ArbitrationCourtProviderError(kind="network_error", message="Сетевая ошибка Checko") from error
        if response.status_code != 200:
            raise ArbitrationCourtProviderError(kind="http_error", message=f"Checko вернул HTTP {response.status_code}", http_status=response.status_code)
        return {**parse_checko_legal_cases(response.json(), page=page, limit=100), "source_url": str(response.url), "http_status": 200}
