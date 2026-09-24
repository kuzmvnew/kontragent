from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import tax_debt_service
from app.services.tax_debt_freshness import (
    FRESH,
    STALE_DATA,
    TaxDebtPublicationContext,
    evaluate_tax_debt_freshness,
    resolve_tax_debt_publication,
)
from app.sources.fns_tax_debt import PILOT_ENVIRONMENT, SOURCE_ID

DATA_AS_OF = date(2026, 9, 1)
SOURCE_AS_OF = datetime(2026, 9, 20, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 9, 21, tzinfo=timezone.utc)
BOUNDARY = date(2026, 9, 30)


class FakeResult:
    def __init__(self, *, mapping=None, scalar=None):
        self.mapping = mapping
        self.scalar = scalar

    def mappings(self):
        return self

    def one_or_none(self):
        return self.mapping

    def scalar_one_or_none(self):
        return self.scalar


class FakeSession:
    def __init__(self, *results):
        self.results = list(results)
        self.closed = False
        self.execute_count = 0

    def execute(self, _statement):
        self.execute_count += 1
        if not self.results:
            raise AssertionError("unexpected read after freshness decision")
        return self.results.pop(0)

    def close(self):
        self.closed = True


def _context(*, actual_until=BOUNDARY, **changes):
    values = {
        "data_as_of": DATA_AS_OF,
        "publication_generation": 0,
        "source_as_of": SOURCE_AS_OF,
        "retrieved_at": RETRIEVED_AT,
        "official_actual_until": actual_until,
    }
    values.update(changes)
    return TaxDebtPublicationContext(**values)


def _company_result():
    return FakeResult(
        mapping={"id": 101, "inn": "7701234567", "entity_type": "legal"}
    )


def _dataset_result(
    *,
    data_as_of=DATA_AS_OF,
    actual_until=BOUNDARY,
    source_as_of=SOURCE_AS_OF,
    retrieved_at=RETRIEVED_AT,
):
    coverage = (
        {}
        if actual_until is None
        else {"official_actual_until": actual_until.isoformat()}
    )
    return FakeResult(
        scalar=SimpleNamespace(
            id=202,
            last_data_date=data_as_of,
            source_as_of=source_as_of,
            retrieved_at=retrieved_at,
            coverage=coverage,
        )
    )


def _snapshot(*, data_as_of=DATA_AS_OF):
    return SimpleNamespace(
        id=303,
        data_date=data_as_of,
        document_date=data_as_of,
        source_document_id="DEBT-1",
        total_arrears=Decimal("100.00"),
        total_penalties=Decimal("20.00"),
        total_fines=Decimal("5.00"),
        total_debt=Decimal("125.00"),
        item_count=0,
        source_reference="fixture://debt-1",
        provenance={"source_id": "S02"},
        limitation_states=["dated_snapshot"],
        retrieved_at=RETRIEVED_AT,
    )


def _read(monkeypatch, *, checked_at, dataset, snapshot):
    results = [_company_result(), dataset]
    if snapshot is not ...:
        results.append(FakeResult(scalar=snapshot))
    session = FakeSession(*results)
    monkeypatch.setattr(tax_debt_service, "get_session", lambda: session)
    monkeypatch.setattr(tax_debt_service, "utc_now", lambda: checked_at)
    return tax_debt_service.get_tax_debt_check_for_company(
        101,
        include_items=False,
    ), session


def test_official_actual_until_is_inclusive_and_expires_next_day():
    on_boundary = evaluate_tax_debt_freshness(
        _context(),
        checked_at=datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc),
    )
    after_boundary = evaluate_tax_debt_freshness(
        _context(),
        checked_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    assert on_boundary.state == FRESH
    assert after_boundary.state == STALE_DATA
    assert after_boundary.reason == "official_actual_until_expired"


@pytest.mark.parametrize(
    "missing_field",
    [
        "official_actual_until",
        "source_as_of",
        "data_as_of",
        "retrieved_at",
    ],
)
def test_missing_freshness_metadata_fails_closed(missing_field):
    freshness = evaluate_tax_debt_freshness(
        _context(**{missing_field: None}),
        checked_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )

    assert freshness.state == STALE_DATA
    assert freshness.reason == "freshness_metadata_missing"
    assert freshness.missing_fields == (missing_field,)


def test_active_generation_metadata_cannot_be_extended_by_newer_discovery():
    state = SimpleNamespace(
        source_id=SOURCE_ID,
        enabled=True,
        pilot_environment=PILOT_ENVIRONMENT,
        dataset_id=202,
        cohort_inns=["7701234567"],
        query_generation=7,
        baseline_generation=0,
        active_data_date=DATA_AS_OF,
        baseline_data_date=date(2026, 8, 1),
        official_actual_until=date(2026, 10, 31),
    )
    active_generation = SimpleNamespace(
        last_data_date=DATA_AS_OF,
        source_as_of=SOURCE_AS_OF,
        retrieved_at=RETRIEVED_AT,
        official_actual_until=BOUNDARY,
    )

    class PilotSession:
        def get(self, model, key):
            assert key == SOURCE_ID
            return state

        def scalar(self, _statement):
            return active_generation

    context = resolve_tax_debt_publication(
        PilotSession(),
        dataset=SimpleNamespace(
            id=202,
            last_data_date=date(2026, 8, 1),
            source_as_of=None,
            retrieved_at=None,
            coverage={},
        ),
        inn="7701234567",
    )
    freshness = evaluate_tax_debt_freshness(
        context,
        checked_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    assert context.publication_generation == 7
    assert context.official_actual_until == BOUNDARY
    assert freshness.state == STALE_DATA
    assert freshness.reason == "official_actual_until_expired"


def test_fresh_snapshot_found_stays_found(monkeypatch):
    result, session = _read(
        monkeypatch,
        checked_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        dataset=_dataset_result(),
        snapshot=_snapshot(),
    )

    assert result["state"] == "FOUND"
    assert result["result"] == "found"
    assert result["freshness"] == "fresh"
    assert result["official_actual_until"] == BOUNDARY
    assert session.execute_count == 3


def test_expired_found_snapshot_returns_stale_data_without_reading_fact(monkeypatch):
    result, session = _read(
        monkeypatch,
        checked_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        dataset=_dataset_result(),
        snapshot=_snapshot(),
    )

    assert result["state"] == STALE_DATA
    assert result["result"] == "unavailable"
    assert result["freshness"] == "stale"
    assert result["data_date"] == DATA_AS_OF
    assert result["source"] == "fns_tax_debt"
    assert result["checked_at"] == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert result["reason"] == "official_actual_until_expired"
    assert "official_actual_until_expired" in result["limitation_states"]
    assert session.execute_count == 2


def test_expired_not_found_snapshot_returns_stale_data(monkeypatch):
    fresh, _ = _read(
        monkeypatch,
        checked_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        dataset=_dataset_result(),
        snapshot=None,
    )
    stale, _ = _read(
        monkeypatch,
        checked_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        dataset=_dataset_result(),
        snapshot=None,
    )

    assert fresh["state"] == "NOT_FOUND"
    assert fresh["result"] == "not_found"
    assert stale["state"] == STALE_DATA
    assert stale["result"] == "unavailable"


def test_missing_metadata_returns_stale_data(monkeypatch):
    result, _ = _read(
        monkeypatch,
        checked_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
        dataset=_dataset_result(actual_until=None),
        snapshot=...,
    )

    assert result["state"] == STALE_DATA
    assert result["reason"] == "freshness_metadata_missing"
    assert result["freshness_missing_fields"] == ["official_actual_until"]


def test_new_fresh_snapshot_restores_found(monkeypatch):
    stale, _ = _read(
        monkeypatch,
        checked_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        dataset=_dataset_result(),
        snapshot=_snapshot(),
    )
    new_data_as_of = date(2026, 10, 1)
    fresh, _ = _read(
        monkeypatch,
        checked_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        dataset=_dataset_result(
            data_as_of=new_data_as_of,
            actual_until=date(2026, 10, 31),
            source_as_of=datetime(2026, 10, 1, 8, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 10, 1, 9, tzinfo=timezone.utc),
        ),
        snapshot=_snapshot(data_as_of=new_data_as_of),
    )

    assert stale["state"] == STALE_DATA
    assert fresh["state"] == "FOUND"
    assert fresh["result"] == "found"
    assert fresh["data_date"] == new_data_as_of
