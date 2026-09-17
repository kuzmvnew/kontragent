from __future__ import annotations

from datetime import datetime
import re

import httpx

from app.providers.nostroy_provider import NostroyProviderError


NOPRIZ_NRS_API_URL = "https://nrs.nopriz.ru/api/specialist/list"
NOPRIZ_NRS_URL = "https://nrs.nopriz.ru/"
NOSTROY_NRS_URL = "https://nrs.nostroy.ru/"
_REGISTRATION_NUMBER = re.compile(r"[А-ЯA-Z]{1,3}-\d{6}")


def _date(value):
    if not value:
        return None
    clean = str(value).replace("*", "").strip()
    try:
        return datetime.strptime(clean, "%d.%m.%Y").date()
    except ValueError as error:
        raise NostroyProviderError(kind="invalid_response", message="Некорректная дата НРС") from error


def parse_nopriz_nrs_search(payload: dict, registration_number: str) -> dict:
    if not _REGISTRATION_NUMBER.fullmatch(str(registration_number or "")):
        raise ValueError("Требуется точный регистрационный номер специалиста")
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise NostroyProviderError(kind="invalid_response", message="НРС НОПРИЗ не подтвердил успешный ответ")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise NostroyProviderError(kind="invalid_response", message="Неожиданная структура НРС НОПРИЗ")
    rows, count = data["data"], data.get("count")
    if not isinstance(count, int) or count != len(rows):
        raise NostroyProviderError(kind="invalid_response", message="Счётчик НРС НОПРИЗ не согласован со строками")
    records = []
    for row in rows:
        if row.get("registrationNumber") != registration_number:
            raise NostroyProviderError(kind="identity_mismatch", message="НРС НОПРИЗ вернул не exact registration number")
        work_types = row.get("workTypes") or {}
        if not isinstance(work_types, dict):
            raise NostroyProviderError(kind="invalid_response", message="Некорректные виды работ НРС НОПРИЗ")
        statuses = {code: {"status_code": value.get("statusCode"), "status": value.get("statusTitle"), "valid_to": _date(value.get("exclusionDate")).isoformat() if value.get("exclusionDate") else None} for code, value in work_types.items()}
        records.append(
            {
                "source_record_id": str(row.get("id")),
                "registration_number": registration_number,
                "person_name": row.get("fio"),
                "professional_status": statuses,
                "valid_from": _date(row.get("inclusionProtocolDate")),
                "status_date": _date(row.get("changesDate")),
            }
        )
    return {"found": bool(records), "records": records, "total": count}


class NoprizNrsProvider:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=45, follow_redirects=True, http2=False, headers={"User-Agent": "Mozilla/5.0 (compatible; Kontragent/1.0; low-load exact-record lookup)", "Referer": NOPRIZ_NRS_URL, "Accept": "application/json"})

    def check_registration_number(self, registration_number: str) -> dict:
        request = {"filters": {}, "searchString": registration_number, "page": 1, "pageCount": "20", "sortBy": {}}
        try:
            response = self.client.post(NOPRIZ_NRS_API_URL, json=request)
        except httpx.TimeoutException as error:
            raise NostroyProviderError(kind="timeout", message="Превышено время ожидания НРС НОПРИЗ") from error
        except httpx.RequestError as error:
            raise NostroyProviderError(kind="network_error", message="Сетевая ошибка НРС НОПРИЗ") from error
        if response.status_code in {403, 429}:
            raise NostroyProviderError(kind="source_protection", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        if response.status_code != 200:
            raise NostroyProviderError(kind="http_error", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        try:
            parsed = parse_nopriz_nrs_search(response.json(), registration_number)
        except ValueError as error:
            if isinstance(error, NostroyProviderError):
                raise
            raise NostroyProviderError(kind="invalid_response", message="НРС НОПРИЗ вернул не JSON") from error
        return {**parsed, "http_status": 200}


class NostroyNrsProvider:
    """Records the official access boundary without decoding protected images."""

    def check_registration_number(self, registration_number: str) -> dict:
        if not _REGISTRATION_NUMBER.fullmatch(str(registration_number or "")):
            raise ValueError("Требуется точный регистрационный номер специалиста")
        raise NostroyProviderError(
            kind="source_protection",
            message="НРС НОСТРОЙ публикует ID и ФИО изображениями; автоматическое декодирование не выполняется",
        )
