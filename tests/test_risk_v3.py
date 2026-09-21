from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.contracts.risk_v3 import (
    Applicability,
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
    return candidate(
        code,
        applicability=Applicability.NOT_APPLICABLE,
        observation=Observation.UNKNOWN,
        execution=Execution.CHECKED,
        freshness=Freshness.UNKNOWN,
        scope=ScopeCompleteness.COMPLETE,
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


def check(result, code):
    return next(item for item in result.resolved_checks if item.capability_code == code)


def blocking(result, code):
    item = next(
        value for value in result.mandatory_gate.blocking_checks
        if value.capability_code == code
    )
    return item.reasons


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
        (first, second), company_id=COMPANY_ID
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
    result = resolve_evidence_candidates((older, current), company_id=COMPANY_ID)
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
    result = resolve_evidence_candidates((negative,), company_id=COMPANY_ID)
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
    result = resolve_evidence_candidates((negative,), company_id=COMPANY_ID)
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
    assert original.coverage_policy_version == "coverage-v3.0.0"


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
