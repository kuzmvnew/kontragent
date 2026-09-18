from datetime import datetime, timezone

import pytest

from app.contracts.risk import RiskProfile
from app.contracts.source_architecture import (
    ApplicabilityClass,
    FreshnessStatus,
    NormalizedCheckResult,
    NormalizedEvidence,
    NormalizedResultStatus,
    SourceClass,
)
from app.services.capability_applicability_service import apply_capability_applicability
from app.services.coverage_engine_service import build_coverage_v2
from app.services.risk_engine_v3_service import build_risk_v3
from app.services.source_capability_catalog import CATALOG
from app.services.source_rate_governor import (
    FIRMOTEKA_BASELINE_POLICY,
    CircuitOpenError,
    SourceBudgetExceededError,
    SourceRateGovernor,
    SourceRatePolicy,
)
from app.services.source_resolution_service import SourceResolver, SourceRunnerRegistry
from app.services.summary_engine_v3_service import build_summary_v3
from app.sources.direct_runners import CbrZskRunner, FnsBankinformRunner, FsspDirectRunner
from app.sources.firmoteka import FirmotekaSourceAdapter

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def result(code, status, source_class, source, *, coverage=1, value=None, confidence=1):
    return NormalizedCheckResult(
        check_code=code, result=status, source_class=source_class, source_code=source,
        exact_identifier_match=True if status in {NormalizedResultStatus.FOUND, NormalizedResultStatus.NOT_FOUND} else None,
        checked_at=NOW, source_as_of=NOW, freshness=FreshnessStatus.CURRENT,
        coverage=coverage, confidence=confidence,
        evidence=(NormalizedEvidence(fact=code, value=value or {}, evidence_id=f"{source}:1"),) if value is not None else (),
        limitation="partial" if coverage < 1 else None,
    )


def clean_mandatory():
    values = {}
    for capability in CATALOG.for_profile(RiskProfile.GENERAL_LE):
        if RiskProfile.GENERAL_LE not in capability.mandatory_for_profiles:
            continue
        values[capability.capability_id] = result(
            capability.capability_id, NormalizedResultStatus.NOT_FOUND,
            SourceClass.OFFICIAL_DOWNLOADED_DATASET, f"official_{capability.capability_id}",
        )
    values["registration"] = result(
        "registration", NormalizedResultStatus.FOUND,
        SourceClass.OFFICIAL_DOWNLOADED_DATASET, "fns_egrul",
        value={"status":"ACTIVE"},
    )
    values["finance"] = result(
        "finance", NormalizedResultStatus.FOUND,
        SourceClass.OFFICIAL_DOWNLOADED_DATASET, "fns_revenue_expenses",
        value={"revenue":10_000_000,"calculated_difference":1_000_000},
    )
    return values


def test_firmoteka_registration_is_first_class_normalized_bridge_result():
    payload = {"requested_inn":"7700000000","rendered_inn":"7700000000","identity_match":True,
        "status_normalized":"ACTIVE","registration_date":"2020-01-01","ogrn":"1207700000000",
        "fetched_at":NOW.isoformat(),"fns_egrul_as_of":NOW.isoformat(),"url":"https://firmoteka.ru/7700000000"}
    registration = next(x for x in FirmotekaSourceAdapter().normalize(payload) if x.check_code == "registration")
    assert registration.result == NormalizedResultStatus.FOUND
    assert registration.source_class == SourceClass.AUTHORIZED_BRIDGE
    assert registration.coverage == 1


def test_source_precedence_and_no_double_count_select_direct_fssp():
    bridge = result("fssp", NormalizedResultStatus.FOUND, SourceClass.AUTHORIZED_BRIDGE, "firmoteka_fssp", value={"count":74})
    direct = result("fssp", NormalizedResultStatus.FOUND, SourceClass.OFFICIAL_DIRECT, "fssp_direct", value={"count":96})
    resolved = SourceResolver().resolve((bridge, direct))
    assert resolved["fssp"].source_code == "fssp_direct"
    risk = build_risk_v3(resolved, profile=RiskProfile.GENERAL_LE)
    assert len([p for p in risk.points if p.capability_id == "fssp"]) == 1
    assert resolved["fssp"].resolved_by == ("firmoteka_fssp", "fssp_direct")


def test_bridge_fallback_closes_registration_but_negative_fssp_does_not_close():
    registration = result("registration", NormalizedResultStatus.FOUND, SourceClass.AUTHORIZED_BRIDGE, "firmoteka_registration", value={"status":"ACTIVE"}, confidence=.9)
    fssp = result("fssp", NormalizedResultStatus.NOT_FOUND, SourceClass.AUTHORIZED_BRIDGE, "firmoteka_fssp", coverage=.75, confidence=.9)
    coverage = build_coverage_v2(SourceResolver().resolve((registration, fssp)), profile=RiskProfile.GENERAL_LE)
    assert "registration" in coverage.resolved_capabilities
    assert "fssp" in coverage.unresolved_capabilities


def test_ggp_generic_active_text_is_not_bankruptcy_evidence():
    payload = {"requested_inn":"7730709480","rendered_inn":"7730709480","identity_match":True,
        "status_normalized":"ACTIVE","registration_date":"2020-01-01","status_detail":"Действующее юридическое лицо",
        "bankruptcy_indicator":True,"bankruptcy_excerpt":"Действующее юридическое лицо","fetched_at":NOW.isoformat()}
    bankruptcy = next(x for x in FirmotekaSourceAdapter().normalize(payload) if x.check_code == "bankruptcy")
    assert bankruptcy.result == NormalizedResultStatus.PARTIAL
    assert bankruptcy.source_class == SourceClass.DISCOVERY_ONLY
    assert not bankruptcy.evidence


def test_concrete_firmoteka_bankruptcy_event_is_bridge_evidence():
    payload = {"requested_inn":"7700000001","rendered_inn":"7700000001","identity_match":True,
        "status_normalized":"INACTIVE","registration_date":"2020-01-01",
        "status_detail":"Юридическое лицо признано несостоятельным (банкротом) и открыто конкурсное производство",
        "bankruptcy_indicator":True,"fetched_at":NOW.isoformat()}
    bankruptcy = next(x for x in FirmotekaSourceAdapter().normalize(payload) if x.check_code == "bankruptcy")
    assert bankruptcy.result == NormalizedResultStatus.FOUND
    assert bankruptcy.source_class == SourceClass.AUTHORIZED_BRIDGE


def test_fssp_runner_returns_real_fields_and_fails_closed_on_challenge():
    runner = FsspDirectRunner(lambda **_: {"exact_identifier_match":True,"count":2,"remaining_amount":1500,"records":[{"id":"1"},{"id":"2"}]})
    value = runner.run("7700000000", checked_at=NOW)
    assert value.result == NormalizedResultStatus.FOUND
    assert value.evidence[0].value["count"] == 2
    blocked = FsspDirectRunner(lambda **_: {"challenge_required":True}).run("7700000000", checked_at=NOW)
    assert blocked.result == NormalizedResultStatus.UNAVAILABLE


def test_cbr_zsk_only_exposes_high_risk_presence_or_absence():
    found = CbrZskRunner(lambda **_: {"text":"Сведения о высокой группе риска найдены"}).run("7700000000", purpose="Проверка контрагента", initiator="ООО ТЕСТ", checked_at=NOW)
    absent = CbrZskRunner(lambda **_: {"text":"Сведения о высокой группе риска отсутствуют"}).run("7700000000", purpose="Проверка контрагента", initiator="ООО ТЕСТ", checked_at=NOW)
    assert found.result == NormalizedResultStatus.FOUND
    assert absent.result == NormalizedResultStatus.NOT_FOUND
    assert "low" not in str(absent.model_dump()).lower()


def test_bankinform_requires_explicit_bik_and_normalizes_decisions():
    runner = FnsBankinformRunner(lambda **_: {"text":"Найдены решения о приостановлении операций","decisions":[{"date":"2026-09-01","authority":"7700"}]})
    missing = runner.run("7700000000", bik=None, checked_at=NOW)
    assert missing.result == NormalizedResultStatus.UNAVAILABLE
    found = runner.run("7700000000", bik="0000000000", checked_at=NOW)
    assert found.result == NormalizedResultStatus.FOUND
    assert found.evidence[0].value["decisions"]


def test_catalog_has_explicit_applicability_policy_for_every_capability():
    classes = {item.applicability_class for item in CATALOG.all()}
    assert classes == {
        ApplicabilityClass.MANDATORY_ALWAYS,
        ApplicabilityClass.MANDATORY_IF_APPLICABLE,
        ApplicabilityClass.OPTIONAL_CONTEXT,
        ApplicabilityClass.DEFERRED_EXTERNAL_ACCESS,
    }
    assert all(item.applicability_basis for item in CATALOG.all())


def test_bankinform_without_bank_context_is_not_applicable_not_unavailable():
    unavailable = result(
        "bankinform", NormalizedResultStatus.UNAVAILABLE,
        SourceClass.OFFICIAL_DIRECT, "fns_bankinform_direct", coverage=0,
    )
    resolved = apply_capability_applicability(
        {"bankinform": unavailable},
        company={"inn": "7700000000", "okved": "62.01"},
        now=NOW,
    )
    assert resolved["bankinform"].result == NormalizedResultStatus.NOT_APPLICABLE
    assert resolved["bankinform"].source_class == SourceClass.POLICY_RULE


def test_licence_sro_is_na_only_for_non_regulated_okved():
    ordinary = apply_capability_applicability(
        {}, company={"inn": "7700000000", "okved": "62.01"}, now=NOW,
    )
    regulated = apply_capability_applicability(
        {}, company={"inn": "7700000001", "okved": "43.99"}, now=NOW,
    )
    assert ordinary["licences_sro"].result == NormalizedResultStatus.NOT_APPLICABLE
    assert regulated["licences_sro"].result == NormalizedResultStatus.UNAVAILABLE
    coverage = build_coverage_v2(regulated, profile=RiskProfile.GENERAL_LE)
    assert "licences_sro" in coverage.unresolved_capabilities
    assert coverage.mandatory_hard_checks_resolved is False


def test_deferred_rnp_is_reported_but_does_not_reduce_current_coverage():
    baseline = clean_mandatory()
    before = build_coverage_v2(baseline, profile=RiskProfile.GENERAL_LE)
    with_deferred = dict(baseline)
    with_deferred["procurement_rnp"] = result(
        "procurement_rnp", NormalizedResultStatus.UNAVAILABLE,
        SourceClass.OFFICIAL_DIRECT, "eis_rnp", coverage=0,
    )
    after = build_coverage_v2(with_deferred, profile=RiskProfile.GENERAL_LE)
    assert after.coverage_score == before.coverage_score
    assert after.deferred_capabilities == ("procurement_rnp",)
    assert "procurement_rnp" not in after.unresolved_capabilities


def test_checko_40_runner_path_is_registered_and_cacheable():
    registry = SourceRunnerRegistry(); calls=[]
    registry.register("arbitration_resolver", lambda inn: calls.append(inn) or {"inn":inn})
    inns = [f"{7700000000+i:010d}" for i in range(40)]
    output = [registry.get("arbitration_resolver")(inn) for inn in inns]
    assert len(output) == 40 and len(set(calls)) == 40


def test_not_applicable_is_removed_from_coverage_denominator():
    baseline = clean_mandatory()
    without_optional = build_coverage_v2(baseline, profile=RiskProfile.GENERAL_LE)
    baseline["licences_sro"] = result("licences_sro", NormalizedResultStatus.NOT_APPLICABLE, SourceClass.OFFICIAL_DIRECT, "licence_resolver")
    with_na = build_coverage_v2(baseline, profile=RiskProfile.GENERAL_LE)
    assert with_na.coverage_score >= without_optional.coverage_score
    assert "licences_sro" in with_na.not_applicable_capabilities


def test_partial_court_coverage_adds_coverage_but_zero_risk_points():
    partial = result("general_courts", NormalizedResultStatus.PARTIAL, SourceClass.OFFICIAL_DIRECT, "moscow_courts", coverage=.25, value={"coverage_scope":"Moscow"})
    risk = build_risk_v3({"general_courts":partial}, profile=RiskProfile.GENERAL_LE)
    assert "general_courts" in risk.coverage.partial_capabilities
    assert not [p for p in risk.points if p.section == "courts"]


def test_risk_score_arithmetic_and_coverage_are_independent():
    tax = result("tax_debt", NormalizedResultStatus.FOUND, SourceClass.OFFICIAL_DOWNLOADED_DATASET, "fns_tax_debt", value={"total_debt":10_000_000,"revenue":20_000_000})
    risk = build_risk_v3({"tax_debt":tax}, profile=RiskProfile.GENERAL_LE)
    assert risk.risk_score == round(sum(p.points for p in risk.points))
    assert risk.coverage.coverage_score < 30
    assert risk.risk_score > 0


def test_official_active_bankruptcy_has_ninety_point_floor_even_at_low_coverage():
    bankruptcy = result("bankruptcy", NormalizedResultStatus.FOUND, SourceClass.OFFICIAL_DIRECT, "efrsb_direct", value={"procedure":"observation","active_procedure":True})
    risk = build_risk_v3({"bankruptcy":bankruptcy}, profile=RiskProfile.GENERAL_LE)
    assert risk.risk_score >= 90
    assert risk.coverage.coverage_score < 30
    assert risk.overall == "Критический риск"


def test_low_coverage_gate_and_positive_conclusion_gate():
    low = build_risk_v3({"registration":clean_mandatory()["registration"]}, profile=RiskProfile.GENERAL_LE)
    assert low.overall == "Недостаточно данных для полного вывода"
    assert build_summary_v3(low, {"registration":clean_mandatory()["registration"]}).positive_conclusion_allowed is False
    complete = clean_mandatory()
    accepted = build_risk_v3(complete, profile=RiskProfile.GENERAL_LE)
    summary = build_summary_v3(accepted, complete)
    assert accepted.coverage.mandatory_score == 100
    assert summary.positive_conclusion_allowed is True
    assert "Существенных рисков" in summary.conclusion


def test_positive_gate_rejects_even_low_scoring_confirmed_adverse_fact():
    complete = clean_mandatory()
    complete["cbr_warning"] = result(
        "cbr_warning", NormalizedResultStatus.FOUND,
        SourceClass.OFFICIAL_DOWNLOADED_DATASET, "cbr_warning_list",
        value={"match": True},
    )
    risk = build_risk_v3(complete, profile=RiskProfile.GENERAL_LE)
    assert 0 < risk.risk_score < 20
    assert build_summary_v3(risk, complete).positive_conclusion_allowed is False


def test_workflow_completion_is_separate_from_evidence_coverage():
    attempted = clean_mandatory()
    attempted["fssp"] = result(
        "fssp", NormalizedResultStatus.UNAVAILABLE,
        SourceClass.OFFICIAL_DIRECT, "fssp_direct", coverage=0,
    )
    coverage = build_coverage_v2(attempted, profile=RiskProfile.GENERAL_LE)
    assert coverage.workflow_completion_percent == 100
    assert coverage.coverage_score < 100
    del attempted["general_courts"]
    incomplete = build_coverage_v2(attempted, profile=RiskProfile.GENERAL_LE)
    assert incomplete.workflow_completion_percent < 100
    assert incomplete.workflow_unattempted_capabilities == ("general_courts",)


def test_positive_wording_is_source_specific_and_has_no_internal_jargon():
    complete = clean_mandatory(); risk = build_risk_v3(complete, profile=RiskProfile.GENERAL_LE); summary=build_summary_v3(risk,complete)
    combined=" ".join(summary.positive_checks + summary.limitations + (summary.conclusion,))
    assert "Предупредительный список Банка России" in combined
    for token in ("NOT_FOUND","UNAVAILABLE","PARTIAL","rule_code","dataset_code","not_checked","INACTIVE","competitive_proceedings","official flow","Публичный/машинный"):
        assert token not in combined


def test_bankruptcy_and_registration_reasons_are_russian_client_text():
    resolved = {
        "registration": result("registration", NormalizedResultStatus.FOUND, SourceClass.AUTHORIZED_BRIDGE, "firmoteka_registration", value={"status":"INACTIVE"}),
        "bankruptcy": result("bankruptcy", NormalizedResultStatus.FOUND, SourceClass.AUTHORIZED_BRIDGE, "firmoteka_bankruptcy", value={"procedure":"competitive_proceedings"}),
    }
    summary = build_summary_v3(build_risk_v3(resolved, profile=RiskProfile.GENERAL_LE), resolved)
    text = " ".join(summary.main_reasons)
    assert "Не действует" in text
    assert "конкурсное производство" in text
    assert "INACTIVE" not in text
    assert "competitive_proceedings" not in text


def test_large_money_is_never_rendered_in_scientific_notation():
    finance = result("finance", NormalizedResultStatus.FOUND, SourceClass.OFFICIAL_DOWNLOADED_DATASET, "fns_revenue_expenses", value={"revenue":1_000_000,"calculated_difference":-65_221_600_000})
    summary = build_summary_v3(build_risk_v3({"finance":finance}, profile=RiskProfile.GENERAL_LE), {"finance":finance})
    text = " ".join(summary.main_reasons)
    assert "65 221 600 000 ₽" in text
    assert "e+" not in text.lower()


def test_rate_governor_enforces_authorized_identity_cache_and_circuit():
    now=[0.0]; sleeps=[]
    governor=SourceRateGovernor({"firmoteka":FIRMOTEKA_BASELINE_POLICY},clock=lambda:now[0],sleep=lambda seconds:(sleeps.append(seconds),now.__setitem__(0,now[0]+seconds)),random_uniform=lambda a,b:a)
    calls=[]
    assert governor.run("firmoteka","770",lambda:calls.append(1) or "ok",worker_id="w1",egress_ip="1.2.3.4",shard="00") == "ok"
    assert governor.run("firmoteka","770",lambda:calls.append(2) or "changed",worker_id="w1",egress_ip="1.2.3.4",shard="00") == "ok"
    assert calls == [1]
    failing=SourceRateGovernor({"firmoteka":FIRMOTEKA_BASELINE_POLICY},clock=lambda:now[0],sleep=lambda s:now.__setitem__(0,now[0]+s),random_uniform=lambda a,b:a)
    for _ in range(3):
        with pytest.raises(RuntimeError): failing.run("firmoteka",str(_),lambda:(_ for _ in ()).throw(RuntimeError("blocked")),worker_id="w1",egress_ip="1.2.3.4",shard="00")
    with pytest.raises(CircuitOpenError): failing.acquire("firmoteka",worker_id="w1",egress_ip="1.2.3.4",shard="00")


def test_rate_governor_enforces_session_and_daily_budgets():
    policy = SourceRatePolicy(
        0, 0, max_retries=0, max_session_requests=1, max_daily_requests=1,
    )
    governor = SourceRateGovernor({"source": policy}, random_uniform=lambda _a, _b: 0)
    assert governor.run("source", "first", lambda: "ok") == "ok"
    with pytest.raises(SourceBudgetExceededError):
        governor.run("source", "second", lambda: "must-not-run")
