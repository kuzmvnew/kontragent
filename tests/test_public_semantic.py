from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.contracts import semantic_v4
from public_app.contracts import Freshness, PublicProjection, PublicSourceBlock, PublicState
from public_app.main import create_app
from public_app.semantic import (
    MeaningInput,
    PUBLIC_NEXT_INDEX_ENABLED,
    SemanticCompilerError,
    compile_limitation,
    compile_meaning,
    compile_recommendation,
    compile_source_status,
    validate_public_text,
)
from scripts.export_public_release import _risk_projection, _summary_projection
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
