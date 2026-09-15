from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.aggregators import company_product_aggregator
from app.providers.fns_npd_provider import (
    API_URL,
    FnsNpdProvider,
    FnsNpdProviderError,
)
from app.services import npd_service


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, json):
        self.calls.append((url, json))
        return self.response


class SuccessProvider:
    def check_status(self, *, inn, request_date):
        return {
            "is_npd": True,
            "message": "Статус подтверждён",
            "http_status": 200,
        }


class LimitedProvider:
    def check_status(self, *, inn, request_date):
        raise FnsNpdProviderError(
            kind="rate_limited",
            message="Превышено количество запросов",
            http_status=422,
            code="taxpayer.status.service.limited.error",
        )


def make_row(
    *,
    inn="770123456789",
    request_date=date(2026, 9, 15),
    result_status="success",
    is_npd=True,
    message="Статус подтверждён",
    http_status=200,
    error_code=None,
):
    return SimpleNamespace(
        inn=inn,
        request_date=request_date,
        result_status=result_status,
        is_npd=is_npd,
        message=message,
        http_status=http_status,
        error_code=error_code,
        checked_at=datetime(
            2026,
            9,
            15,
            10,
            0,
            tzinfo=timezone.utc,
        ),
    )


def test_provider_success_contract():
    client = FakeClient(
        FakeResponse(
            200,
            {
                "status": True,
                "message": "ok",
            },
        )
    )

    provider = FnsNpdProvider(
        client=client
    )

    result = provider.check_status(
        inn="770123456789",
        request_date=date(2026, 9, 15),
    )

    assert result["is_npd"] is True
    assert result["http_status"] == 200
    assert client.calls == [
        (
            API_URL,
            {
                "inn": "770123456789",
                "requestDate": "2026-09-15",
            },
        )
    ]


def test_provider_maps_rate_limit():
    provider = FnsNpdProvider(
        client=FakeClient(
            FakeResponse(
                422,
                {
                    "code": (
                        "taxpayer.status.service.limited.error"
                    ),
                    "message": "Слишком много запросов",
                },
            )
        )
    )

    with pytest.raises(
        FnsNpdProviderError
    ) as exc_info:
        provider.check_status(
            inn="770123456789",
            request_date=date(2026, 9, 15),
        )

    assert exc_info.value.kind == "rate_limited"
    assert exc_info.value.http_status == 422


def test_legal_entity_is_not_applicable_without_db():
    result = npd_service.get_cached_npd_check_for_inn(
        "7701234567",
        request_date=date(2026, 9, 15),
    )

    assert result["result"] == "not_applicable"
    assert result["checked"] is True
    assert result["applicable"] is False


def test_refresh_success_returns_found(monkeypatch):
    row = make_row()

    monkeypatch.setattr(
        npd_service,
        "_save_attempt",
        lambda **kwargs: row,
    )

    result = npd_service.refresh_npd_check_for_inn(
        "770123456789",
        request_date=date(2026, 9, 15),
        provider=SuccessProvider(),
    )

    assert result["result"] == "found"
    assert result["is_npd"] is True
    assert result["cached"] is False


def test_refresh_rate_limit_is_unavailable(monkeypatch):
    row = make_row(
        result_status="error",
        is_npd=None,
        message="Превышено количество запросов",
        http_status=422,
        error_code=(
            "taxpayer.status.service.limited.error"
        ),
    )

    monkeypatch.setattr(
        npd_service,
        "_save_attempt",
        lambda **kwargs: row,
    )

    result = npd_service.refresh_npd_check_for_inn(
        "770123456789",
        request_date=date(2026, 9, 15),
        provider=LimitedProvider(),
    )

    assert result["result"] == "unavailable"
    assert result["checked"] is False
    assert result["http_status"] == 422


def test_product_aggregator_adds_cached_npd_source(
    monkeypatch,
):
    company = {
        "id": 1,
        "inn": "770123456789",
        "name": "ИП ТЕСТ",
        "sources_used": ["excel_import"],
    }

    npd_check = {
        "checked": True,
        "applicable": True,
        "result": "found",
        "data_date": date(2026, 9, 15),
        "dataset_code": "fns_npd",
        "source": "fns_npd",
        "reason": None,
    }

    monkeypatch.setattr(
        company_product_aggregator,
        "get_cached_npd_check_for_inn",
        lambda inn: npd_check,
    )

    result = (
        company_product_aggregator
        .enrich_company_with_npd(company)
    )

    assert result["npd_check"] == npd_check
    assert "fns_npd" in result["sources_used"]
    assert company["sources_used"] == [
        "excel_import"
    ]


def test_npd_template_compiles():
    from main import templates

    template = templates.get_template(
        "partials/npd.html"
    )

    assert template is not None
