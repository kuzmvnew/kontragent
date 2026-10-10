import httpx

from app.providers.yandex_wordstat_provider import (
    YandexWordstatProvider,
    YandexWordstatProviderError,
)


def test_wordstat_get_top_normalizes_response():
    def handler(request):
        assert request.headers["Authorization"] == "Api-Key test-key"
        assert request.url.path.endswith("/v2/wordstat/topRequests")
        payload = request.read().decode("utf-8")
        assert '"phrase":"ооо ромашка"' in payload
        return httpx.Response(
            200,
            json={
                "totalCount": "120",
                "results": [
                    {"phrase": "ооо ромашка инн", "count": "80"},
                ],
                "associations": [
                    {"phrase": "ромашка отзывы", "count": "40"},
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = YandexWordstatProvider(
        api_key="test-key",
        folder_id="folder-1",
        client=client,
    )

    result = provider.get_top(
        phrase="ооо ромашка",
        num_phrases=100,
    )

    assert result["total_count"] == 120
    assert result["results"][0]["count"] == 80
    assert result["associations"][0]["count"] == 40
    client.close()


def test_wordstat_maps_rate_limit():
    def handler(request):
        return httpx.Response(429, json={"message": "quota exceeded"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = YandexWordstatProvider(api_key="test-key", client=client)

    try:
        provider.get_top(phrase="ооо ромашка")
        raise AssertionError("Expected YandexWordstatProviderError")
    except YandexWordstatProviderError as error:
        assert error.kind == "rate_limited"
        assert error.http_status == 429
    finally:
        client.close()


def test_wordstat_validates_phrase_and_limit():
    provider = YandexWordstatProvider(api_key="test-key")

    try:
        provider.get_top(phrase="")
        raise AssertionError("Expected ValueError")
    except ValueError:
        pass

    try:
        provider.get_top(phrase="test", num_phrases=2001)
        raise AssertionError("Expected ValueError")
    except ValueError:
        pass
