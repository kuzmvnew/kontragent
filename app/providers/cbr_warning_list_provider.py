import json

import httpx


FULL_LIST_JSON_URL = (
    "https://www.cbr.ru/inside/warning-list/black-list-json"
)
API_DOC_URL = "https://www.cbr.ru/development/warning-list/"
REQUEST_TIMEOUT_SECONDS = 60.0


class CbrWarningListProviderError(Exception):
    def __init__(
        self,
        *,
        kind: str,
        message: str,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status


class CbrWarningListProvider:
    """
    Загрузчик официального полного JSON Банка России.

    Используется именно файл, предназначенный Банком России
    для автоматизированных систем. HTML-страницы не скрапятся.
    """

    def __init__(self, client=None):
        self._external_client = client

    def _client(self):
        if self._external_client is not None:
            return self._external_client, False

        return (
            httpx.Client(
                timeout=httpx.Timeout(
                    REQUEST_TIMEOUT_SECONDS
                ),
                follow_redirects=True,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Kontragent/1.0 official-data-ingestion",
                },
            ),
            True,
        )

    def fetch_full_list(self) -> dict:
        client, should_close = self._client()

        try:
            try:
                response = client.get(
                    FULL_LIST_JSON_URL,
                )
            except httpx.TimeoutException as error:
                raise CbrWarningListProviderError(
                    kind="timeout",
                    message=(
                        "Банк России: превышено время ожидания "
                        "предупредительного списка"
                    ),
                ) from error
            except httpx.RequestError as error:
                raise CbrWarningListProviderError(
                    kind="network_error",
                    message=(
                        "Банк России: ошибка сетевого запроса "
                        "предупредительного списка"
                    ),
                ) from error

            if response.status_code != 200:
                kind = (
                    "service_unavailable"
                    if response.status_code >= 500
                    else "http_error"
                )

                raise CbrWarningListProviderError(
                    kind=kind,
                    message=(
                        "Банк России вернул HTTP "
                        f"{response.status_code}"
                    ),
                    http_status=response.status_code,
                )

            raw_content = response.content

            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as error:
                raise CbrWarningListProviderError(
                    kind="invalid_response",
                    message=(
                        "Банк России вернул некорректный JSON "
                        "предупредительного списка"
                    ),
                    http_status=response.status_code,
                ) from error

            if not isinstance(payload, (list, dict)):
                raise CbrWarningListProviderError(
                    kind="invalid_response",
                    message=(
                        "Неожиданный формат JSON "
                        "предупредительного списка Банка России"
                    ),
                    http_status=response.status_code,
                )

            return {
                "payload": payload,
                "raw_content": raw_content,
                "http_status": response.status_code,
                "source_url": FULL_LIST_JSON_URL,
            }

        finally:
            if should_close:
                client.close()
