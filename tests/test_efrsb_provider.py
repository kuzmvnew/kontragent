from datetime import datetime, timezone

import pytest

from app.contracts.source_architecture import NormalizedResultStatus
from app.providers.efrsb_provider import EFRSB_PRODUCTION_BASE_URL, EfrsbProviderError, EfrsbRestProvider
from app.sources.direct_runners import EfrsbDirectRunner


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_efrsb_requires_production_credentials_without_network(monkeypatch):
    monkeypatch.delenv("EFRSB_API_LOGIN", raising=False)
    monkeypatch.delenv("EFRSB_API_PASSWORD", raising=False)
    provider = EfrsbRestProvider()
    assert provider.configured is False
    assert provider.client is None
    with pytest.raises(EfrsbProviderError) as error:
        provider.search_company(inn="6320002223")
    assert error.value.kind == "access_pending"


def test_efrsb_exact_inn_flow_uses_bearer_and_preserves_event_fields():
    class Client:
        def __init__(self): self.calls = []

        def post(self, url, json):
            self.calls.append(("POST", url, json, None))
            return Response({"jwt": "secret-jwt"})

        def get(self, url, params, headers):
            self.calls.append(("GET", url, params, headers))
            if url.endswith("/v1/bankrupts"):
                return Response({
                    "total": 2,
                    "pageData": [
                        {"guid": "debtor-1", "type": "Company", "data": {
                            "name": "ООО ТЕСТ", "inn": "6320002223", "ogrn": "1026301983113",
                        }},
                        {"guid": "wrong", "type": "Company", "data": {
                            "name": "ДРУГАЯ", "inn": "7700000000",
                        }},
                    ],
                })
            return Response({
                "total": 1,
                "pageData": [{
                    "guid": "message-1", "number": "1234567",
                    "datePublish": "2026-09-17T12:00:00", "type": "ArbitralDecree",
                }],
            })

    client = Client()
    provider = EfrsbRestProvider(login="login", password="password", client=client)
    result = provider.search_company(inn="6320002223")
    assert result["exact_identifier_match"] is True
    assert [debtor["inn"] for debtor in result["debtors"]] == ["6320002223"]
    assert result["events"][0] == {
        "debtor": "ООО ТЕСТ", "debtor_inn": "6320002223",
        "case_number": None, "procedure": None, "status": None,
        "publication_number": "1234567",
        "publication_date": "2026-09-17T12:00:00", "event_date": None,
        "message_type": "ArbitralDecree", "source_identifier": "message-1",
        "source_url": "https://fedresurs.ru/bankruptmessages/message-1",
    }
    assert client.calls[0][1] == EFRSB_PRODUCTION_BASE_URL + "/v1/auth"
    assert client.calls[0][2] == {"login": "login", "password": "password"}
    assert client.calls[1][2]["inn"] == "6320002223"
    assert client.calls[2][2]["bankruptGUID"] == "debtor-1"
    assert client.calls[1][3] == {"Authorization": "Bearer secret-jwt"}
    assert all("secret-jwt" not in call[1] for call in client.calls)


def test_efrsb_runner_does_not_invent_active_procedure():
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    result = EfrsbDirectRunner(lambda **_: {
        "exact_identifier_match": True,
        "debtors": [{"guid": "debtor-1", "inn": "6320002223"}],
        "events": [], "coverage_complete": True,
        "source_as_of": now,
    }).run("6320002223", checked_at=now)
    assert result.result == NormalizedResultStatus.PARTIAL
    assert result.coverage == .5
    assert "не подтверждены" in result.limitation


def test_efrsb_mismatched_nonempty_search_cannot_become_not_found():
    class Client:
        def post(self, _url, json): return Response({"jwt": "secret-jwt"})
        def get(self, url, params, headers):
            assert url.endswith("/v1/bankrupts")
            return Response({
                "total": 1,
                "pageData": [{
                    "guid": "wrong", "type": "Company",
                    "data": {"name": "ДРУГАЯ", "inn": "7700000000"},
                }],
            })

    provider = EfrsbRestProvider(login="login", password="password", client=Client())
    raw = provider.search_company(inn="6320002223")
    assert raw["exact_identifier_match"] is False
    result = EfrsbDirectRunner(lambda **_: raw).run("6320002223")
    assert result.result == NormalizedResultStatus.UNAVAILABLE


def test_efrsb_runner_preserves_publication_and_event_dates_separately():
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    result = EfrsbDirectRunner(lambda **_: {
        "exact_identifier_match": True,
        "debtors": [{"guid": "debtor-1", "inn": "6320002223"}],
        "events": [{
            "debtor": "ООО ТЕСТ", "debtor_inn": "6320002223",
            "publication_number": "123", "publication_date": "2026-09-17T12:00:00",
            "event_date": "2026-09-16T10:00:00", "message_type": "ArbitralDecree",
            "source_identifier": "message-1",
        }],
        "coverage_complete": True, "source_as_of": now,
    }).run("6320002223", checked_at=now)
    event = result.evidence[0].value["events"][0]
    assert result.result == NormalizedResultStatus.FOUND
    assert event["publication_date"] == "2026-09-17T12:00:00"
    assert event["event_date"] == "2026-09-16T10:00:00"
