from datetime import date, datetime, timezone

from app.contracts.orchestrator import CompanyCheckMode, CompanyCheckStatus
from app.services import company_check_orchestrator as orchestrator


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def check(result, dataset, **extra):
    value = {
        "checked": result in {"found", "not_found"},
        "result": result,
        "reason": "not_checked" if result == "unavailable" else None,
        "dataset_code": dataset,
        "source": "official",
        "data_date": date(2026, 9, 15) if result != "unavailable" else None,
    }
    value.update(extra)
    return value


def company():
    return {
        "id": 1, "inn": "7700000000", "entity_type": "legal", "name": "ООО ТЕСТ",
        "status": "ACTIVE", "registration_date": date(2020, 1, 1),
        "master_data_date": date(2026, 9, 15), "activity": "торговля", "okved": "47.11",
        "revenue_expense_check": check("found", "fns_revenue_expenses", revenue="10000000", expenses="9000000", calculated_difference="1000000", data_year=2025),
        "tax_debt_check": check("not_found", "fns_tax_debt"),
        "tax_offence_check": check("not_found", "fns_tax_offence"),
        "disqualified_check": check("not_found", "fns_disqualified"),
        "arbitration_court_check": check("not_found", "checko_arbitration_cases"),
        "general_court_check": check("unavailable", "moscow_general_court_cases"),
        "cbr_warning_list_check": check("not_found", "cbr_warning_list"),
        "cbr_zsk_check": {"checked": False, "status": "not_checked", "result": None},
        "fns_bankinform_check": {"checked": False, "status": "not_checked", "result": None},
        "fssp_check": check("unavailable", "fssp_enforcement"),
        "bankruptcy_check": {"checked": False, "result": "unavailable", "reason": "access_pending"},
        "legal_events": [], "company_public_facts": [],
    }


def datasets():
    codes = {
        "fns_revenue_expenses", "fns_tax_debt", "fns_tax_offence", "fns_disqualified",
        "checko_arbitration_cases", "moscow_general_court_cases", "cbr_warning_list",
        "cbr_zsk", "fns_account_suspension", "fssp_enforcement",
    }
    return {code: {"operational_status": "current", "source_as_of": NOW, "checked_at": NOW} for code in codes}


def setup(monkeypatch):
    payload = company()
    monkeypatch.setattr(orchestrator, "get_company_for_web", lambda inn: payload)
    monkeypatch.setattr(orchestrator, "get_dataset_states", datasets)
    monkeypatch.setattr(
        orchestrator, "_ensure_human_action",
        lambda inn, source: orchestrator.CompanyCheckOutcome(
            check_code=source, status=CompanyCheckStatus.HUMAN_ACTION_REQUIRED,
            source_code=source, source_url=f"https://example.test/{source}",
            message="Требуется ручное подтверждение.",
        ),
    )
    return payload


def test_quick_is_cache_only_and_preserves_access_states(monkeypatch):
    setup(monkeypatch)
    result = orchestrator.run_company_check(
        "7700000000", mode=CompanyCheckMode.QUICK, persist=False, now=NOW,
    )
    statuses = {item.check_code: item.status for item in result.outcomes}
    assert statuses["tax_debt"] == CompanyCheckStatus.SUCCESS_NOT_FOUND
    assert statuses["bankruptcy"] == CompanyCheckStatus.ACCESS_PENDING
    assert statuses["fssp"] == CompanyCheckStatus.UNAVAILABLE
    assert result.human_action_queue == ()


def test_full_runs_registered_checks_and_returns_human_action_queue(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(
        orchestrator, "_ensure_human_action",
        lambda inn, source: orchestrator.CompanyCheckOutcome(
            check_code=source, status=CompanyCheckStatus.HUMAN_ACTION_REQUIRED,
            source_code=source, source_url=f"https://example.test/{source}",
            message="Требуется ручное подтверждение.",
        ),
    )
    result = orchestrator.run_company_check(
        "7700000000", mode=CompanyCheckMode.FULL,
        runners={"general_courts": lambda inn: check("not_found", "official_courts")},
        persist=False, now=NOW,
    )
    statuses = {item.check_code: item.status for item in result.outcomes}
    assert statuses["general_courts"] == CompanyCheckStatus.SUCCESS_NOT_FOUND
    assert {item.check_code for item in result.human_action_queue} == {
        "fssp", "fns_bankinform", "cbr_zsk",
    }


def test_refresh_due_skips_current_runner(monkeypatch):
    setup(monkeypatch)
    calls = []
    orchestrator.run_company_check(
        "7700000000", mode=CompanyCheckMode.REFRESH_DUE,
        runners={"tax_debt": lambda inn: calls.append(inn) or check("not_found", "fns")},
        persist=False, now=NOW,
    )
    assert calls == []


def test_runner_error_never_becomes_not_found(monkeypatch):
    setup(monkeypatch)

    def fail(_inn):
        raise RuntimeError("provider failed")

    result = orchestrator.run_company_check(
        "7700000000", mode=CompanyCheckMode.FULL,
        runners={"general_courts": fail}, persist=False, now=NOW,
    )
    status = next(item.status for item in result.outcomes if item.check_code == "general_courts")
    assert status == CompanyCheckStatus.ERROR
