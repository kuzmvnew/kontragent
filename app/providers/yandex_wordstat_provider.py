import httpx


BASE_URL = "https://searchapi.api.cloud.yandex.net/v2/wordstat"
REQUEST_TIMEOUT_SECONDS = 30.0


class YandexWordstatProviderError(Exception):
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


class YandexWordstatProvider:
    """Thin REST client for Yandex Search API / Wordstat."""

    def __init__(
        self,
        *,
        api_key: str,
        folder_id: str | None = None,
        client=None,
    ):
        api_key = (api_key or "").strip()
        if not api_key:
            raise ValueError("api_key is required")

        self.api_key = api_key
        self.folder_id = (folder_id or "").strip() or None
        self._external_client = client

    def _client(self):
        if self._external_client is not None:
            return self._external_client, False

        return (
            httpx.Client(
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            ),
            True,
        )

    def _post(self, path: str, payload: dict) -> dict:
        client, should_close = self._client()

        try:
            try:
                response = client.post(
                    f"{BASE_URL}/{path}",
                    headers={
                        "Authorization": f"Api-Key {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.TimeoutException as error:
                raise YandexWordstatProviderError(
                    kind="timeout",
                    message="Wordstat: request timed out",
                ) from error
            except httpx.RequestError as error:
                raise YandexWordstatProviderError(
                    kind="network_error",
                    message="Wordstat: network request failed",
                ) from error

            try:
                data = response.json()
            except ValueError as error:
                raise YandexWordstatProviderError(
                    kind="invalid_response",
                    message="Wordstat returned invalid JSON",
                    http_status=response.status_code,
                ) from error

            if response.status_code == 200:
                if not isinstance(data, dict):
                    raise YandexWordstatProviderError(
                        kind="invalid_response",
                        message="Wordstat returned unexpected payload",
                        http_status=response.status_code,
                    )
                return data

            message = (
                data.get("message")
                if isinstance(data, dict)
                else None
            ) or f"Wordstat: HTTP {response.status_code}"

            if response.status_code in (401, 403):
                kind = "auth_error"
            elif response.status_code == 429:
                kind = "rate_limited"
            elif response.status_code >= 500:
                kind = "service_unavailable"
            else:
                kind = "http_error"

            raise YandexWordstatProviderError(
                kind=kind,
                message=message,
                http_status=response.status_code,
            )
        finally:
            if should_close:
                client.close()

    def _base_payload(
        self,
        *,
        regions: list[str] | None,
        devices: list[str] | None,
    ) -> dict:
        payload = {
            "regions": regions or [],
            "devices": devices or ["DEVICE_ALL"],
        }
        if self.folder_id:
            payload["folderId"] = self.folder_id
        return payload

    @staticmethod
    def _normalize_phrase_rows(rows) -> list[dict]:
        result = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            phrase = str(row.get("phrase") or "").strip()
            if not phrase:
                continue
            try:
                count = int(row.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            result.append({"phrase": phrase, "count": count})
        return result

    def get_top(
        self,
        *,
        phrase: str,
        num_phrases: int = 100,
        regions: list[str] | None = None,
        devices: list[str] | None = None,
    ) -> dict:
        phrase = (phrase or "").strip()
        if not phrase:
            raise ValueError("phrase is required")
        if len(phrase) > 400:
            raise ValueError("phrase must be <= 400 characters")
        if not 1 <= int(num_phrases) <= 2000:
            raise ValueError("num_phrases must be between 1 and 2000")

        payload = self._base_payload(
            regions=regions,
            devices=devices,
        )
        payload.update(
            {
                "phrase": phrase,
                "numPhrases": int(num_phrases),
            }
        )

        data = self._post("topRequests", payload)
        try:
            total_count = int(data.get("totalCount") or 0)
        except (TypeError, ValueError):
            total_count = 0

        return {
            "phrase": phrase,
            "total_count": total_count,
            "results": self._normalize_phrase_rows(
                data.get("results")
            ),
            "associations": self._normalize_phrase_rows(
                data.get("associations")
            ),
        }

    def get_regions_tree(self) -> dict:
        payload = {}
        if self.folder_id:
            payload["folderId"] = self.folder_id
        return self._post("getRegionsTree", payload)

    def get_dynamics(
        self,
        *,
        phrase: str,
        period: str,
        from_date: str,
        to_date: str,
        regions: list[str] | None = None,
        devices: list[str] | None = None,
    ) -> dict:
        phrase = (phrase or "").strip()
        if not phrase:
            raise ValueError("phrase is required")

        payload = self._base_payload(
            regions=regions,
            devices=devices,
        )
        payload.update(
            {
                "phrase": phrase,
                "period": period,
                "fromDate": from_date,
                "toDate": to_date,
            }
        )
        return self._post("dynamics", payload)
