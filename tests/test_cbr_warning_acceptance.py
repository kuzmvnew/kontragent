from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from app.ingestion.cbr_warning_list import (
    normalize_inn, parse_cbr_warning_payload, validate_cbr_warning_snapshot,
)
from app.providers.cbr_warning_list_provider import CbrWarningListProvider, CbrWarningListProviderError
from app.services import cbr_warning_list_service as service
from scripts import sync_cbr_warning_list as sync


ROW = {"id": 1, "nameOrg": "SYNTHETIC TEST ONLY", "inn": "7701234567", "dt": "16.09.2026"}


@pytest.mark.parametrize("bad", [None, "", "INN7701234567", "770123 4567", "7701234567/1", "７７０１２３４５６７"])
def test_invalid_identifier_is_not_silently_repaired(bad):
    assert normalize_inn(bad) is None
    result = service.get_cbr_warning_list_check_for_inn(bad)
    assert result["result"] == "unavailable"
    assert result["checked"] is False
    assert result["is_listed"] is None
    assert result["record_count"] is None


def test_non_object_rows_are_counted_and_block_publication():
    parsed = parse_cbr_warning_payload([ROW, "broken"], data_date=date(2026, 9, 16))
    assert parsed["source_records"] == 2
    assert parsed["rejected_records"] == 1
    with pytest.raises(ValueError):
        validate_cbr_warning_snapshot(parsed)


def test_conflicting_duplicate_ids_block_publication():
    parsed = parse_cbr_warning_payload([ROW, {**ROW, "inn": "7812345678"}], data_date=date(2026, 9, 16))
    assert parsed["conflicting_duplicates"] == 1
    with pytest.raises(ValueError):
        validate_cbr_warning_snapshot(parsed)


def test_identical_duplicate_is_explicitly_counted():
    parsed = parse_cbr_warning_payload([ROW, ROW], data_date=date(2026, 9, 16))
    validate_cbr_warning_snapshot(parsed)
    assert parsed["source_records"] == parsed["imported_records"] + parsed["duplicate_records"] == 2


@pytest.mark.parametrize("payload", [[], {"Data": []}, [{"error": "unavailable"}]])
def test_empty_or_invalid_snapshot_is_never_published(payload):
    parsed = parse_cbr_warning_payload(payload, data_date=date(2026, 9, 16))
    with pytest.raises(ValueError):
        validate_cbr_warning_snapshot(parsed)


class Result:
    def __init__(self, scalar):
        self.scalar = scalar
    def scalar_one_or_none(self):
        return self.scalar
    def scalar_one(self):
        return self.scalar


class Session:
    def __init__(self, *values):
        self.values = iter(values)
        self.closed = False
    def execute(self, statement):
        return Result(next(self.values))
    def close(self):
        self.closed = True


@pytest.mark.parametrize("dataset, count, reason", [
    (None, None, "dataset_not_registered"),
    (SimpleNamespace(id=1, enabled=False, last_data_date=date(2026, 9, 16)), None, "dataset_disabled"),
    (SimpleNamespace(id=1, enabled=True, last_data_date=None), None, "dataset_not_loaded"),
    (SimpleNamespace(id=1, enabled=True, last_data_date=date(2026, 9, 16), operational_status="current", official_actual_until=date.today()), 0, "dataset_snapshot_missing"),
])
def test_unavailable_is_not_a_negative_result(monkeypatch, dataset, count, reason):
    session = Session(dataset, count)
    monkeypatch.setattr(service, "get_session", lambda: session)
    check = service.get_cbr_warning_list_check_for_inn("7701234567")
    assert check["result"] == "unavailable"
    assert check["reason"] == reason
    assert check["checked"] is False
    assert check["is_listed"] is None
    assert session.closed


def test_stale_snapshot_cannot_return_clean_negative(monkeypatch):
    dataset = SimpleNamespace(
        id=1,
        enabled=True,
        last_data_date=date(2026, 9, 16),
        operational_status="current",
        official_actual_until=date(2020, 1, 1),
    )
    session = Session(dataset)
    monkeypatch.setattr(service, "get_session", lambda: session)

    check = service.get_cbr_warning_list_check_for_inn("7701234567")

    assert check["result"] == "unavailable"
    assert check["checked"] is False
    assert check["is_listed"] is None
    assert check["reason"] == "dataset_stale"


def test_database_connection_error_is_not_not_found(monkeypatch):
    def broken():
        raise OperationalError("secret sql", {}, Exception("secret dsn"))
    monkeypatch.setattr(service, "get_session", broken)
    check = service.get_cbr_warning_list_check_for_inn("7701234567")
    assert check["result"] == "unavailable"
    assert check["reason"] == "storage_error"
    assert check["is_listed"] is None
    assert "secret" not in str(check)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_failure_never_becomes_empty_list(status):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=[])))
    with client, pytest.raises(CbrWarningListProviderError) as caught:
        CbrWarningListProvider(client=client).fetch_full_list()
    assert caught.value.http_status == status


def test_timeout_is_an_explicit_provider_error():
    def handler(request):
        raise httpx.ReadTimeout("test timeout", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CbrWarningListProviderError) as caught:
            CbrWarningListProvider(client=client).fetch_full_list()
    assert caught.value.kind == "timeout"


def test_html_with_http_200_is_not_a_valid_list():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>error</html>"))) as client:
        with pytest.raises(CbrWarningListProviderError) as caught:
            CbrWarningListProvider(client=client).fetch_full_list()
    assert caught.value.kind == "invalid_response"


def test_fetch_failure_is_in_ledger_before_any_publish(monkeypatch):
    events = []
    monkeypatch.setattr(sync, "ensure_cbr_warning_list_dataset", lambda: None)
    monkeypatch.setattr(sync, "get_dataset_id", lambda: 1)
    monkeypatch.setattr(sync, "start_ingestion", lambda **kwargs: events.append("start") or 2)
    monkeypatch.setattr(sync, "finish_ingestion_failure", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(sync, "replace_cbr_warning_list", lambda *a, **k: pytest.fail("must not publish"))
    class BrokenProvider:
        def fetch_full_list(self):
            assert events == ["start"]
            raise CbrWarningListProviderError(kind="timeout", message="synthetic timeout")
    with pytest.raises(CbrWarningListProviderError):
        sync.sync_cbr_warning_list(provider=BrokenProvider())
    assert events[1]["run_id"] == 2
    assert events[1]["details"]["error_kind"] == "timeout"
