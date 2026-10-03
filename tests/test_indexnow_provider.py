import httpx

from app.providers.indexnow_provider import (
    IndexNowProvider,
    IndexNowProviderError,
)


def test_indexnow_submits_same_host_urls():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(202)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = IndexNowProvider(
        key="12345678testkey",
        client=client,
    )

    result = provider.submit(
        [
            "https://example.ru/company/a-1",
            "https://example.ru/company/b-2",
        ]
    )

    assert result["status"] == "accepted"
    assert result["submitted"] == 2
    assert result["host"] == "example.ru"
    assert captured["path"] == "/indexnow"
    assert '"host":"example.ru"' in captured["body"]
    client.close()


def test_indexnow_rejects_mixed_hosts():
    provider = IndexNowProvider(key="12345678testkey")

    try:
        provider.submit(
            [
                "https://a.example.ru/company/a",
                "https://b.example.ru/company/b",
            ]
        )
        raise AssertionError("Expected ValueError")
    except ValueError:
        pass


def test_indexnow_maps_invalid_key():
    def handler(request):
        return httpx.Response(403)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = IndexNowProvider(key="12345678testkey", client=client)

    try:
        provider.submit(["https://example.ru/company/a"])
        raise AssertionError("Expected IndexNowProviderError")
    except IndexNowProviderError as error:
        assert error.kind == "invalid_key"
        assert error.http_status == 403
    finally:
        client.close()
