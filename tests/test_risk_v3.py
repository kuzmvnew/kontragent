from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.contracts.risk_v3 import (
    Applicability,
    ApplicabilityDecision,
    AssessmentStatus,
    BlockingReason,
    Execution,
    Freshness,
    NormalizedEvidenceCandidate,
    Observation,
    OverallRiskResult,
    ResolutionState,
    ScopeCompleteness,
    SourceClass,
    SubjectIdentity,
    SubjectScope,
    TemporalKind,
    UnsupportedSubjectOutcome,
)
from app.services.risk_engine_v3_service import (
    calculate_risk_v3,
    load_risk_v3_policy,
    resolve_evidence_candidates,
)


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
COMPANY_ID = 101


def candidate(
    code,
    *,
    ref=None,
    applicability=Applicability.APPLICABLE,
    observation=Observation.FOUND,
    execution=Execution.CHECKED,
    freshness=Freshness.CURRENT,
    scope=ScopeCompleteness.COMPLETE,
    source_class=None,
    negative_closure=False,
    payload=None,
    effective_at=NOW,
    applicability_decision=None,
):
    policy = load_risk_v3_policy().by_code[code]
    if source_class is None:
        source_class = sorted(policy.found_source_classes, key=lambda item: item.value)[0]
    if observation != Observation.FOUND:
        payload = {}
    return NormalizedEvidenceCandidate(
        candidate_ref=ref or f"candidate:{code}",
        capability_code=code,
        fact_identity=policy.fact_identity,
        company_id=COMPANY_ID,
        subject_identity=SubjectIdentity(company_id=COMPANY_ID, inn="7700000000"),
        source_code=f"source:{code}:{source_class.value}",
        source_class=source_class,
        evidence_refs=(f"evidence:{ref or code}",),
        exact_identity_match=True,
        applicability=applicability,
        applicability_decision=applicability_decision,
        observation=observation,
        execution=execution,
        freshness=freshness,
        scope=scope,
        temporal_kind=policy.temporal_kind,
        source_as_of=NOW,
        effective_at=effective_at,
        retrieved_at=NOW,
        checked_at=NOW,
        negative_closure_capable=negative_closure,
        fact_payload=payload if payload is not None else {"adverse": False},
    )


def not_applicable(code):
    policy = load_risk_v3_policy().by_code[code]
    source_class = sorted(
        policy.allowed_source_classes, key=lambda item: item.value
    )[0]
    evidence_ref = f"evidence:{code}"
    source_code = f"source:{code}:{source_class.value}"
    return candidate(
        code,
        applicability=Applicability.NOT_APPLICABLE,
        applicability_decision=ApplicabilityDecision(
            rule_id=policy.applicability_rule_id or "",
            rule_version=policy.applicability_rule_version or "",
            subject_scope=SubjectScope.LEGAL_ENTITY,
            evidence_refs=(evidence_ref,),
            source_refs=(source_code,),
            source_classes=(source_class,),
            based_on_data_absence=False,
            decided_at=NOW,
        ),
        observation=Observation.UNKNOWN,
        execution=Execution.CHECKED,
        freshness=Freshness.UNKNOWN,
        scope=ScopeCompleteness.COMPLETE,
        source_class=source_class,
    )


def complete_legal_baseline():
    policy = load_risk_v3_policy()
    return tuple(
        candidate(item.code)
        if item.mandatory_mode == "ALWAYS"
        else not_applicable(item.code)
        for item in policy.capabilities
    )


def replace(values, code, replacement):
    return tuple(replacement if item.capability_code == code else item for item in values)


def policy_with_capability(policy, code, **updates):
    return dataclass_replace(
        policy,
        capabilities=tuple(
            dataclass_replace(item, **updates) if item.code == code else item
            for item in policy.capabilities
        ),
    )


def check(result, code):
    return next(item for item in result.resolved_checks if item.capability_code == code)


def blocking(result, code):
    item = next(
        value for value in result.mandatory_gate.blocking_checks
        if value.capability_code == code
    )
    return item.reasons


def assert_not_applicable_proof_fails_closed(
    value, code="finance", policy=None
):
    result = calculate_risk_v3(
        replace(complete_legal_baseline(), code, value),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
        policy=policy,
    )
    resolved = check(result, code)
    assert resolved.applicability == Applicability.APPLICABILITY_UNKNOWN
    assert resolved.resolution_state == ResolutionState.UNRESOLVED
    assert code in result.coverage_snapshot.applicable_codes
    assert code in result.coverage_snapshot.unresolved_codes
    assert blocking(result, code) == (BlockingReason.APPLICABILITY_UNKNOWN,)
    assert result.mandatory_gate.allowed is False
    assert (
        result.overall_result
        == OverallRiskResult.INCOMPLETE_NO_POSITIVE_CONCLUSION
    )
    return result


def test_multi_axis_contract_allows_found_checked_stale_without_erasing_fact():
    item = candidate("registration", freshness=Freshness.STALE)
    assert item.observation == Observation.FOUND
    assert item.freshness == Freshness.STALE


@pytest.mark.parametrize(
    "execution",
    (Execution.SOURCE_UNAVAILABLE, Execution.TIMEOUT, Execution.PARSING_ERROR),
)
def test_failed_execution_cannot_be_not_found(execution):
    with pytest.raises(ValidationError):
        candidate(
            "tax_debt",
            observation=Observation.NOT_FOUND,
            execution=execution,
            negative_closure=True,
        )


def test_not_found_requires_negative_closure():
    with pytest.raises(ValidationError):
        candidate("tax_debt", observation=Observation.NOT_FOUND)


def test_not_applicable_is_checked_and_excluded_from_denominator():
    result = calculate_risk_v3(
        complete_legal_baseline(),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert result.coverage_snapshot.denominator == 9
    assert result.coverage_snapshot.numerator == 9
    assert result.coverage_snapshot.percentage == 100
    assert "finance" not in result.coverage_snapshot.applicable_codes
    assert result.mandatory_gate.allowed is True


def test_case_a_discovery_only_not_applicable_fails_closed():
    unproven = candidate(
        "finance",
        applicability=Applicability.NOT_APPLICABLE,
        observation=Observation.UNKNOWN,
        execution=Execution.CHECKED,
        freshness=Freshness.UNKNOWN,
        scope=ScopeCompleteness.COMPLETE,
        source_class=SourceClass.DISCOVERY_ONLY,
    )
    result = calculate_risk_v3(
        replace(complete_legal_baseline(), "finance", unproven),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    resolved = check(result, "finance")
    assert resolved.applicability == Applicability.APPLICABILITY_UNKNOWN
    assert resolved.resolution_state == ResolutionState.UNRESOLVED
    assert "finance" in result.coverage_snapshot.applicable_codes
    assert "finance" in result.coverage_snapshot.unresolved_codes
    assert result.coverage_snapshot.denominator == 10
    assert blocking(result, "finance") == (
        BlockingReason.APPLICABILITY_UNKNOWN,
    )
    assert result.mandatory_gate.allowed is False
    assert (
        result.overall_result
        == OverallRiskResult.INCOMPLETE_NO_POSITIVE_CONCLUSION
    )


def test_exact_identity_case_a_qa_reproduction_fails_closed():
    value = not_applicable("finance").model_copy(
        update={"exact_identity_match": False}
    )
    result = assert_not_applicable_proof_fails_closed(value)
    assert result.coverage_snapshot.numerator == 9
    assert result.coverage_snapshot.denominator == 10
    assert result.coverage_snapshot.percentage == 90


def test_exact_identity_case_b_valid_identity_remains_not_applicable():
    value = not_applicable("finance")
    assert value.exact_identity_match is True
    result = calculate_risk_v3(
        replace(complete_legal_baseline(), "finance", value),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    resolved = check(result, "finance")
    assert resolved.applicability == Applicability.NOT_APPLICABLE
    assert resolved.resolution_state == ResolutionState.RESOLVED
    assert result.mandatory_gate.allowed is True


def test_exact_identity_case_c_policy_without_requirement_does_not_invent_one():
    policy = load_risk_v3_policy()
    synthetic_policy = policy_with_capability(
        policy, "finance", exact_identity_required=False
    )
    value = not_applicable("finance").model_copy(
        update={"exact_identity_match": False}
    )
    result = calculate_risk_v3(
        replace(complete_legal_baseline(), "finance", value),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
        policy=synthetic_policy,
    )
    resolved = check(result, "finance")
    assert resolved.applicability == Applicability.NOT_APPLICABLE
    assert resolved.resolution_state == ResolutionState.RESOLVED
    assert result.mandatory_gate.allowed is True


def test_exact_identity_case_f_false_identity_with_perfect_provenance_fails_closed():
    value = not_applicable("licence_sro").model_copy(
        update={"exact_identity_match": False}
    )
    assert_not_applicable_proof_fails_closed(value, code="licence_sro")


def test_scope_case_a_partial_scope_fails_closed():
    value = not_applicable("finance").model_copy(
        update={"scope": ScopeCompleteness.PARTIAL}
    )
    assert_not_applicable_proof_fails_closed(value)


def test_scope_case_b_unknown_scope_fails_closed():
    value = not_applicable("finance").model_copy(
        update={"scope": ScopeCompleteness.UNKNOWN}
    )
    assert_not_applicable_proof_fails_closed(value)


def test_scope_fact_cases_c_e_i_valid_complete_proof_remains_resolved():
    policy = load_risk_v3_policy().by_code["finance"]
    value = not_applicable("finance")
    assert value.scope == policy.expected_scope
    assert value.fact_identity == policy.fact_identity
    result = calculate_risk_v3(
        replace(complete_legal_baseline(), "finance", value),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    resolved = check(result, "finance")
    assert resolved.applicability == Applicability.NOT_APPLICABLE
    assert resolved.resolution_state == ResolutionState.RESOLVED
    assert result.mandatory_gate.allowed is True


def test_fact_identity_case_d_qa_reproduction_fails_closed():
    value = not_applicable("finance").model_copy(
        update={"fact_identity": "unrelated.fact"}
    )
    result = assert_not_applicable_proof_fails_closed(value)
    limitation = next(
        item
        for item in check(result, "finance").limitations
        if item.limitation_code == BlockingReason.APPLICABILITY_UNKNOWN
    )
    assert limitation.parameters["candidate_fact_identity"] == "unrelated.fact"
    assert limitation.parameters["policy_fact_identity"] == "finance.period_result"


def test_fact_identity_case_f_mismatch_with_perfect_provenance_fails_closed():
    value = not_applicable("licence_sro").model_copy(
        update={"fact_identity": "unrelated.fact"}
    )
    assert_not_applicable_proof_fails_closed(value, code="licence_sro")


def test_scope_case_g_partial_scope_with_perfect_provenance_fails_closed():
    value = not_applicable("licence_sro").model_copy(
        update={"scope": ScopeCompleteness.PARTIAL}
    )
    assert_not_applicable_proof_fails_closed(value, code="licence_sro")


def test_scope_fact_case_h_double_mismatch_fails_closed():
    value = not_applicable("finance").model_copy(
        update={
            "fact_identity": "unrelated.fact",
            "scope": ScopeCompleteness.PARTIAL,
        }
    )
    assert_not_applicable_proof_fails_closed(value)


def test_case_a_unsupported_non_discovery_source_fails_closed():
    value = not_applicable("finance")
    assert value.exact_identity_match is True
    source_class = SourceClass.OFFICIAL_DIRECT
    source_code = f"source:finance:{source_class.value}"
    decision = value.applicability_decision.model_copy(
        update={
            "source_classes": (source_class,),
            "source_refs": (source_code,),
        }
    )
    value = value.model_copy(
        update={
            "source_class": source_class,
            "source_code": source_code,
            "applicability_decision": decision,
        }
    )
    result = assert_not_applicable_proof_fails_closed(value)
    assert result.coverage_snapshot.denominator == 10


def test_case_b_discovery_candidate_cannot_claim_official_provenance():
    official_decision = not_applicable("finance").applicability_decision
    disguised = candidate(
        "finance",
        applicability=Applicability.NOT_APPLICABLE,
        applicability_decision=official_decision,
        observation=Observation.UNKNOWN,
        execution=Execution.CHECKED,
        freshness=Freshness.UNKNOWN,
        scope=ScopeCompleteness.COMPLETE,
        source_class=SourceClass.DISCOVERY_ONLY,
    )
    assert_not_applicable_proof_fails_closed(disguised)


def test_case_c_candidate_and_decision_source_class_mismatch_fails_closed():
    value = not_applicable("licence_sro")
    assert value.exact_identity_match is True
    assert value.source_class == SourceClass.OFFICIAL_DIRECT
    decision = value.applicability_decision.model_copy(
        update={
            "source_classes": (SourceClass.OFFICIAL_DOWNLOADED_DATASET,),
            "source_refs": (value.source_code,),
        }
    )
    value = value.model_copy(update={"applicability_decision": decision})
    assert_not_applicable_proof_fails_closed(value, code="licence_sro")


def test_case_d_mixed_decision_classes_with_unsupported_class_fails_closed():
    value = not_applicable("finance")
    decision = value.applicability_decision.model_copy(
        update={
            "source_classes": (
                SourceClass.OFFICIAL_DOWNLOADED_DATASET,
                SourceClass.OFFICIAL_DIRECT,
            )
        }
    )
    value = value.model_copy(update={"applicability_decision": decision})
    assert_not_applicable_proof_fails_closed(value)


def test_case_e_unbound_evidence_ref_fails_closed():
    value = not_applicable("finance")
    decision = value.applicability_decision.model_copy(
        update={"evidence_refs": ("evidence:another-candidate",)}
    )
    value = value.model_copy(update={"applicability_decision": decision})
    assert_not_applicable_proof_fails_closed(value)


def test_case_f_unbound_source_ref_fails_closed():
    value = not_applicable("finance")
    decision = value.applicability_decision.model_copy(
        update={"source_refs": ("source:another-candidate",)}
    )
    value = value.model_copy(update={"applicability_decision": decision})
    assert_not_applicable_proof_fails_closed(value)


def test_case_b_empty_mandatory_set_fails_closed_without_zero_over_zero():
    full_policy = load_risk_v3_policy()
    finance_policy = full_policy.by_code["finance"]
    empty_policy = dataclass_replace(
        full_policy,
        capabilities=(finance_policy,),
    )
    result = calculate_risk_v3(
        (not_applicable("finance"),),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
        policy=empty_policy,
    )
    assert result.coverage_snapshot.denominator == 0
    assert result.coverage_snapshot.numerator == 0
    assert result.coverage_snapshot.percentage == 0
    assert result.mandatory_gate.mandatory_applicable_codes == ()
    assert result.mandatory_gate.allowed is False
    assert result.mandatory_gate.blocking_checks[0].reasons == (
        BlockingReason.EMPTY_MANDATORY_SET,
    )
    assert (
        result.overall_result
        == OverallRiskResult.INCOMPLETE_NO_POSITIVE_CONCLUSION
    )


def test_case_c_required_source_unavailable_blocks_positive_gate():
    unavailable = candidate(
        "registration",
        observation=Observation.UNKNOWN,
        execution=Execution.SOURCE_UNAVAILABLE,
        freshness=Freshness.UNKNOWN,
        scope=ScopeCompleteness.UNKNOWN,
    )
    result = calculate_risk_v3(
        replace(complete_legal_baseline(), "registration", unavailable),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert blocking(result, "registration") == (
        BlockingReason.SOURCE_UNAVAILABLE,
    )
    assert result.mandatory_gate.allowed is False
    assert (
        result.overall_result
        == OverallRiskResult.INCOMPLETE_NO_POSITIVE_CONCLUSION
    )


def test_case_g_policy_proven_not_applicable_is_excluded():
    result = calculate_risk_v3(
        complete_legal_baseline(),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    finance = check(result, "finance")
    assert finance.applicability == Applicability.NOT_APPLICABLE
    assert finance.resolution_state == ResolutionState.RESOLVED
    assert finance.applicability_decision is not None
    assert (
        finance.applicability_decision.rule_id
        == load_risk_v3_policy().by_code["finance"].applicability_rule_id
    )
    assert "finance" not in result.coverage_snapshot.applicable_codes
    assert "finance" not in result.mandatory_gate.mandatory_applicable_codes
    assert set(finance.applicability_decision.evidence_refs) <= set(
        next(
            item
            for item in result.evidence_snapshot
            if item.capability_code == "finance"
        ).evidence_refs
    )
    assert finance.applicability_decision.source_refs == finance.source_refs
    assert result.mandatory_gate.allowed is True


@pytest.mark.parametrize(
    "decision_update",
    (
        {"rule_id": "UNAPPROVED_RULE"},
        {"rule_version": "UNAPPROVED_VERSION"},
        {"subject_scope": SubjectScope.INDIVIDUAL_ENTREPRENEUR},
        {"based_on_data_absence": True},
        {"source_classes": (SourceClass.DISCOVERY_ONLY,)},
    ),
)
def test_not_applicable_requires_exact_rule_and_non_discovery_proof(
    decision_update,
):
    value = not_applicable("finance")
    invalid_decision = value.applicability_decision.model_copy(
        update=decision_update
    )
    value = value.model_copy(
        update={"applicability_decision": invalid_decision}
    )
    result = calculate_risk_v3(
        replace(complete_legal_baseline(), "finance", value),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert check(result, "finance").applicability == (
        Applicability.APPLICABILITY_UNKNOWN
    )
    assert result.mandatory_gate.allowed is False


@pytest.mark.parametrize(
    "dimension",
    (
        "permission",
        "subject",
        "rule_id",
        "rule_version",
        "data_absence",
        "fact_identity",
        "expected_scope",
        "exact_identity",
        "candidate_source_allowlist",
        "decision_source_allowlist",
        "source_class_binding",
        "evidence_binding",
        "source_ref_binding",
    ),
)
def test_not_applicable_systematic_trust_matrix_fails_closed(dimension):
    policy = load_risk_v3_policy()
    value = not_applicable("finance")
    decision = value.applicability_decision

    if dimension == "permission":
        policy = policy_with_capability(
            policy, "finance", not_applicable_allowed=False
        )
    elif dimension == "subject":
        decision = decision.model_copy(
            update={"subject_scope": SubjectScope.INDIVIDUAL_ENTREPRENEUR}
        )
    elif dimension == "rule_id":
        decision = decision.model_copy(update={"rule_id": "UNAPPROVED_RULE"})
    elif dimension == "rule_version":
        decision = decision.model_copy(
            update={"rule_version": "UNAPPROVED_VERSION"}
        )
    elif dimension == "data_absence":
        decision = decision.model_copy(update={"based_on_data_absence": True})
    elif dimension == "fact_identity":
        value = value.model_copy(update={"fact_identity": "unrelated.fact"})
    elif dimension == "expected_scope":
        value = value.model_copy(update={"scope": ScopeCompleteness.PARTIAL})
    elif dimension == "exact_identity":
        value = value.model_copy(update={"exact_identity_match": False})
    elif dimension == "candidate_source_allowlist":
        source_class = SourceClass.OFFICIAL_DIRECT
        source_code = f"source:finance:{source_class.value}"
        decision = decision.model_copy(
            update={
                "source_classes": (source_class,),
                "source_refs": (source_code,),
            }
        )
        value = value.model_copy(
            update={"source_class": source_class, "source_code": source_code}
        )
    elif dimension == "decision_source_allowlist":
        decision = decision.model_copy(
            update={
                "source_classes": (
                    SourceClass.OFFICIAL_DOWNLOADED_DATASET,
                    SourceClass.OFFICIAL_DIRECT,
                )
            }
        )
    elif dimension == "source_class_binding":
        policy = policy_with_capability(
            policy,
            "finance",
            allowed_source_classes=frozenset(
                {
                    SourceClass.OFFICIAL_DOWNLOADED_DATASET,
                    SourceClass.OFFICIAL_DIRECT,
                }
            ),
        )
        decision = decision.model_copy(
            update={"source_classes": (SourceClass.OFFICIAL_DIRECT,)}
        )
    elif dimension == "evidence_binding":
        decision = decision.model_copy(
            update={"evidence_refs": ("evidence:foreign",)}
        )
    elif dimension == "source_ref_binding":
        decision = decision.model_copy(
            update={"source_refs": ("source:foreign",)}
        )

    value = value.model_copy(update={"applicability_decision": decision})
    assert_not_applicable_proof_fails_closed(value, policy=policy)


def test_not_applicable_requires_confirmed_subject_scope_at_resolution_boundary():
    resolved = resolve_evidence_candidates(
        (not_applicable("finance"),),
        company_id=COMPANY_ID,
    )
    finance = next(
        item for item in resolved if item.capability_code == "finance"
    )
    assert finance.applicability == Applicability.APPLICABILITY_UNKNOWN
    assert finance.resolution_state == ResolutionState.UNRESOLVED


def test_always_mandatory_policy_cannot_be_removed_by_not_applicable_claim():
    finance_rule = not_applicable("finance").applicability_decision
    unproven = candidate(
        "registration",
        applicability=Applicability.NOT_APPLICABLE,
        applicability_decision=finance_rule,
        observation=Observation.UNKNOWN,
        execution=Execution.CHECKED,
        freshness=Freshness.UNKNOWN,
        scope=ScopeCompleteness.COMPLETE,
    )
    result = calculate_risk_v3(
        replace(complete_legal_baseline(), "registration", unproven),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert check(result, "registration").applicability == (
        Applicability.APPLICABILITY_UNKNOWN
    )
    assert "registration" in result.coverage_snapshot.applicable_codes
    assert "registration" in result.mandatory_gate.mandatory_applicable_codes
    assert result.mandatory_gate.allowed is False


@pytest.mark.parametrize(
    ("execution", "reason"),
    (
        (Execution.NOT_CHECKED, BlockingReason.NOT_CHECKED),
        (Execution.SOURCE_UNAVAILABLE, BlockingReason.SOURCE_UNAVAILABLE),
        (Execution.TIMEOUT, BlockingReason.TIMEOUT),
        (Execution.PARSING_ERROR, BlockingReason.PARSING_ERROR),
    ),
)
def test_execution_blocking_reasons_are_stable(execution, reason):
    values = replace(
        complete_legal_baseline(),
        "registration",
        candidate(
            "registration",
            observation=Observation.UNKNOWN,
            execution=execution,
            freshness=Freshness.UNKNOWN,
            scope=ScopeCompleteness.UNKNOWN,
        ),
    )
    result = calculate_risk_v3(
        values,
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert blocking(result, "registration") == (reason,)


def test_observation_unknown_and_applicability_unknown_block_separately():
    unknown_observation = replace(
        complete_legal_baseline(),
        "registration",
        candidate("registration", observation=Observation.UNKNOWN),
    )
    result = calculate_risk_v3(
        unknown_observation,
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert blocking(result, "registration") == (BlockingReason.OBSERVATION_UNKNOWN,)

    unknown_applicability = replace(
        complete_legal_baseline(),
        "registration",
        candidate(
            "registration",
            applicability=Applicability.APPLICABILITY_UNKNOWN,
            observation=Observation.UNKNOWN,
        ),
    )
    result = calculate_risk_v3(
        unknown_applicability,
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert blocking(result, "registration") == (
        BlockingReason.APPLICABILITY_UNKNOWN,
    )


def test_partial_and_stale_current_state_are_not_resolved():
    stale = replace(
        complete_legal_baseline(),
        "registration",
        candidate("registration", freshness=Freshness.STALE),
    )
    stale_result = calculate_risk_v3(
        stale,
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert blocking(stale_result, "registration") == (
        BlockingReason.STALE_CURRENT_STATE,
    )
    assert check(stale_result, "registration").observation == Observation.FOUND

    partial = replace(
        complete_legal_baseline(),
        "registration",
        candidate("registration", scope=ScopeCompleteness.PARTIAL),
    )
    partial_result = calculate_risk_v3(
        partial,
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert blocking(partial_result, "registration") == (
        BlockingReason.PARTIAL_SCOPE,
    )
    assert "registration" in partial_result.coverage_snapshot.partial_codes

    unknown_freshness = replace(
        complete_legal_baseline(),
        "registration",
        candidate("registration", freshness=Freshness.UNKNOWN),
    )
    unknown_freshness_result = calculate_risk_v3(
        unknown_freshness,
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert blocking(unknown_freshness_result, "registration") == (
        BlockingReason.FRESHNESS_UNKNOWN,
    )

    unknown_scope = replace(
        complete_legal_baseline(),
        "registration",
        candidate("registration", scope=ScopeCompleteness.UNKNOWN),
    )
    unknown_scope_result = calculate_risk_v3(
        unknown_scope,
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert blocking(unknown_scope_result, "registration") == (
        BlockingReason.SCOPE_UNKNOWN,
    )


def test_historical_event_is_not_discarded_only_because_it_is_stale():
    values = replace(
        complete_legal_baseline(),
        "bankruptcy",
        candidate(
            "bankruptcy",
            freshness=Freshness.STALE,
            payload={"adverse": True},
        ),
    )
    result = calculate_risk_v3(
        values,
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert check(result, "bankruptcy").resolution_state == ResolutionState.RESOLVED
    assert any(item.factor_code == "BANKRUPTCY_ADVERSE_EVENT" for item in result.factors)


def test_adverse_factor_remains_visible_when_another_check_is_incomplete():
    values = replace(
        complete_legal_baseline(),
        "tax_debt",
        candidate("tax_debt", payload={"adverse": True, "total_debt": "100"}),
    )
    values = tuple(item for item in values if item.capability_code != "cbr_zsk")
    result = calculate_risk_v3(
        values,
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert result.overall_result == OverallRiskResult.RISK_FACTORS_PRESENT
    assert any(item.factor_code == "TAX_DEBT_PRESENT" for item in result.factors)
    assert result.mandatory_gate.allowed is False


def test_conflicting_authoritative_observations_are_not_silently_ranked():
    first = candidate(
        "tax_debt", ref="tax-a", payload={"adverse": True, "total_debt": "100"}
    )
    second = candidate(
        "tax_debt", ref="tax-b", payload={"adverse": True, "total_debt": "200"}
    )
    result = resolve_evidence_candidates(
        (first, second),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
    )
    resolved = next(item for item in result if item.capability_code == "tax_debt")
    assert resolved.resolution_state == ResolutionState.CONFLICTING_EVIDENCE
    assert resolved.observation == Observation.UNKNOWN
    assert set(resolved.conflict_refs) == {"evidence:tax-a", "evidence:tax-b"}


def test_older_superseded_fact_is_not_automatically_a_conflict():
    older = candidate(
        "tax_debt",
        ref="old",
        payload={"adverse": True, "total_debt": "100"},
        effective_at=NOW - timedelta(days=30),
    )
    current = candidate(
        "tax_debt",
        ref="new",
        payload={"adverse": True, "total_debt": "200"},
        effective_at=NOW,
    )
    result = resolve_evidence_candidates(
        (older, current),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
    )
    resolved = next(item for item in result if item.capability_code == "tax_debt")
    assert resolved.resolution_state == ResolutionState.RESOLVED
    assert resolved.fact_payload["total_debt"] == "200"


@pytest.mark.parametrize(
    "source_class",
    (SourceClass.DISCOVERY_ONLY, SourceClass.AUTHORIZED_BRIDGE),
)
def test_unapproved_negative_source_cannot_close_check(source_class):
    negative = candidate(
        "tax_debt",
        observation=Observation.NOT_FOUND,
        source_class=source_class,
        negative_closure=True,
    )
    result = resolve_evidence_candidates(
        (negative,),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
    )
    resolved = next(item for item in result if item.capability_code == "tax_debt")
    assert resolved.resolution_state == ResolutionState.UNRESOLVED
    assert resolved.observation == Observation.UNKNOWN
    assert any(
        item.limitation_code == BlockingReason.NEGATIVE_CLOSURE_NOT_PROVEN
        for item in resolved.limitations
    )


def test_allowed_negative_closing_source_resolves_not_found():
    negative = candidate(
        "tax_debt",
        observation=Observation.NOT_FOUND,
        source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
        negative_closure=True,
    )
    result = resolve_evidence_candidates(
        (negative,),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
    )
    resolved = next(item for item in result if item.capability_code == "tax_debt")
    assert resolved.resolution_state == ResolutionState.RESOLVED
    assert resolved.observation == Observation.NOT_FOUND


@pytest.mark.parametrize(
    "scope", (SubjectScope.INDIVIDUAL_ENTREPRENEUR, SubjectScope.UNKNOWN)
)
def test_unsupported_subject_never_receives_legal_denominator(scope):
    result = calculate_risk_v3(
        (), company_id=COMPANY_ID, subject_scope=scope, calculated_at=NOW
    )
    assert isinstance(result, UnsupportedSubjectOutcome)
    assert result.status == AssessmentStatus.UNSUPPORTED_SUBJECT
    assert result.reason_code == "SUBJECT_SCOPE_NOT_SUPPORTED"
    assert not hasattr(result, "coverage_snapshot")


def test_same_snapshot_and_versions_are_deterministic():
    first = calculate_risk_v3(
        complete_legal_baseline(),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    second = calculate_risk_v3(
        tuple(reversed(complete_legal_baseline())),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert first == second


def test_policy_change_creates_new_identity_without_mutating_old_result():
    original_policy = load_risk_v3_policy()
    changed_policy = dataclass_replace(
        original_policy, coverage_policy_version="coverage-v3.1.0"
    )
    original = calculate_risk_v3(
        complete_legal_baseline(),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
        policy=original_policy,
    )
    changed = calculate_risk_v3(
        complete_legal_baseline(),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
        policy=changed_policy,
    )
    assert changed.input_hash != original.input_hash
    assert changed.assessment_id != original.assessment_id
    assert original.coverage_policy_version == "coverage-v3.0.1"


def test_calculation_makes_zero_provider_refresh_or_product_aggregator_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external/product aggregation call is forbidden in Risk v3")

    monkeypatch.setattr(
        "app.aggregators.company_aggregator.get_company_for_web", forbidden
    )
    monkeypatch.setattr(
        "app.aggregators.company_product_aggregator.get_company_for_web", forbidden
    )
    monkeypatch.setattr(
        "app.services.arbitration_court_service.refresh_arbitration_court_check",
        forbidden,
    )
    result = calculate_risk_v3(
        complete_legal_baseline(),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    assert result.coverage_snapshot.numerator == 9


def test_v3_contract_and_payload_contain_no_forbidden_aggregate_scoring():
    result = calculate_risk_v3(
        complete_legal_baseline(),
        company_id=COMPANY_ID,
        subject_scope=SubjectScope.LEGAL_ENTITY,
        calculated_at=NOW,
    )
    serialized = str(result.model_dump(mode="json")).lower()
    for forbidden in (
        "risk_score",
        "reliability_index",
        "weighted_coverage",
        "section_score",
        "aggregate_points",
    ):
        assert forbidden not in serialized
