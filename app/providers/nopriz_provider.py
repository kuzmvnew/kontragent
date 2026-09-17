from __future__ import annotations

import httpx

from app.providers.nostroy_provider import NostroyProviderError, parse_member_search


NOPRIZ_API_URL = "https://reestr.nopriz.ru/api/sro/all/member/list"
NOPRIZ_REGISTRY_URL = "https://reestr.nopriz.ru/sro/all/member/list"


class NoprizMemberProvider:
    """Low-load exact-INN adapter for the official NOPRIZ frontend API."""

    def __init__(self, client=None):
        self.client = client or httpx.Client(
            timeout=45,
            follow_redirects=True,
            http2=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Kontragent/1.0; low-load exact-INN lookup)",
                "Referer": NOPRIZ_REGISTRY_URL,
                "Accept": "application/json",
            },
        )

    def check_inn(self, inn: str) -> dict:
        request = {"filters": {}, "searchString": inn, "page": 1, "pageCount": 20, "sortBy": {}}
        try:
            response = self.client.post(NOPRIZ_API_URL, json=request)
        except httpx.TimeoutException as error:
            raise NostroyProviderError(kind="timeout", message="Превышено время ожидания НОПРИЗ") from error
        except httpx.RequestError as error:
            raise NostroyProviderError(kind="network_error", message="Сетевая ошибка НОПРИЗ") from error
        if response.status_code in {403, 429}:
            raise NostroyProviderError(kind="source_protection", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        if response.status_code != 200:
            raise NostroyProviderError(kind="http_error", message=f"Источник вернул HTTP {response.status_code}", http_status=response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise NostroyProviderError(kind="invalid_response", message="НОПРИЗ вернул не JSON") from error
        parsed = parse_member_search(payload, inn)
        return {**parsed, "http_status": 200}
