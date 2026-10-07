from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.contracts.company_view_v1 import DataState
from app.services.fns_tax_regime_readiness import (
    evaluate_family_readiness,
    resolve_current_company_coverage,
)


NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)
SOURCE_DATE = date(2026, 9, 1)


def _datasets():
    values = {
        code: {
            "enabled": True,
            "operational_status": "current",
            "last_success_at": NOW,
            "last_data_date": SOURCE_DATE,
            "official_actual_until": date(2026, 10, 25),
            "freshness_policy": "irregular",
            "coverage": {"release_identity": f"{code}-release"},
        }
        for code in ("fns_tax_regime", "fns_snr", "fns_snrip")
    }
    values["fns_tax_regime"]["coverage"]["members"] = {
        "legal": {"release_identity": "fns_snr-release"},
        "ip": {"release_identity": "fns_snrip-release"},
    }
    return values


@pytest.mark.parametrize(
    ("code", "change", "expected"),
    [
        ("fns_tax_regime", {"operational_status": "stale"}, DataState.STALE_DATA),
        ("fns_snr", {"operational_status": "stale"}, DataState.STALE_DATA),
        ("fns_snrip", {"operational_status": "stale"}, DataState.STALE_DATA),
        ("fns_tax_regime", {"operational_status": "unavailable"}, DataState.SOURCE_UNAVAILABLE),
        ("fns_snr", {"operational_status": "unavailable"}, DataState.SOURCE_UNAVAILABLE),
        ("fns_snrip", {"operational_status": "unavailable"}, DataState.SOURCE_UNAVAILABLE),
        (
            "fns_snrip",
            {"operational_status": "error", "last_error": "XSD schema mismatch"},
            DataState.PARSING_ERROR,
        ),
        ("fns_snr", {"last_success_at": None}, DataState.NOT_CHECKED),
        ("fns_snrip", {"last_success_at": None}, DataState.NOT_CHECKED),
    ],
)
def test_mandatory_family_component_degrades_readiness(code, change, expected):
    datasets = _datasets()
    datasets[code].update(change)
    assert evaluate_family_readiness(datasets, now=NOW).state == expected


def test_all_members_current_and_compatible():
    readiness = evaluate_family_readiness(_datasets(), now=NOW)
    assert readiness.state == DataState.FOUND
    assert readiness.source_data_date == SOURCE_DATE
    assert readiness.release_identity == "fns_tax_regime-release"


@pytest.mark.parametrize("change", [
    {"last_data_date": date(2026, 8, 1)},
    {"coverage": {"release_identity": "old-member"}},
])
def test_mixed_member_generation_is_not_current(change):
    datasets = _datasets()
    datasets["fns_snrip"].update(change)
    assert evaluate_family_readiness(datasets, now=NOW).state == DataState.NOT_CHECKED


def test_negative_coverage_requires_current_frozen_generation():
    pointer = SimpleNamespace(
        generation=7,
        active_pointer="file:///accepted/bundle.json",
        validation_metadata={
            "checksum": "a" * 64,
            "validation": {"release_identity": "fns_tax_regime-release"},
        },
    )
    row = {
        "id": "coverage-1",
        "company_id": 42,
        "source_id": "fns_tax_regime",
        "worker_source_id": "fns_tax_regime",
        "mode": "local_bulk_replay",
        "status": "NOT_FOUND",
        "execution_status": "succeeded",
        "fact_count": 0,
        "checked_at": NOW,
        "updated_at": NOW,
        "publication_generation": 7,
        "source_data_date": SOURCE_DATE,
        "replay_pointer": pointer.active_pointer,
        "replay_checksum": "a" * 64,
        "source_snapshot": {"release_identity": "fns_tax_regime-release"},
    }
    kwargs = {
        "company_id": 42,
        "source_data_date": SOURCE_DATE,
        "release_identity": "fns_tax_regime-release",
    }
    assert resolve_current_company_coverage([row], pointer, **kwargs).state == DataState.NOT_FOUND
    for field, stale_value in (
        ("publication_generation", 6),
        ("replay_pointer", "file:///old"),
        ("replay_checksum", "b" * 64),
        ("source_data_date", date(2026, 8, 1)),
        ("source_snapshot", {"release_identity": "old-release"}),
    ):
        stale = {**row, field: stale_value}
        assert resolve_current_company_coverage([stale], pointer, **kwargs).state == DataState.NOT_CHECKED
    for status, execution in (("PENDING", "pending"), ("RUNNING", "running")):
        pending = {**row, "status": status, "execution_status": execution}
        assert resolve_current_company_coverage([pending], pointer, **kwargs).state == DataState.NOT_CHECKED
