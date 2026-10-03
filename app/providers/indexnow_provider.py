from urllib.parse import urlparse

import httpx


INDEXNOW_URL = "https://yandex.com/indexnow"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_URLS_PER_REQUEST = 10_000


class IndexNowProviderError(Exception):
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


class IndexNowProvider:
    def __init__(
        self,
        *,
        key: str,
        key_location: str | None = None,
        client=None,
    ):
        key = (key or "").strip()
        if not 8 <= len(key) <= 128:
            raise ValueError("IndexNow key must contain 8-128 characters")

        self.key = key
        self.key_location = (key_location or "").strip() or None
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

    @staticmethod
    def _validate_urls(urls: list[str]) -> tuple[str, list[str]]:
        if not urls:
            raise ValueError("At least one URL is required")
        if len(urls) > MAX_URLS_PER_REQUEST:
            raise ValueError("IndexNow accepts at most 10000 URLs per request")

        cleaned = []
        host = None

        for raw_url in urls:
            url = str(raw_url).strip()
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(f"Invalid URL: {url}")

            if host is None:
                host = parsed.netloc
            elif parsed.netloc != host:
                raise ValueError("All IndexNow URLs must use the same host")

            cleaned.append(url)

        return host or "", cleaned

    def submit(self, urls: list[str]) -> dict:
        host, cleaned_urls = self._validate_urls(urls)
        payload = {
            "host": host,
            "key": self.key,
            "urlList": cleaned_urls,
        }
        if self.key_location:
            payload["keyLocation"] = self.key_location

        client, should_close = self._client()
        try:
            try:
                response = client.post(
                    INDEXNOW_URL,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
            except httpx.TimeoutException as error:
                raise IndexNowProviderError(
                    kind="timeout",
                    message="IndexNow: request timed out",
                ) from error
            except httpx.RequestError as error:
                raise IndexNowProviderError(
                    kind="network_error",
                    message="IndexNow: network request failed",
                ) from error

            if response.status_code in (200, 202):
                return {
                    "status": "accepted",
                    "http_status": response.status_code,
                    "submitted": len(cleaned_urls),
                    "host": host,
                }

            if response.status_code == 403:
                kind = "invalid_key"
            elif response.status_code == 429:
                kind = "rate_limited"
            elif response.status_code == 422:
                kind = "validation_error"
            elif response.status_code >= 500:
                kind = "service_unavailable"
            else:
                kind = "http_error"

            raise IndexNowProviderError(
                kind=kind,
                message=f"IndexNow: HTTP {response.status_code}",
                http_status=response.status_code,
            )
        finally:
            if should_close:
                client.close()
