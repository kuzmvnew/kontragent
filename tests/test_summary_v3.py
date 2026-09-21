from app.contracts.risk_v3 import (
    Applicability,
    Execution,
    Freshness,
    NormalizedEvidenceCandidate,
    Observation,
    ScopeCompleteness,
    SourceClass,
    SubjectIdentity,
    SubjectScope,
)
from app.services.risk_engine_v3_service import calculate_risk_v3, load_risk_v3_policy
from app.services.summary_engine_v3_service import build_summary_v3
from tests.test_risk_v3 import COMPANY_ID, NOW, complete_legal_baseline, replace


def test_summary_is_deterministic_pure_projection_with_canonical_reason_order():
    values = replace(
        complete_legal_baseline(),
        "registration",
        next(item for item in complete_legal_baseline() if item.capability_code == "registration").model_copy(
            update={"fact_payload": {"adverse": True}}
        ),
    )
    values = replace(
        values,
        "tax_debt",
        next(item for item in values if item.capability_code == "tax_debt").model_copy(
            update={"fact_payload": {"adverse": True, "total_debt": "100"}}
        ),
    )
    risk = calculate_risk_v3(
        values,
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    first = build_summary_v3(risk, generated_at=NOW)
    second = build_summary_v3(risk, generated_at=NOW)
    assert first == second
    assert first.key_reason_refs[0].endswith("REGISTRATION_ADVERSE_STATUS")
    assert first.overall_conclusion.result == risk.overall_result
    assert first.overall_conclusion.positive_conclusion_allowed is False


def test_not_found_is_completed_check_not_confirmed_positive_fact():
    policy = load_risk_v3_policy().by_code["tax_debt"]
    negative = NormalizedEvidenceCandidate(
        candidate_ref="tax-negative",
        capability_code="tax_debt",
        fact_identity=policy.fact_identity,
        company_id=COMPANY_ID,
        subject_identity=SubjectIdentity(company_id=COMPANY_ID, inn="7700000000"),
        source_code="fns_tax_debt",
        source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
        evidence_refs=("tax-negative-evidence",),
        exact_identity_match=True,
        applicability=Applicability.APPLICABLE,
        observation=Observation.NOT_FOUND,
        execution=Execution.CHECKED,
        freshness=Freshness.CURRENT,
        scope=ScopeCompleteness.COMPLETE,
        temporal_kind=policy.temporal_kind,
        source_as_of=NOW,
        checked_at=NOW,
        negative_closure_capable=True,
    )
    risk = calculate_risk_v3(
        replace(complete_legal_baseline(), "tax_debt", negative),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    summary = build_summary_v3(risk, generated_at=NOW)
    assert any(
        item.capability_code == "tax_debt"
        for item in summary.completed_checks_without_adverse_finding
    )
    assert all(
        item.fact_code != "tax_debt" for item in summary.confirmed_positive_facts
    )


def test_summary_recommendations_all_have_structured_origins():
    value = next(
        item for item in complete_legal_baseline() if item.capability_code == "tax_debt"
    ).model_copy(update={"fact_payload": {"adverse": True, "total_debt": "100"}})
    risk = calculate_risk_v3(
        replace(complete_legal_baseline(), "tax_debt", value),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    summary = build_summary_v3(risk, generated_at=NOW)
    assert summary.recommendations
    assert all(item.origin_ref for item in summary.recommendations)
    assert all(item.origin_type.value in {"FACTOR", "LIMITATION"} for item in summary.recommendations)


def test_summary_service_has_no_domain_or_provider_dependency(monkeypatch):
    risk = calculate_risk_v3(
        complete_legal_baseline(),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    # No session/source argument exists; generation remains possible even when
    # any hypothetical direct data access is represented by a failing callable.
    monkeypatch.setattr(
        "sqlalchemy.orm.Session.execute",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("DB reread")),
    )
    summary = build_summary_v3(risk, generated_at=NOW)
    assert summary.risk_assessment_id == risk.assessment_id
