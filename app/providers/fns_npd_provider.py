from datetime import date

import httpx


API_URL = (
    "https://statusnpd.nalog.ru/"
    "api/v1/tracker/taxpayer_status"
)

REQUEST_TIMEOUT_SECONDS = 65.0


class FnsNpdProviderError(Exception):
    def __init__(
        self,
        *,
        kind,
        message,
        http_status=None,
        code=None,
    ):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status
        self.code = code


class FnsNpdProvider:
    def __init__(self, client=None):
        self._external_client = client

    def _client(self):
        if self._external_client is not None:
            return self._external_client, False

        return (
            httpx.Client(
                timeout=httpx.Timeout(
                    REQUEST_TIMEOUT_SECONDS
                )
            ),
            True,
        )

    def check_status(
        self,
        *,
        inn: str,
        request_date: date,
    ) -> dict:
        client, should_close = self._client()

        try:
            try:
                response = client.post(
                    API_URL,
                    json={
                        "inn": str(inn),
                        "requestDate": (
                            request_date.isoformat()
                        ),
                    },
                )
            except httpx.TimeoutException as error:
                raise FnsNpdProviderError(
                    kind="timeout",
                    message="ФНС НПД: превышено время ожидания ответа",
                ) from error
            except httpx.RequestError as error:
                raise FnsNpdProviderError(
                    kind="network_error",
                    message=(
                        "ФНС НПД: ошибка сетевого запроса"
                    ),
                ) from error

            try:
                payload = response.json()
            except ValueError as error:
                raise FnsNpdProviderError(
                    kind="invalid_response",
                    message=(
                        "ФНС НПД вернул некорректный JSON"
                    ),
                    http_status=response.status_code,
                ) from error

            if response.status_code == 200:
                status = payload.get("status")

                if not isinstance(status, bool):
                    raise FnsNpdProviderError(
                        kind="invalid_response",
                        message=(
                            "ФНС НПД не вернул boolean status"
                        ),
                        http_status=response.status_code,
                    )

                return {
                    "is_npd": status,
                    "message": payload.get("message"),
                    "http_status": response.status_code,
                }

            code = payload.get("code")
            message = (
                payload.get("message")
                or f"ФНС НПД: HTTP {response.status_code}"
            )

            if response.status_code == 422:
                if code == (
                    "taxpayer.status.service.limited.error"
                ):
                    kind = "rate_limited"
                elif code == (
                    "taxpayer.status.service.unavailable.error"
                ):
                    kind = "service_unavailable"
                else:
                    kind = "validation_error"

                raise FnsNpdProviderError(
                    kind=kind,
                    message=message,
                    http_status=response.status_code,
                    code=code,
                )

            if response.status_code >= 500:
                raise FnsNpdProviderError(
                    kind="service_unavailable",
                    message=message,
                    http_status=response.status_code,
                    code=code,
                )

            raise FnsNpdProviderError(
                kind="http_error",
                message=message,
                http_status=response.status_code,
                code=code,
            )

        finally:
            if should_close:
                client.close()
