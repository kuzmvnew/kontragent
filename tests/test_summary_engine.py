from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.contracts.risk import ChangeOrigin, RiskSeverity, RiskSignalStatus
from app.contracts.summary import SummaryMode
from app.services.risk_engine_service import build_risk_assessment
from app.services.summary_engine_service import build_summary, can_reuse_summary


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def check(result, dataset, **extra):
    values = {
        "checked": result in {"found", "not_found", "not_applicable"},
        "applicable": result != "not_applicable",
        "result": result,
        "data_date": None if result in {"unavailable", "not_applicable"} else date(2026, 9, 15),
        "dataset_code": dataset,
        "source": "official_source",
        "reason": "not_checked" if result == "unavailable" else None,
    }
    values.update(extra)
    return values


def company(**overrides):
    base = {
        "id": 1, "inn": "7700000000", "entity_type": "legal", "name": "ООО ТЕСТ",
        "full_name": "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ ТЕСТ",
        "status": "ACTIVE", "registration_date": date(2020, 1, 1),
        "master_data_date": date(2026, 9, 15), "activity": "торговля", "okved": "47.11",
        "revenue_expense_check": check(
            "found", "fns_revenue_expenses", revenue="10000000", expenses="9000000",
            calculated_difference="1000000", data_year=2025,
        ),
        "tax_debt_check": check("not_found", "fns_tax_debt"),
        "tax_offence_check": check("not_found", "fns_tax_offence"),
        "disqualified_check": check("not_found", "fns_disqualified"),
        "arbitration_court_check": check("not_found", "checko_arbitration_cases"),
        "general_court_check": check("unavailable", "moscow_general_court_cases"),
        "cbr_warning_list_check": check("not_found", "cbr_warning_list"),
        "cbr_zsk_check": {"checked": False, "status": "not_checked", "result": None},
        "fns_bankinform_check": {"checked": False, "status": "not_checked", "result": None},
        "legal_events": [], "company_public_facts": [],
    }
    base.update(overrides)
    return base


def datasets():
    codes = {
        "fns_revenue_expenses", "fns_tax_debt", "fns_tax_offence", "fns_disqualified",
        "checko_arbitration_cases", "moscow_general_court_cases", "cbr_warning_list",
        "cbr_zsk", "fns_account_suspension",
    }
    return {code: {"operational_status": "current", "source_as_of": NOW, "checked_at": NOW} for code in codes}


def assessment(**company_overrides):
    return build_risk_assessment(company(**company_overrides), datasets=datasets(), now=NOW)


def texts(result, block):
    return [item.text for item in getattr(result.text_blocks, block)]


def test_short_conclusion_keeps_risk_and_incompleteness_separate():
    result = build_summary(assessment(), now=NOW)
    text = result.text_blocks.short_conclusion.text
    assert "материальные риск-сигналы не выявлены" in text
    assert "Проверка неполная" in text
    assert "надёж" not in text


def test_tax_numeric_wording_includes_value_denominator_ratio_date_and_rule_trace():
    risk = assessment(tax_debt_check=check("found", "fns_tax_debt", has_debt=True, total_debt="2000000"))
    result = build_summary(risk, now=NOW)
    statement = next(item for item in result.text_blocks.main_factors if item.signal_ids == ("tax.debt",))
    assert "2 000 000 RUB" in statement.text
    assert "10 000 000 RUB" in statement.text
    assert "20%" in statement.text
    assert "2026-09-17" in statement.text
    assert statement.rule_id == "TAX_DEBT_PRESENT"
    assert statement.evidence_refs
    assert statement.calculation == "2000000 / 10000000 = 0.2"


def test_missing_denominator_is_explicit_and_not_called_significant():
    risk = assessment(
        revenue_expense_check=check("unavailable", "fns_revenue_expenses"),
        tax_debt_check=check("found", "fns_tax_debt", has_debt=True, total_debt="2000000"),
    )
    text = " ".join(texts(build_summary(risk, now=NOW), "main_factors"))
    assert "Материальность относительно выручки не рассчитана" in text
    assert "значитель" not in text.lower()


def test_court_claim_is_never_worded_as_debt_and_partial_coverage_is_visible():
    court = check(
        "found", "checko_arbitration_cases", loaded_case_count=7, reported_total_cases=19,
        coverage_complete=False, signals={"total_claim_amount": {"value": "120000000"}},
    )
    result = build_summary(assessment(arbitration_court_check=court), now=NOW)
    combined = " ".join(texts(result, "main_factors") + texts(result, "limitations"))
    assert "Сумма заявленных требований — 120 000 000 RUB" in combined
    assert "не является подтверждённым долгом" in combined
    assert "загружены частично" in combined
    assert "компания должна" not in combined.lower()


def test_zsk_negative_has_narrow_public_check_wording():
    risk = assessment(cbr_zsk_check={
        "checked": True, "status": "completed",
        "result": "high_risk_information_not_found", "checked_at": NOW,
    })
    result = build_summary(risk, now=NOW)
    combined = " ".join(texts(result, "main_factors"))
    assert "В публичной проверке Банка России" in combined
    assert "сведения о высокой группе риска не обнаружены" in combined
    assert "надёж" not in combined


def test_finance_and_tax_offence_quantities_are_rendered_with_periods():
    risk = assessment(tax_offence_check=check(
        "found", "fns_tax_offence", fine_amount="241486695.40",
        document_date="2025-12-02", has_offence=True,
    ))
    result = build_summary(risk, now=NOW)
    combined = " ".join(texts(result, "main_factors"))
    assert "Выручка — 10 000 000 RUB" not in combined  # profitable finance is not a warning factor
    assert "241 486 695.4 RUB" in combined
    assert "2025-12-02" in combined
    assert "Вид правонарушения источник не публикует" in combined


def test_finance_warning_is_numeric_and_period_dated():
    risk = assessment(revenue_expense_check=check(
        "found", "fns_revenue_expenses", revenue="10000000", expenses="12000000",
        calculated_difference="-2000000", data_year=2025,
    ))
    combined = " ".join(texts(build_summary(risk, now=NOW), "main_factors"))
    assert "Выручка — 10 000 000 RUB" in combined
    assert "расходы — 12 000 000 RUB" in combined
    assert "за 2025" in combined


def test_cbr_warning_found_uses_regulatory_not_reliability_wording():
    risk = assessment(cbr_warning_list_check=check("found", "cbr_warning_list"))
    combined = " ".join(texts(build_summary(risk, now=NOW), "main_factors"))
    assert "предупредительном списке Банка России" in combined
    assert "точному идентификатору" in combined
    assert "надёж" not in combined


def test_unavailable_and_not_checked_never_become_nothing_found():
    result = build_summary(assessment(), now=NOW)
    combined = " ".join(texts(result, "limitations"))
    assert "ФССП не подключён" in combined
    assert "чистый результат не сформирован" in combined
    assert "ничего не найдено" not in combined.lower()


def test_not_applicable_is_not_positive_or_a_limitation():
    result = build_summary(assessment(), now=NOW)
    all_text = " ".join(item.text for item in result.explainability)
    assert "Отраслевые лицензии/SRO не активированы" not in all_text


def test_hard_blocker_precedes_other_factors():
    event = {
        "event_type": "bankruptcy_observation", "event_date": date(2026, 8, 1),
        "status": "active", "source": "court", "source_identifier": "B1",
        "liquidation_event": False, "bankruptcy_procedure_confirmed": True,
    }
    risk = assessment(
        legal_events=[event],
        tax_debt_check=check("found", "fns_tax_debt", has_debt=True, total_debt="2000000"),
    )
    result = build_summary(risk, now=NOW)
    assert result.text_blocks.main_factors[0].signal_ids == ("bankruptcy.active_procedure",)
    assert result.text_blocks.main_factors[0].hard_blocker is True


def test_all_critical_factors_survive_ui_limit():
    risk = assessment()
    template = next(item for item in risk.signals if item.signal_code == "tax.debt")
    critical = tuple(
        template.model_copy(update={
            "signal_code": f"critical.{index}", "severity": RiskSeverity.CRITICAL,
            "status": RiskSignalStatus.CONFIRMED_RISK, "rule_code": "ACTIVE_BANKRUPTCY_PROCEDURE",
        })
        for index in range(7)
    )
    risk = risk.model_copy(update={"signals": critical})
    result = build_summary(risk, now=NOW, max_factors=5)
    assert len(result.text_blocks.main_factors) == 7


def test_statement_traceability_answers_why():
    result = build_summary(assessment(), now=NOW)
    assert all(item.statement_id.startswith(result.summary_id) for item in result.explainability)
    assert all(item.summary_id == result.summary_id for item in result.explainability)
    assert all(item.risk_assessment_id == result.risk_assessment_id for item in result.explainability)
    meaningful = [item for item in result.explainability if item.kind != "CONCLUSION"]
    assert all(item.signal_ids or item.evidence_refs for item in meaningful)


def test_versioning_is_explicit():
    result = build_summary(assessment(), now=NOW)
    assert result.summary_engine_version == "summary-engine-1.0.1"
    assert result.risk_engine_version == "risk-engine-1.0.0"
    assert result.ruleset_version == "risk-rules-1.0.0"


def test_cache_reuse_and_invalidation_contract():
    previous = SimpleNamespace(
        risk_assessment_id="a", mode="USER", summary_engine_version="summary-engine-1.0.1",
        deal_context_hash="c", projection_policy_version="p",
    )
    assert can_reuse_summary(previous, risk_assessment_id="a", mode=SummaryMode.USER, deal_context_hash="c", projection_policy_version="p")
    assert not can_reuse_summary(previous, risk_assessment_id="b", mode=SummaryMode.USER, deal_context_hash="c", projection_policy_version="p")
    assert not can_reuse_summary(previous, risk_assessment_id="a", mode=SummaryMode.BULK, deal_context_hash="c", projection_policy_version="p")
    assert not can_reuse_summary(previous, risk_assessment_id="a", mode=SummaryMode.USER, deal_context_hash="changed", projection_policy_version="p")
    assert not can_reuse_summary(previous, risk_assessment_id="a", mode=SummaryMode.USER, deal_context_hash="c", projection_policy_version="changed")


def test_public_mode_is_filtered_and_explicitly_not_launch_approved():
    result = build_summary(assessment(), mode=SummaryMode.PUBLIC, now=NOW)
    assert result.public_projection_approved is False
    assert all(item.public_visible for item in result.text_blocks.main_factors)
    assert all(item.public_visible for item in result.text_blocks.limitations)


def test_due_diligence_payload_carries_sources_dates_versions_and_limitations():
    result = build_summary(assessment(), mode=SummaryMode.DUE_DILIGENCE, now=NOW)
    assert result.text_blocks.limitations
    statement = result.text_blocks.limitations[0]
    assert statement.source_refs
    assert statement.rule_version
    assert result.ruleset_version


def test_ruleset_only_monitoring_change_is_not_company_change():
    current = assessment().model_copy(update={"change_origin": ChangeOrigin.RULESET_CHANGE})
    previous = assessment().model_copy(update={"assessment_id": "previous"})
    result = build_summary(current, mode=SummaryMode.MONITORING, previous_assessment=previous, now=NOW)
    assert result.comparison.methodology_only is True
    assert result.comparison.company_change is False
    assert "не событие компании" in result.text_blocks.changes[0].text


def test_coverage_and_deal_context_changes_are_not_company_events():
    previous = assessment().model_copy(update={"assessment_id": "previous"})
    for origin in (ChangeOrigin.COVERAGE_CHANGE, ChangeOrigin.DEAL_CONTEXT_CHANGE):
        current = assessment().model_copy(update={"change_origin": origin})
        result = build_summary(current, mode=SummaryMode.MONITORING, previous_assessment=previous, now=NOW)
        assert result.comparison.company_change is False
        assert "не подтверждает изменение самой компании" in result.text_blocks.changes[0].text


def test_bulk_mode_is_compact_and_keeps_completeness():
    result = build_summary(assessment(), mode=SummaryMode.BULK, now=NOW)
    assert "warnings" in result.compact_text
    assert "critical" in result.compact_text
    assert "Core" in result.compact_text
    assert "source unavailable" in result.compact_text


def test_person_mode_filters_private_internal_evidence():
    risk = assessment(tax_debt_check=check("found", "fns_tax_debt", has_debt=True, total_debt="2000000"))
    source = next(item for item in risk.signals if item.signal_code == "tax.debt")
    private = source.model_copy(update={
        "signal_code": "person.private", "source_code": "PRIVATE_INTERNAL",
        "dataset_code": "private_person", "evidence_refs": ("private_internal:1",),
    })
    risk = risk.model_copy(update={"signals": (*risk.signals, private)})
    result = build_summary(risk, mode=SummaryMode.PERSON, now=NOW)
    assert all("person.private" not in item.signal_ids for item in result.explainability)


def test_account_suspension_stale_wording_discloses_age():
    risk = assessment().model_copy(update={
        "signals": tuple(
            item.model_copy(update={"status": RiskSignalStatus.STALE})
            if item.signal_code == "compliance.account_suspension" else item
            for item in assessment().signals
        )
    })
    result = build_summary(risk, now=NOW)
    text = " ".join(texts(result, "limitations"))
    assert "Актуальная проверка приостановлений" in text
    assert "последняя дата" in text


def test_recommendations_are_specific_not_automatic_refusal():
    risk = assessment(tax_debt_check=check("found", "fns_tax_debt", has_debt=True, total_debt="2000000"))
    result = build_summary(risk, now=NOW)
    text = " ".join(texts(result, "recommendations"))
    assert "Запросить подтверждение погашения" in text
    assert "не работать" not in text.lower()
