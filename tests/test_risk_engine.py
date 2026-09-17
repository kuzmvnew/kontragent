from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.contracts.report import DealContext
from app.contracts.risk import RiskSignal, RiskSignalStatus
from app.contracts.risk import ChangeOrigin
from app.services import risk_engine_service
from app.services.risk_engine_service import build_risk_assessment, can_reuse_assessment, load_ruleset


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def check(result, dataset, *, data_date=date(2026, 9, 15), **extra):
    values = {
        "checked": result in {"found", "not_found", "not_applicable"},
        "applicable": result != "not_applicable",
        "result": result,
        "data_date": None if result in {"unavailable", "not_applicable"} else data_date,
        "dataset_code": dataset,
        "source": "official_source",
        "reason": "not_checked" if result == "unavailable" else ("profile" if result == "not_applicable" else None),
    }
    values.update(extra)
    return values


def company(**overrides):
    base = {
        "id": 1,
        "inn": "7700000000",
        "entity_type": "legal",
        "name": "ООО ТЕСТ",
        "full_name": "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ ТЕСТ",
        "status": "ACTIVE",
        "registration_date": date(2020, 1, 1),
        "master_data_date": date(2026, 9, 15),
        "activity": "розничная торговля",
        "okved": "47.11",
        "revenue_expense_check": check("found", "fns_revenue_expenses", revenue="10000000", expenses="9000000", calculated_difference="1000000", data_year=2025),
        "tax_debt_check": check("not_found", "fns_tax_debt"),
        "tax_offence_check": check("not_found", "fns_tax_offence"),
        "disqualified_check": check("not_found", "fns_disqualified"),
        "arbitration_court_check": check("not_found", "checko_arbitration_cases"),
        "general_court_check": check("unavailable", "moscow_general_court_cases"),
        "cbr_warning_list_check": check("not_found", "cbr_warning_list"),
        "cbr_zsk_check": {"checked": False, "status": "not_checked", "result": None},
        "fns_bankinform_check": {"checked": False, "status": "not_checked", "result": None},
        "legal_events": [],
        "company_public_facts": [],
    }
    base.update(overrides)
    return base


def datasets(**statuses):
    codes = {
        "fns_revenue_expenses", "fns_tax_debt", "fns_tax_offence", "fns_disqualified",
        "checko_arbitration_cases", "moscow_general_court_cases", "cbr_warning_list",
        "cbr_zsk", "fns_account_suspension",
    }
    result = {
        code: {"operational_status": "current", "source_as_of": NOW, "checked_at": NOW}
        for code in codes
    }
    for code, status in statuses.items():
        result[code] = {"operational_status": status, "source_as_of": NOW, "checked_at": NOW}
    return result


def signal(result, code):
    return next(item for item in result.signals if item.signal_code == code)


def test_ruleset_is_versioned_and_hashed():
    version, digest, rules = load_ruleset()
    assert version == "risk-rules-2.0.0"
    assert len(digest) == 64
    assert rules["TAX_DEBT_TIERED"].thresholds["high_ratio"] == "0.25"


def test_tax_debt_found_uses_amount_ratio_and_threshold():
    payload = company(tax_debt_check=check("found", "fns_tax_debt", has_debt=True, total_debt="2000000", snapshot_id=9))
    result = build_risk_assessment(payload, datasets=datasets(), now=NOW)
    item = signal(result, "tax.debt")
    assert item.status == RiskSignalStatus.WARNING
    assert item.calculation == "2000000 / 10000000 = 0.2"
    assert "20.00%" in item.explanation


def test_multiple_risks_do_not_get_reduced_by_unavailable_coverage():
    payload = company(
        tax_debt_check=check("found", "fns_tax_debt", has_debt=True, total_debt="2000000"),
        cbr_warning_list_check=check("found", "cbr_warning_list"),
    )
    result = build_risk_assessment(payload, datasets=datasets(), now=NOW)
    assert sum(item.status == RiskSignalStatus.CONFIRMED_RISK for item in result.signals) >= 1
    assert result.completeness.unavailable >= 1
    assert result.overall_status == "HIGH"


@pytest.mark.parametrize(
    ("source_result", "operational_status", "expected"),
    [
        ("not_found", "current", "NO_RISK_FOUND"),
        ("unavailable", "current", "NOT_CHECKED"),
        ("unavailable", "source_blocked", "UNAVAILABLE"),
        ("not_found", "stale", "STALE"),
        ("not_applicable", "current", "NOT_APPLICABLE"),
    ],
)
def test_all_non_risk_check_states_are_preserved(source_result, operational_status, expected):
    payload = company(tax_offence_check=check(source_result, "fns_tax_offence"))
    result = build_risk_assessment(payload, datasets=datasets(fns_tax_offence=operational_status), now=NOW)
    assert signal(result, "tax.offence").status == expected


def test_partial_court_sample_is_not_full_period_and_claim_is_not_debt():
    arbitration = check(
        "found", "checko_arbitration_cases", loaded_case_count=7,
        reported_total_cases=19, coverage_complete=False, cases=[{"claim_amount": "5000000"}],
        signals={"loaded_case_count": {"period": {"from": "2025-09-17", "to": "2026-09-17"}}},
    )
    result = build_risk_assessment(company(arbitration_court_check=arbitration), datasets=datasets(), now=NOW)
    item = signal(result, "litigation.arbitration")
    assert item.status == "PARTIAL_COVERAGE"
    assert item.observed_value["reported_total_cases"] == 19
    assert "не является подтверждённым долгом" in item.explanation


def test_general_courts_always_preserve_targeted_partial_coverage():
    general = check("not_found", "moscow_general_court_cases", coverage={"regions_checked": ["Moscow"], "portals_checked": 1})
    result = build_risk_assessment(company(general_court_check=general), datasets=datasets(), now=NOW)
    assert signal(result, "litigation.general_courts").status == "PARTIAL_COVERAGE"


def test_liquidation_is_not_bankruptcy():
    event = {
        "event_type": "liquidation_in_process", "event_date": date(2026, 8, 1),
        "status": "active", "source": "fns", "source_identifier": "L1",
        "liquidation_event": True, "bankruptcy_procedure_confirmed": False,
    }
    result = build_risk_assessment(company(legal_events=[event]), datasets=datasets(), now=NOW)
    item = signal(result, "bankruptcy.liquidation_separate")
    assert item.status == "WARNING"
    assert "не равна банкротству" in item.explanation


def test_confirmed_bankruptcy_is_critical_hard_signal():
    event = {
        "event_type": "bankruptcy_observation", "event_date": date(2026, 8, 1),
        "status": "active", "source": "court", "source_identifier": "B1",
        "liquidation_event": False, "bankruptcy_procedure_confirmed": True,
    }
    result = build_risk_assessment(company(legal_events=[event]), datasets=datasets(), now=NOW)
    assert result.overall_status == "CRITICAL"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"checked": True, "status": "completed", "result": "high_risk_information_found", "checked_at": NOW}, "CONFIRMED_RISK"),
        ({"checked": True, "status": "completed", "result": "high_risk_information_not_found", "checked_at": NOW}, "NO_RISK_FOUND"),
        ({"checked": False, "status": "challenge_required", "result": "challenge_required"}, "UNAVAILABLE"),
    ],
)
def test_zsk_found_negative_and_unavailable(raw, expected):
    result = build_risk_assessment(company(cbr_zsk_check=raw), datasets=datasets(), now=NOW)
    assert signal(result, "compliance.cbr_zsk").status == expected


def test_stale_protected_account_check_is_not_clean():
    raw = {"checked": True, "status": "completed", "result": "active_suspensions_not_found", "checked_at": NOW}
    result = build_risk_assessment(company(fns_bankinform_check=raw), datasets=datasets(fns_account_suspension="stale"), now=NOW)
    assert signal(result, "compliance.account_suspension").status == "STALE"


def test_account_suspension_found_is_strong_signal():
    raw = {"checked": True, "status": "completed", "result": "active_suspensions_found", "checked_at": NOW, "evidence": {"decision_count": 2}}
    result = build_risk_assessment(company(fns_bankinform_check=raw), datasets=datasets(), now=NOW)
    assert signal(result, "compliance.account_suspension").status == "CONFIRMED_RISK"


def test_mass_address_is_context_only_with_value_and_rule_version():
    fact = {
        "id": 5, "fact_type": "mass_address", "source_code": "master_registry",
        "currentness": "derived_current_snapshot", "observed_at": NOW,
        "value": {"exact_full_address_active_count": 28, "coverage": "exact_full_address_active_count_only"},
    }
    result = build_risk_assessment(company(company_public_facts=[fact]), datasets=datasets(), now=NOW)
    item = signal(result, "management.address_context")
    assert item.status == "INFO"
    assert item.threshold["classification"] == "context_only"


def test_disqualified_record_preserves_identity_uncertainty():
    result = build_risk_assessment(company(disqualified_check=check("found", "fns_disqualified")), datasets=datasets(), now=NOW)
    assert "не доказывает" in signal(result, "management.disqualified_record").explanation


def test_profiles_and_applicability_are_resolved_before_context_checks():
    ip = company(inn="123456789012", entity_type="individual_entrepreneur")
    result = build_risk_assessment(ip, datasets=datasets(), now=NOW)
    assert result.profile == "IP"
    assert signal(result, "licence.context_not_applicable").status == "NOT_APPLICABLE"
    procurement = build_risk_assessment(ip, datasets=datasets(), deal_context=DealContext(subject="участие в закупке"), now=NOW)
    assert signal(procurement, "procurement.rnp_access").status == "UNAVAILABLE"


def test_registration_year_integer_is_supported():
    result = build_risk_assessment(company(registration_date=2026), datasets=datasets(), now=NOW)
    assert result.profile == "NEW_COMPANY"


def test_missing_registration_status_is_not_a_completed_check():
    result = build_risk_assessment(
        company(status=None, master_data_date=None), datasets=datasets(), now=NOW,
    )
    item = signal(result, "registration.status")
    assert item.status == "NOT_CHECKED"
    assert item.severity == "NONE"
    assert result.overall_status != "NO_MATERIAL_RISKS"


def test_finance_uses_financial_rule_not_registration_rule():
    result = build_risk_assessment(company(), datasets=datasets(), now=NOW)
    assert signal(result, "finance.revenue_expense").rule_code == "FINANCIAL_RESULT"


def test_partial_general_court_coverage_does_not_raise_business_risk():
    general = check(
        "not_found", "moscow_general_court_cases",
        coverage={"regions_checked": ["Moscow"], "portals_checked": 1},
    )
    result = build_risk_assessment(
        company(general_court_check=general), datasets=datasets(), now=NOW,
    )
    item = signal(result, "litigation.general_courts")
    assert item.status == "PARTIAL_COVERAGE"
    assert item.severity == "NONE"


def test_avtovaz_like_partial_found_courts_do_not_create_attention_alone():
    general = check(
        "found", "moscow_general_court_cases",
        cases=[{"role": "defendant"}] * 3,
        coverage={"regions_checked": ["Moscow"], "portals_checked": 1},
    )
    result = build_risk_assessment(
        company(
            general_court_check=general,
            arbitration_court_check=check("not_found", "checko_arbitration_cases"),
        ),
        datasets=datasets(), now=NOW,
    )
    item = signal(result, "litigation.general_courts")
    assert item.status == "PARTIAL_COVERAGE"
    assert item.severity == "NONE"
    assert result.overall_status == "INSUFFICIENT_DATA"


def test_extreme_tax_ratio_requires_data_quality_review_and_is_not_high_by_ratio_alone():
    payload = company(
        revenue_expense_check=check(
            "found", "fns_revenue_expenses", revenue="1000", expenses="900",
            calculated_difference="100", data_year=2025,
        ),
        tax_debt_check=check(
            "found", "fns_tax_debt", has_debt=True, total_debt="6000000", snapshot_id=7,
        ),
    )
    result = build_risk_assessment(payload, datasets=datasets(), now=NOW)
    debt = signal(result, "tax.debt")
    quality = signal(result, "data_quality.tax_debt_ratio")
    assert debt.severity == "MEDIUM"
    assert quality.status == "DATA_QUALITY_REVIEW_REQUIRED"
    assert quality.observed_value["numerator"] == "6000000"
    assert quality.observed_value["denominator"] == "1000"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"checked": True, "result": "found", "data_date": date(2026, 9, 15)}, "WARNING"),
        ({"checked": True, "result": "not_found", "data_date": date(2026, 9, 15)}, "NO_RISK_FOUND"),
        ({"checked": False, "result": "unavailable", "reason": "challenge_required"}, "UNAVAILABLE"),
    ],
)
def test_fssp_found_not_found_and_unavailable_are_distinct(raw, expected):
    result = build_risk_assessment(company(fssp_check=raw), datasets=datasets(), now=NOW)
    assert signal(result, "enforcement.fssp").status == expected


def test_no_risk_found_contract_rejects_unknown_coverage():
    source = signal(build_risk_assessment(company(), datasets=datasets(), now=NOW), "tax.debt")
    with pytest.raises(ValidationError):
        RiskSignal.model_validate({**source.model_dump(), "coverage": "unknown"})


def test_ruleset_and_engine_versions_are_saved_in_output():
    result = build_risk_assessment(company(), datasets=datasets(), now=NOW)
    assert result.risk_engine_version == "risk-engine-2.0.1"
    assert result.ruleset_version == "risk-rules-2.0.0"
    assert len(result.ruleset_hash) == 64


def test_cache_reuse_requires_all_meaningful_hashes_and_engine_version():
    previous = SimpleNamespace(input_hash="i", deal_context_hash="c", ruleset_hash="r", risk_engine_version="risk-engine-2.0.1")
    assert can_reuse_assessment(previous, input_hash="i", context_hash="c", ruleset_hash="r")
    assert not can_reuse_assessment(previous, input_hash="changed", context_hash="c", ruleset_hash="r")
    assert not can_reuse_assessment(previous, input_hash="i", context_hash="changed", ruleset_hash="r")
    assert not can_reuse_assessment(previous, input_hash="i", context_hash="c", ruleset_hash="changed")


@pytest.mark.parametrize(
    ("prior", "input_hash", "context_hash", "ruleset_hash", "payload", "expected"),
    [
        (None, "i", "c", "r", {"company": {}}, ChangeOrigin.SOURCE_CHANGE),
        (SimpleNamespace(ruleset_hash="old", deal_context_hash="c", input_hash="i", input_snapshot={}), "i", "c", "new", {}, ChangeOrigin.RULESET_CHANGE),
        (SimpleNamespace(ruleset_hash="r", deal_context_hash="old", input_hash="i", input_snapshot={}), "i", "new", "r", {}, ChangeOrigin.DEAL_CONTEXT_CHANGE),
        (SimpleNamespace(ruleset_hash="r", deal_context_hash="c", input_hash="old", input_snapshot={"company": {"id": 1}, "dataset_states": {"a": 1}}), "new", "c", "r", {"company": {"id": 1}, "dataset_states": {"a": 2}}, ChangeOrigin.COVERAGE_CHANGE),
        (SimpleNamespace(ruleset_hash="r", deal_context_hash="c", input_hash="old", input_snapshot={"company": {"id": 1}}), "new", "c", "r", {"company": {"id": 2}}, ChangeOrigin.SOURCE_CHANGE),
    ],
)
def test_cache_invalidation_change_origin(prior, input_hash, context_hash, ruleset_hash, payload, expected):
    assert risk_engine_service._change_origin(
        prior, input_hash=input_hash, context_hash=context_hash,
        ruleset_hash=ruleset_hash, payload=payload,
    ) == expected


def test_recalculate_forces_new_calculation(monkeypatch):
    observed = {}
    monkeypatch.setattr(
        risk_engine_service,
        "calculate_company_risk",
        lambda company, deal_context=None, force_recalculate=False: observed.update(force=force_recalculate) or "result",
    )
    assert risk_engine_service.recalculate_company_risk("7700000000") == "result"
    assert observed == {"force": True}
