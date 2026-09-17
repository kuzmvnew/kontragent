from __future__ import annotations

from datetime import datetime
import re

import httpx


NOSTROY_API_URL = "https://reestr.nostroy.ru/api/sro/all/member/list"
NOSTROY_REGISTRY_URL = "https://reestr.nostroy.ru/sro/all/member/list"
_INN10 = re.compile(r"\d{10}")
_OGRN13 = re.compile(r"\d{13}")


class NostroyProviderError(Exception):
    def __init__(self, *, kind: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status


def _date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError:
        raise NostroyProviderError(kind="invalid_response", message="Некорректная дата в ответе НОСТРОЙ")


def parse_member_search(payload: dict, inn: str) -> dict:
    """Validate an exact-INN response and expose company-safe fields only."""
    if not _INN10.fullmatch(str(inn or "")):
        raise ValueError("Для company-проверки требуется 10-значный ИНН")
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise NostroyProviderError(kind="invalid_response", message="НОСТРОЙ не подтвердил успешный ответ")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise NostroyProviderError(kind="invalid_response", message="Неожиданная структура ответа НОСТРОЙ")
    rows = data["data"]
    count = data.get("count")
    if not isinstance(count, int) or count < 0 or count != len(rows):
        raise NostroyProviderError(kind="invalid_response", message="Счётчик ответа НОСТРОЙ не согласован со строками")

    records = []
    for row in rows:
        if not isinstance(row, dict) or row.get("inn") != inn:
            raise NostroyProviderError(kind="identity_mismatch", message="Поиск НОСТРОЙ вернул не exact ИНН")
        ogrn = row.get("ogrnip")
        if ogrn is not None and not _OGRN13.fullmatch(str(ogrn)):
            raise NostroyProviderError(kind="identity_mismatch", message="Запись юрлица содержит некорректный ОГРН")
        status = row.get("member_status") or {}
        sro = row.get("sro") or {}
        if not isinstance(status, dict) or not isinstance(sro, dict):
            raise NostroyProviderError(kind="invalid_response", message="Некорректные вложенные поля НОСТРОЙ")
        records.append(
            {
                "member_id": row.get("id"),
                "inn": inn,
                "ogrn": ogrn,
                "registration_number": row.get("registration_number"),
                "inventory_number": row.get("inventory_number"),
                "registry_registration_date": _date(row.get("registry_registration_date")),
                "member_status_code": status.get("code"),
                "member_status": status.get("title"),
                "sro_id": sro.get("id"),
                "sro_registration_number": sro.get("registration_number"),
                "sro_name": sro.get("full_description"),
                "last_updated_at": row.get("last_updated_at_date_time_string") or None,
            }
        )
    return {"found": bool(records), "records": records, "total": count}


class NostroyMemberProvider:
    def __init__(self, client=None):
        self.client = client or httpx.Client(
            timeout=45,
            follow_redirects=True,
            http2=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Kontragent/1.0; low-load exact-INN lookup)",
                "Referer": NOSTROY_REGISTRY_URL,
                "Accept": "application/json",
            },
        )

    def check_inn(self, inn: str) -> dict:
        if not _INN10.fullmatch(str(inn or "")):
            raise ValueError("Для company-проверки требуется 10-значный ИНН")
        request = {"filters": {}, "searchString": inn, "page": 1, "pageCount": 20, "sortBy": {}}
        try:
            response = self.client.post(NOSTROY_API_URL, json=request)
        except httpx.TimeoutException as error:
            raise NostroyProviderError(kind="timeout", message="Превышено время ожидания НОСТРОЙ") from error
        except httpx.RequestError as error:
            raise NostroyProviderError(kind="network_error", message="Сетевая ошибка НОСТРОЙ") from error
        if response.status_code in {403, 429}:
            raise NostroyProviderError(kind="source_protection", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        if response.status_code != 200:
            raise NostroyProviderError(kind="http_error", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise NostroyProviderError(kind="invalid_response", message="НОСТРОЙ вернул не JSON") from error
        parsed = parse_member_search(payload, inn)
        return {**parsed, "http_status": 200}
