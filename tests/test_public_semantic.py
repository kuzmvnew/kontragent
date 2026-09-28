from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.contracts import semantic_v4
from app.contracts.company_view_v1 import (
    Audience,
    CompanyViewModelV1,
    CompanyViewSectionV1,
    DataState,
)
from app.services.company_view_service import (
    filter_company_view,
    normalize_firmoteka_projection,
    select_semantic_facts,
)
from public_app.contracts import (
    Freshness,
    PublicCompanyViewV1,
    PublicProjection,
    PublicSourceBlock,
    PublicState,
)
from public_app.main import create_app
from public_app.semantic import (
    MeaningInput,
    PUBLIC_NEXT_INDEX_ENABLED,
    SemanticCompilerError,
    aggregate_clean_conclusion_proof,
    compile_limitation,
    compile_meaning,
    compile_recommendation,
    compile_source_status,
    validate_public_text,
)
from scripts.export_public_release import (
    _index_eligible,
    _public_company_view,
    _risk_projection,
    _summary_projection,
)
from tests.public_test_support import projection


INTERNAL_MARKERS = (
    "{'",
    '"origin_ref"',
    "fact_ref",
    "origin_check_ref",
    "limitation_code",
    "recommendation_code",
    "parameters",
    "evidence_refs",
    "APPLICABILITY_UNKNOWN",
    "STALE_DATA",
    "REVEXP",
    "PAYTAX",
    "DEBTAM",
    "TAXOFFENCE",
    "Risk v3",
    "rules-v3",
    "meaning_id",
    "FIRMOTEKA_AUTHORIZED_BRIDGE",
    "TAX_OFFENCE_PRESENT",
)


class _Repository:
    def __init__(self, item: PublicProjection):
        self.item = item

    def get_company(self, inn):
        return self.item if inn == self.item.company.inn else None

    def ready(self):
        return True, self.item.publication.release_id, 1

    def search(self, _query, limit=20):
        return [self.item][:limit]

    def sitemap_rows(self):
        return []


NOW = "2026-09-25T07:00:00+00:00"


def _clean_risk_v3() -> dict:
    return {
        "assessment_id": "11111111-1111-1111-1111-111111111111",
        "company_id": 1,
        "subject_scope": "LEGAL_ENTITY",
        "status": "CALCULATED",
        "risk_model_version": "risk-v3.0.0",
        "ruleset_version": "risk-rules-v3.0.0",
        "coverage_policy_version": "coverage-v3.0.0",
        "applicability_policy_version": "applicability-v3.0.0",
        "source_resolution_policy_version": "resolution-v3.0.0",
        "freshness_policy_version": "freshness-v3.0.0",
        "input_hash": "a" * 64,
        "calculated_at": NOW,
        "evidence_snapshot": [
            {
                "candidate_ref": "candidate:tax-debt:1",
                "capability_code": "tax_debt",
                "fact_identity": "company:1:tax_debt",
                "company_id": 1,
                "subject_identity": {"company_id": 1},
                "source_code": "fns_tax_debt",
                "source_class": "OFFICIAL_DOWNLOADED_DATASET",
                "evidence_refs": ["evidence:tax-debt:1"],
                "exact_identity_match": True,
                "applicability": "APPLICABLE",
                "observation": "NOT_FOUND",
                "execution": "CHECKED",
                "freshness": "CURRENT",
                "scope": "COMPLETE",
                "scope_details": {"coverage": "official-current-dataset"},
                "temporal_kind": "CURRENT_STATE",
                "source_as_of": NOW,
                "checked_at": NOW,
                "negative_closure_capable": True,
                "limitations": [],
            }
        ],
        "resolved_checks": [
            {
                "check_ref": "risk-v3:1:tax_debt",
                "capability_code": "tax_debt",
                "fact_identity": "company:1:tax_debt",
                "company_id": 1,
                "applicability": "APPLICABLE",
                "observation": "NOT_FOUND",
                "execution": "CHECKED",
                "freshness": "CURRENT",
                "scope": "COMPLETE",
                "scope_details": {"coverage": "official-current-dataset"},
                "temporal_kind": "CURRENT_STATE",
                "resolution_state": "RESOLVED",
                "selected_evidence_refs": ["evidence:tax-debt:1"],
                "candidate_refs": ["candidate:tax-debt:1"],
                "source_refs": ["fns_tax_debt"],
                "negative_closure_proven": True,
                "source_as_of": NOW,
                "checked_at": NOW,
                "limitations": [],
                "resolution_policy_version": "resolution-v3.0.0",
            }
        ],
        "factors": [],
        "coverage_snapshot": {
            "applicable_codes": ["tax_debt"],
            "resolved_codes": ["tax_debt"],
            "unresolved_codes": [],
            "partial_codes": [],
            "applicability_unknown_codes": [],
            "numerator": 1,
            "denominator": 1,
            "percentage": "100.00",
            "mandatory_applicable_codes": ["tax_debt"],
            "mandatory_resolved_codes": ["tax_debt"],
            "mandatory_numerator": 1,
            "mandatory_denominator": 1,
            "calculated_at": NOW,
            "coverage_policy_version": "coverage-v3.0.0",
        },
        "mandatory_gate": {
            "allowed": True,
            "policy_version": "applicability-v3.0.0",
            "mandatory_applicable_codes": ["tax_debt"],
            "resolved_codes": ["tax_debt"],
            "blocking_checks": [],
            "evaluated_at": NOW,
        },
        "limitations": [],
        "overall_result": "NO_ADVERSE_FACTORS_AFTER_MANDATORY_GATE",
    }


def _incomplete_risk_v3(
    blocking_reason: str,
    *,
    applicability: str = "APPLICABLE",
    execution: str = "CHECKED",
    freshness: str = "CURRENT",
    scope: str = "COMPLETE",
    resolution_state: str = "UNRESOLVED",
) -> dict:
    risk = _clean_risk_v3()
    check = risk["resolved_checks"][0]
    check.update(
        {
            "applicability": applicability,
            "observation": "UNKNOWN",
            "execution": execution,
            "freshness": freshness,
            "scope": scope,
            "resolution_state": resolution_state,
            "selected_evidence_refs": [],
            "negative_closure_proven": False,
        }
    )
    if resolution_state == "CONFLICTING_EVIDENCE":
        check["conflict_refs"] = ["candidate:tax-debt:1", "candidate:tax-debt:2"]
    applicable_codes = ["tax_debt"] if applicability == "APPLICABLE" else []
    risk["coverage_snapshot"].update(
        {
            "applicable_codes": applicable_codes,
            "resolved_codes": [],
            "unresolved_codes": applicable_codes,
            "partial_codes": ["tax_debt"] if scope == "PARTIAL" else [],
            "applicability_unknown_codes": (
                ["tax_debt"] if applicability == "APPLICABILITY_UNKNOWN" else []
            ),
            "numerator": 0,
            "denominator": len(applicable_codes),
            "percentage": "0.00",
            "mandatory_resolved_codes": [],
            "mandatory_numerator": 0,
        }
    )
    risk["mandatory_gate"].update(
        {
            "allowed": False,
            "resolved_codes": [],
            "blocking_checks": [
                {
                    "capability_code": "tax_debt",
                    "reasons": [blocking_reason],
                    "check_ref": "risk-v3:1:tax_debt",
                }
            ],
        }
    )
    risk["overall_result"] = "INCOMPLETE_NO_POSITIVE_CONCLUSION"
    return risk


def test_meaning_compiler_is_deterministic_traceable_and_abstains_on_unknown_code():
    value = MeaningInput(
        meaning_id="meaning:tax-debt:1",
        factor_code="TAX_DEBT_PRESENT",
        factor_refs=("factor:tax-debt:1",),
        fact_refs=("fact:tax-debt:1",),
        source_refs=("source:fns-tax-debt",),
        source_dates=(date(2026, 8, 1),),
        current_state="Текущий подтверждённый факт",
        recency="Текущие сведения",
        recommendation_code="REQUEST_TAX_DEBT_CLEARANCE",
        ruleset_version="risk-rules-v3.0.0",
    )
    first = compile_meaning(value)
    second = compile_meaning(value)
    assert first == second
    assert first is not None
    assert "налоговая задолженность" in first.headline.casefold()
    assert value.fact_refs and value.factor_refs and value.source_refs
    assert "fact_refs" not in first.model_dump()
    assert compile_meaning(value.model_copy(update={"factor_code": "UNAPPROVED"})) is None


def test_abstain_guards_missing_origin_denominator_and_unsupported_claims():
    with pytest.raises(ValidationError, match="traceable origin"):
        MeaningInput(
            meaning_id="meaning:invalid",
            factor_code="TAX_DEBT_PRESENT",
            source_refs=("source:fns",),
            current_state="Текущий факт",
            ruleset_version="v3",
        )
    with pytest.raises(ValidationError, match="denominator"):
        MeaningInput(
            meaning_id="meaning:no-denominator",
            factor_code="FINANCE_ADVERSE_RESULT",
            factor_refs=("factor:finance",),
            source_refs=("source:fns",),
            current_state="Исторический факт",
            ruleset_version="v3",
            denominator_required=True,
        )
    with pytest.raises(SemanticCompilerError):
        validate_public_text({"conclusion": "Компания ненадёжна"})


def test_not_found_requires_negative_closure_and_stale_is_explicitly_historical():
    unsafe_negative = compile_source_status(
        "NOT_FOUND", source_date=date(2026, 8, 1), negative_closure_proven=False
    )
    safe_negative = compile_source_status(
        "NOT_FOUND", source_date=date(2026, 8, 1), negative_closure_proven=True
    )
    stale = compile_source_status(
        "STALE_DATA", source_date=date(2024, 8, 1), negative_closure_proven=False
    )
    assert unsafe_negative.label == "Недостаточно данных"
    assert "не выявлен" in safe_negative.explanation
    assert "01.08.2024" in stale.explanation
    assert "Текущее состояние не подтверждено" in stale.explanation
    with pytest.raises(ValidationError, match="negative closure"):
        PublicSourceBlock(
            code="DEBTAM",
            state=PublicState.NOT_FOUND,
            values={},
            source_name="ФНС",
            source_data_date=date(2026, 8, 1),
            result_date=date(2026, 9, 25),
            freshness=Freshness.CURRENT,
        )
    safe_source = PublicSourceBlock(
        code="DEBTAM",
        state=PublicState.NOT_FOUND,
        values={},
        source_name="ФНС",
        source_data_date=date(2026, 8, 1),
        result_date=date(2026, 9, 25),
        freshness=Freshness.CURRENT,
        negative_closure_proven=True,
    )
    assert safe_source.state == PublicState.NOT_FOUND


def test_qa_empty_coverage_reproduction_fails_closed_and_summary_abstains():
    risk = {
        "calculated_at": NOW,
        "factors": [],
        "limitations": [],
        "coverage_snapshot": {},
    }
    public_risk = _risk_projection(risk)
    summary = _summary_projection(
        {"generated_at": NOW, "structured_payload": {}},
        public_risk,
    )

    assert public_risk.state == PublicState.PARTIAL
    assert public_risk.limitations
    assert public_risk.limitations[0].headline == "Проверка выполнена не полностью."
    assert "Неблагоприятные факторы не выявлены" not in summary.short_conclusion
    assert summary.short_conclusion == (
        "Недостаточно данных для общего положительного вывода. "
        "Ограничения проверки указаны ниже."
    )
    serialized = json.dumps(
        {
            "risk": public_risk.model_dump(mode="json"),
            "summary": summary.model_dump(mode="json"),
        },
        ensure_ascii=False,
    )
    assert "AGGREGATE_CLEAN_PROOF_NOT_PROVEN" not in serialized
    assert "limitation_code" not in serialized

    item = projection().model_copy(update={"risk": public_risk, "summary": summary})
    web = TestClient(create_app(_Repository(item)))
    html = web.get(f"/companies/{item.company.inn}")
    api = web.get(f"/api/company/{item.company.inn}")
    assert html.status_code == api.status_code == 200
    combined = html.text + json.dumps(api.json(), ensure_ascii=False)
    assert "Недостаточно данных для общего положительного вывода" in combined
    assert "Неблагоприятные факторы не выявлены" not in combined
    assert "AGGREGATE_CLEAN_PROOF_NOT_PROVEN" not in combined


@pytest.mark.parametrize("coverage_value", [None, {}, {"denominator": 0}])
def test_missing_empty_and_zero_denominator_coverage_fail_closed(coverage_value):
    risk = _clean_risk_v3()
    if coverage_value is None:
        risk.pop("coverage_snapshot")
    else:
        risk["coverage_snapshot"] = coverage_value

    proof = aggregate_clean_conclusion_proof(risk)
    public_risk = _risk_projection(risk)

    assert proof.proven is False
    assert public_risk.state == PublicState.PARTIAL
    assert public_risk.limitations


def test_canonical_zero_denominator_and_empty_mandatory_set_fail_closed():
    risk = _clean_risk_v3()
    risk["resolved_checks"] = []
    risk["coverage_snapshot"].update(
        {
            "applicable_codes": [],
            "resolved_codes": [],
            "unresolved_codes": [],
            "numerator": 0,
            "denominator": 0,
            "percentage": "0.00",
            "mandatory_applicable_codes": [],
            "mandatory_resolved_codes": [],
            "mandatory_numerator": 0,
            "mandatory_denominator": 0,
        }
    )
    risk["mandatory_gate"].update(
        {
            "allowed": False,
            "mandatory_applicable_codes": [],
            "resolved_codes": [],
            "blocking_checks": [
                {
                    "capability_code": "__mandatory_policy__",
                    "reasons": ["EMPTY_MANDATORY_SET"],
                    "check_ref": "risk-v3:1:mandatory-policy",
                }
            ],
        }
    )
    risk["overall_result"] = "INCOMPLETE_NO_POSITIVE_CONCLUSION"

    assert aggregate_clean_conclusion_proof(risk).proven is False
    assert _risk_projection(risk).state == PublicState.PARTIAL


def test_missing_and_unresolved_mandatory_gate_fail_closed():
    missing = _clean_risk_v3()
    missing.pop("mandatory_gate")
    unresolved = _incomplete_risk_v3("NOT_CHECKED", execution="NOT_CHECKED")

    for risk in (missing, unresolved):
        assert aggregate_clean_conclusion_proof(risk).proven is False
        assert _risk_projection(risk).state == PublicState.PARTIAL


@pytest.mark.parametrize(
    ("blocking_reason", "overrides"),
    [
        ("APPLICABILITY_UNKNOWN", {"applicability": "APPLICABILITY_UNKNOWN"}),
        ("STALE_CURRENT_STATE", {"freshness": "STALE"}),
        (
            "SOURCE_UNAVAILABLE",
            {
                "execution": "SOURCE_UNAVAILABLE",
                "freshness": "UNKNOWN",
                "scope": "UNKNOWN",
            },
        ),
        (
            "TIMEOUT",
            {"execution": "TIMEOUT", "freshness": "UNKNOWN", "scope": "UNKNOWN"},
        ),
        (
            "PARSING_ERROR",
            {
                "execution": "PARSING_ERROR",
                "freshness": "UNKNOWN",
                "scope": "UNKNOWN",
            },
        ),
        ("PARTIAL_SCOPE", {"scope": "PARTIAL"}),
        ("CONFLICTING_EVIDENCE", {"resolution_state": "CONFLICTING_EVIDENCE"}),
    ],
)
def test_incomplete_canonical_risk_axes_fail_closed(blocking_reason, overrides):
    risk = _incomplete_risk_v3(blocking_reason, **overrides)

    assert aggregate_clean_conclusion_proof(risk).proven is False
    public_risk = _risk_projection(risk)
    assert public_risk.state == PublicState.PARTIAL
    assert public_risk.limitations


def test_valid_canonical_clean_negative_is_explicit_traceable_and_narrow():
    risk = _clean_risk_v3()
    proof = aggregate_clean_conclusion_proof(risk)
    public_risk = _risk_projection(risk)
    summary = _summary_projection(
        {"generated_at": NOW, "structured_payload": {}},
        public_risk,
    )

    assert proof.proven is True
    assert proof.reason_codes == ()
    assert proof.resolved_check_refs == ("risk-v3:1:tax_debt",)
    assert proof.coverage_calculated_at == NOW
    assert proof.mandatory_gate_evaluated_at == NOW
    assert public_risk.state == PublicState.NOT_FOUND
    assert public_risk.limitations == ()
    assert summary.short_conclusion == (
        "Неблагоприятные факторы не выявлены в рамках выполненных актуальных проверок."
    )
    assert "рисков нет" not in summary.short_conclusion.casefold()


def test_proven_not_applicable_check_does_not_block_clean_conclusion():
    risk = _clean_risk_v3()
    risk["resolved_checks"].append(
        {
            "check_ref": "risk-v3:1:industry_specific",
            "capability_code": "industry_specific",
            "fact_identity": "company:1:industry-specific",
            "company_id": 1,
            "applicability": "NOT_APPLICABLE",
            "applicability_decision": {
                "rule_id": "applicability.industry-specific",
                "rule_version": "1.0.0",
                "subject_scope": "LEGAL_ENTITY",
                "evidence_refs": ["evidence:registration:1"],
                "source_refs": ["source:egrul"],
                "source_classes": ["OFFICIAL_DIRECT"],
                "based_on_data_absence": False,
                "decided_at": NOW,
            },
            "observation": "UNKNOWN",
            "execution": "CHECKED",
            "freshness": "CURRENT",
            "scope": "COMPLETE",
            "temporal_kind": "CURRENT_STATE",
            "resolution_state": "RESOLVED",
            "resolution_policy_version": "resolution-v3.0.0",
        }
    )

    assert aggregate_clean_conclusion_proof(risk).proven is True
    assert _risk_projection(risk).state == PublicState.NOT_FOUND


def test_negative_closure_is_required_by_aggregate_proof():
    risk = _clean_risk_v3()
    risk["resolved_checks"][0]["negative_closure_proven"] = False

    assert aggregate_clean_conclusion_proof(risk).proven is False
    assert _risk_projection(risk).state == PublicState.PARTIAL


def test_partial_aggregate_is_not_index_eligible_but_proven_clean_is():
    item = projection()
    clean_risk = _risk_projection(_clean_risk_v3())
    partial_risk = _risk_projection(
        {
            "calculated_at": NOW,
            "factors": [],
            "limitations": [],
            "coverage_snapshot": {},
        }
    )

    assert _index_eligible(item.company, item.sources, clean_risk) is True
    assert _index_eligible(item.company, item.sources, partial_risk) is False


def test_limitations_and_recommendations_are_approved_public_objects():
    limitation = compile_limitation("APPLICABILITY_UNKNOWN")
    recommendation = compile_recommendation("REQUEST_TAX_DEBT_CLEARANCE")
    assert limitation.headline == "Применимость проверки не определена."
    assert "положительного вывода" in limitation.effect_on_conclusion
    assert recommendation is not None
    assert "Учтите действующую налоговую задолженность" in recommendation.action
    assert compile_recommendation("CHECK_SOMETHING_ALREADY_CHECKED") is None


def test_risk_v3_adapter_compiles_codes_deduplicates_factors_and_never_stringifies_objects():
    raw_factor = {
        "factor_ref": "risk:tax-debt:factor:TAX_DEBT_PRESENT",
        "factor_code": "TAX_DEBT_PRESENT",
        "fact_ref": "fact:tax-debt:1",
        "recommendation_code": "REQUEST_TAX_DEBT_CLEARANCE",
        "rule_version": "1.0.0",
        "recency": "CURRENT",
        "source_refs": ["source:fns-tax-debt"],
        "source_as_of": "2026-08-01T00:00:00+00:00",
        "parameters": {"total_debt": "125000.00"},
    }
    risk = {
        "calculated_at": "2026-09-25T07:00:00+00:00",
        "risk_model_version": "risk-v3.0.0",
        "ruleset_version": "risk-rules-v3.0.0",
        "factors": [raw_factor, {**raw_factor, "factor_ref": "duplicate-source-factor"}],
        "limitations": [
            {
                "limitation_code": "APPLICABILITY_UNKNOWN",
                "origin_check_ref": "finance",
                "parameters": {},
                "evidence_refs": [],
            }
        ],
        "coverage_snapshot": {"mandatory_hard_checks_resolved": False},
    }
    public_risk = _risk_projection(risk)
    assert len(public_risk.factors) == 1
    assert public_risk.factors[0].title == "По данным ФНС указана налоговая задолженность."
    assert public_risk.limitations[0].headline == "Применимость проверки не определена."
    summary = _summary_projection(
        {
            "generated_at": "2026-09-25T07:00:00+00:00",
            "structured_payload": {
                "limitations": risk["limitations"],
                "recommendations": [
                    {
                        "recommendation_code": "REQUEST_TAX_DEBT_CLEARANCE",
                        "origin_ref": raw_factor["factor_ref"],
                        "parameters": raw_factor["parameters"],
                    }
                ],
            },
        },
        public_risk,
    )
    assert summary.recommendations[0].action.startswith("Учтите действующую налоговую задолженность")
    serialized = json.dumps(summary.model_dump(mode="json"), ensure_ascii=False)
    assert "{'" not in serialized
    assert "recommendation_code" not in serialized
    assert "limitation_code" not in serialized


def test_public_api_sanitizes_internal_meaning_identity_before_fail_closed_validation():
    raw_factor = {
        "factor_ref": "risk-v3:factor:tax-debt:accepted",
        "factor_code": "TAX_DEBT_PRESENT",
        "fact_ref": "fact:tax-debt:accepted",
        "recommendation_code": "REQUEST_TAX_DEBT_CLEARANCE",
        "rule_version": "risk-rules-v3.9.7",
        "recency": "CURRENT",
        "source_refs": ["source:fns-tax-debt"],
        "source_as_of": "2026-09-01T00:00:00+00:00",
        "parameters": {"total_debt": "125000.00"},
    }
    persisted_risk = {
        "calculated_at": NOW,
        "risk_model_version": "risk-v3.9.7",
        "ruleset_version": "risk-rules-v3.9.7",
        "factors": [raw_factor],
        "limitations": [],
        "coverage_snapshot": {"mandatory_hard_checks_resolved": True},
    }
    public_risk = _risk_projection(persisted_risk)
    internal_meaning_id = public_risk.factors[0].meaning_id
    assert internal_meaning_id == "meaning:risk-v3:factor:tax-debt:accepted"
    public_summary = _summary_projection(
        {
            "generated_at": NOW,
            "structured_payload": {
                "recommendations": [
                    {"recommendation_code": "REQUEST_TAX_DEBT_CLEARANCE"}
                ]
            },
        },
        public_risk,
    )
    item = projection().model_copy(
        update={
            "risk": public_risk,
            "summary": public_summary,
            "company_view": PublicCompanyViewV1(
                revision="cv1:" + "c" * 64,
                generated_at=datetime.fromisoformat(NOW),
                inn=projection().company.inn,
                sections=(),
            ),
        }
    )
    web = TestClient(create_app(_Repository(item)))
    api = web.get(f"/api/company/{item.company.inn}")
    card = web.get(f"/companies/{item.company.inn}")
    assert api.status_code == card.status_code == 200
    payload = api.json()
    serialized = json.dumps(payload, ensure_ascii=False)

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert "meaning_id" not in set(keys(payload))
    assert internal_meaning_id not in serialized
    assert internal_meaning_id not in card.text
    headline = public_risk.factors[0].public_headline
    assert headline in serialized and headline in card.text
    assert item.public_conclusion in serialized and item.public_conclusion in card.text
    revision = payload["view"]["revision"]
    assert revision == item.company_view.revision
    assert f'data-view-revision="{revision}"' in card.text
    validate_public_text(payload)


def test_public_related_person_identifiers_and_public_source_contacts_reach_api_and_ssr():
    base = projection()
    observed_at = datetime.fromisoformat(NOW)
    candidates = normalize_firmoteka_projection(
        {
            "requested_inn": base.company.inn,
            "rendered_inn": base.company.inn,
            "name": base.company.name,
            "url": f"https://firmoteka.ru/{base.company.inn}",
            "fns_egrul_as_of": "2026-09-16",
            "manager": "Чундышко Гисса Арамбиевич",
            "manager_position": "Директор",
            "manager_details": {
                "name": "Чундышко Гисса Арамбиевич",
                "position": "Директор",
                "tin": "010701178084",
                "passport_number": "NEVER-PUBLIC",
                "home_address": "NEVER-PUBLIC",
            },
            "founders": [
                {
                    "type": "person",
                    "title": "Физические лица (1)",
                    "items": [
                        {
                            "name": "Чундышко Гисса Арамбиевич",
                            "tin": "010701178084",
                            "share": "100%",
                            "date": "2022-01-19",
                            "passport_series": "NEVER-PUBLIC",
                            "registration_address": "NEVER-PUBLIC",
                        }
                    ],
                }
            ],
            "contacts": {
                "phones": [
                    {
                        "value": "+7 900 100-20-30",
                        "scope": "corporate",
                        "source_as_of": "2026-09-20",
                    },
                    {
                        "value": "+7 900 100-20-31",
                        "scope": "personal",
                        "person_name": "Чундышко Гисса Арамбиевич",
                        "role": "Учредитель",
                        "source_as_of": "2026-09-20",
                        "passport": "NEVER-PUBLIC",
                    },
                ],
                "emails": [
                    {
                        "value": "office@example.test",
                        "scope": "corporate",
                        "source_as_of": "2026-09-20",
                    },
                    {
                        "value": "owner@example.test",
                        "scope": "personal",
                        "person_name": "Чундышко Гисса Арамбиевич",
                        "source_as_of": "2026-09-20",
                        "residential_address": "NEVER-PUBLIC",
                    },
                ],
            },
        },
        company_id=1,
        snapshot_identity="snapshot:public-person-contact",
        retrieved_at=observed_at,
    )
    facts = select_semantic_facts(candidates, observed_at=observed_at)
    sections = tuple(
        CompanyViewSectionV1(
            section_key=section_key,
            state=DataState.FOUND,
            facts=tuple(
                fact for fact in facts if fact.anchor.section_key == section_key
            ),
        )
        for section_key in ("management", "founders", "contacts")
    )
    semantic_view = filter_company_view(
        CompanyViewModelV1(
            revision="cv1:" + "c" * 64,
            generated_at=observed_at,
            audience=Audience.INTERNAL,
            company_id=1,
            inn=base.company.inn,
            sections=sections,
        ),
        audience=Audience.PUBLIC,
    )
    public_view = _public_company_view(
        semantic_view,
        company_id=1,
        risk=base.risk,
        summary=base.summary,
        sources=base.sources,
    )
    raw = base.model_dump(mode="json")
    raw["company_view"] = public_view.model_dump(mode="json")
    item = PublicProjection.model_validate(raw)
    web = TestClient(create_app(_Repository(item)))
    api = web.get(f"/api/company/{base.company.inn}")
    card = web.get(f"/companies/{base.company.inn}")
    assert api.status_code == card.status_code == 200
    payload = api.json()
    sections_by_key = {
        section["section_key"]: section for section in payload["view"]["sections"]
    }
    founder = sections_by_key["founders"]["items"][0]
    manager = sections_by_key["management"]["items"][0]
    contacts = sections_by_key["contacts"]["items"]
    assert founder["value"]["name"] == "Чундышко Гисса Арамбиевич"
    assert founder["value"]["identifiers"] == [
        {"identifier_type": "INN", "value": "010701178084"}
    ]
    assert founder["value"]["person_ref"] == manager["value"]["person_ref"]
    assert {item["relation_type"] for item in founder["value"]["relations"]} == {
        "FOUNDER",
        "MANAGER",
    }
    assert len(contacts) == 4
    assert {item["value"]["contact_type"] for item in contacts} == {"PHONE", "EMAIL"}
    assert {item["value"]["contact_scope"] for item in contacts} == {
        "CORPORATE",
        "PERSONAL",
    }
    personal = [
        item for item in contacts if item["value"]["contact_scope"] == "PERSONAL"
    ]
    assert all(
        item["value"]["related_person_ref"] == founder["value"]["person_ref"]
        for item in personal
    )
    assert all(item["source"]["name"] == "Firmoteka · вторичный источник" for item in contacts)
    assert all(item["source"]["source_class"] == "Публичный вторичный источник" for item in contacts)
    assert all(item["source"]["reference"] == f"https://firmoteka.ru/{base.company.inn}" for item in contacts)
    assert all(item["source"]["source_data_date"] == "2026-09-20" for item in contacts)
    assert all(
        datetime.fromisoformat(item["source"]["retrieved_at"].replace("Z", "+00:00"))
        == observed_at
        for item in contacts
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "010701178084" in serialized
    assert "010701178084" in card.text
    assert "Чундышко Гисса Арамбиевич" in card.text
    assert card.text.count('class="semantic-card contact-card"') == 4
    for value in (
        "+7 900 100-20-30",
        "+7 900 100-20-31",
        "office@example.test",
        "owner@example.test",
        "Публичный вторичный источник",
    ):
        assert value in serialized and value in card.text
    for forbidden in (
        "NEVER-PUBLIC",
        "passport_number",
        "passport_series",
        "registration_address",
        "residential_address",
        "home_address",
        "FIRMOTEKA_AUTHORIZED_BRIDGE",
        "company_id",
        "source_ref",
    ):
        assert forbidden not in serialized and forbidden not in card.text
    validate_public_text(payload)


@pytest.mark.parametrize(
    "value",
    [
        {"meaning_id": "meaning:risk-v3:factor:1"},
        {"safe": "meaning:risk-v3:factor:1"},
        {"safe": "FIRMOTEKA_AUTHORIZED_BRIDGE"},
        {"safe": "AUTHORIZED_BRIDGE"},
        {"safe": "TAX_OFFENCE_PRESENT"},
        {"nested": {"raw_sha256": "a" * 64}},
        {"nested": {"passport_number": "1234 567890"}},
        {"nested": {"registration_address": "private"}},
        {"nested": {"residential_address": "private"}},
        {"nested": {"home_address": "private"}},
        {"nested": {"source_ref": "internal://source"}},
    ],
)
def test_public_semantic_validator_rejects_internal_keys_and_values(value):
    with pytest.raises(SemanticCompilerError):
        validate_public_text(value)


def test_public_numeric_index_is_fail_closed_and_absent_from_semantic_contract():
    assert PUBLIC_NEXT_INDEX_ENABLED is False
    assert semantic_v4.PUBLIC_NEXT_INDEX_ENABLED is False
    assert "score" not in semantic_v4.SemanticEnvelope.model_fields
    assert "index" not in semantic_v4.SemanticEnvelope.model_fields


def test_observed_card_regression_has_business_russian_and_zero_raw_leaks():
    value = projection().model_dump(mode="json")
    value["risk"].update(
        {
            "title": "TAX_DEBT_PRESENT",
            "explanation": "{'fact_ref': None, 'parameters': {}}",
            "limitations": [
                {
                    "fact_ref": None,
                    "origin_check_ref": "finance",
                    "parameters": {},
                    "evidence_refs": [],
                    "limitation_code": "APPLICABILITY_UNKNOWN",
                }
            ],
        }
    )
    value["summary"].update(
        {
            "short_conclusion": "{'recommendation_code': 'REQUEST_TAX_DEBT_CLEARANCE'}",
            "main_factors": ["TAX_DEBT_PRESENT", "TAX_OFFENCE_PRESENT"],
            "limitations": [{"limitation_code": "APPLICABILITY_UNKNOWN"}],
            "recommendations": [
                {"recommendation_code": "REQUEST_TAX_DEBT_CLEARANCE"},
                {"recommendation_code": "REVIEW_TAX_OFFENCE"},
            ],
        }
    )
    value["sources"][2].update(
        {
            "state": "FOUND",
            "values": {"Общая задолженность, ₽": "125000.00"},
            "limitation": None,
        }
    )
    value["sources"][3].update(
        {
            "state": "STALE_DATA",
            "freshness": "STALE",
            "source_data_date": "2024-08-01",
            "values": {"Сумма штрафа, ₽": "1000.00"},
            "limitation": "STALE_DATA",
        }
    )
    item = PublicProjection.model_validate(value)
    web = TestClient(create_app(_Repository(item)))
    html = web.get(f"/companies/{item.company.inn}")
    api = web.get(f"/api/company/{item.company.inn}")
    assert html.status_code == api.status_code == 200
    assert "По данным ФНС указана налоговая задолженность" in html.text
    assert "Применимость проверки не определена" in html.text
    assert "Учтите действующую налоговую задолженность" in html.text
    assert "Данные устарели" in html.text
    assert "01.08.2024" in html.text
    combined = html.text + json.dumps(api.json(), ensure_ascii=False)
    for marker in INTERNAL_MARKERS:
        assert marker not in combined
    validate_public_text(api.json())
