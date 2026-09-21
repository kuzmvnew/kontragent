"""Deterministic Summary v3 projection from one persisted Risk v3 result."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from app.contracts.risk_v3 import (
    MaterialityState,
    Observation,
    Recency,
    ResolutionState,
    RiskAssessmentV3,
    RiskSeverity,
)
from app.contracts.summary_v3 import (
    CompletedCheckWithoutAdverseFinding,
    ConfirmedPositiveFact,
    Recommendation,
    RecommendationOriginType,
    SummaryConclusion,
    SummaryV3,
    TraceabilityEntry,
)


LOGGER = logging.getLogger(__name__)
SUMMARY_MODEL_VERSION = "summary-v3.0.0"
PROJECTION_POLICY_VERSION = "summary-projection-v3.0.0"

_SEVERITY_ORDER = {
    RiskSeverity.CRITICAL: 4,
    RiskSeverity.HIGH: 3,
    RiskSeverity.MEDIUM: 2,
    RiskSeverity.LOW: 1,
}
_RECENCY_ORDER = {
    Recency.CURRENT: 4,
    Recency.RECENT: 3,
    Recency.HISTORICAL: 2,
    Recency.UNKNOWN: 1,
}
_ACTION_ORDER = {
    "IMMEDIATE": 4,
    "REVIEW": 3,
    "MONITOR": 2,
    "NONE": 1,
}


def _reason_sort_key(factor):
    materiality = (
        factor.business_materiality.ratio
        if factor.business_materiality.state == MaterialityState.KNOWN
        and factor.business_materiality.ratio is not None
        else Decimal("-Infinity")
    )
    return (
        -int(factor.hard_blocker),
        -_SEVERITY_ORDER[factor.severity],
        -materiality,
        -_RECENCY_ORDER[factor.recency],
        -_ACTION_ORDER[factor.actionability.value],
        factor.factor_code,
    )


def build_summary_v3(
    assessment: RiskAssessmentV3,
    *,
    generated_at: datetime | None = None,
    summary_model_version: str = SUMMARY_MODEL_VERSION,
    projection_policy_version: str = PROJECTION_POLICY_VERSION,
) -> SummaryV3:
    """Project Summary from RiskAssessmentV3 without any source/domain reads."""

    generated_at = generated_at or datetime.now(timezone.utc)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    summary_id = str(
        uuid5(
            NAMESPACE_URL,
            ":".join(
                (
                    "next.company:summary-v3",
                    assessment.assessment_id,
                    summary_model_version,
                    projection_policy_version,
                )
            ),
        )
    )
    ordered_factors = tuple(sorted(assessment.factors, key=_reason_sort_key))
    key_reason_refs = tuple(item.factor_ref for item in ordered_factors)

    completed = tuple(
        CompletedCheckWithoutAdverseFinding(
            check_ref=item.check_ref,
            capability_code=item.capability_code,
            evidence_refs=item.selected_evidence_refs,
        )
        for item in assessment.resolved_checks
        if item.resolution_state == ResolutionState.RESOLVED
        and item.observation == Observation.NOT_FOUND
    )

    positive_facts: list[ConfirmedPositiveFact] = []
    for check in assessment.resolved_checks:
        raw = check.fact_payload.get("confirmed_positive_facts", ())
        if not isinstance(raw, (list, tuple)):
            continue
        for value in raw:
            if isinstance(value, str):
                code, parameters = value, {}
            elif isinstance(value, dict) and value.get("fact_code"):
                code = str(value["fact_code"])
                parameters = dict(value.get("parameters") or {})
            else:
                continue
            positive_facts.append(
                ConfirmedPositiveFact(
                    fact_code=code,
                    origin_check_ref=check.check_ref,
                    evidence_refs=check.selected_evidence_refs,
                    parameters=parameters,
                )
            )

    recommendations = [
        Recommendation(
            recommendation_code=item.recommendation_code,
            origin_type=RecommendationOriginType.FACTOR,
            origin_ref=item.factor_ref,
            action_class=item.actionability.value,
            parameters={"factor_code": item.factor_code},
        )
        for item in ordered_factors
    ]
    recommendations.extend(
        Recommendation(
            recommendation_code="RESOLVE_" + item.limitation_code,
            origin_type=RecommendationOriginType.LIMITATION,
            origin_ref=item.origin_check_ref or item.fact_ref or item.limitation_code,
            action_class="COMPLETE_CHECK",
            parameters=item.parameters,
        )
        for item in assessment.limitations
        if item.blocks_positive_conclusion
    )
    unique_recommendations = []
    seen_recommendations = set()
    for item in recommendations:
        key = (item.recommendation_code, item.origin_type, item.origin_ref)
        if key not in seen_recommendations:
            seen_recommendations.add(key)
            unique_recommendations.append(item)

    traceability = [
        TraceabilityEntry(
            output_ref=item.factor_ref,
            check_refs=(item.underlying_check_ref,)
            if item.underlying_check_ref
            else (),
            factor_refs=(item.factor_ref,),
            evidence_refs=item.evidence_refs,
        )
        for item in ordered_factors
    ]
    traceability.extend(
        TraceabilityEntry(
            output_ref=f"completed:{item.check_ref}",
            check_refs=(item.check_ref,),
            evidence_refs=item.evidence_refs,
        )
        for item in completed
    )
    traceability.extend(
        TraceabilityEntry(
            output_ref=f"limitation:{index}:{item.limitation_code}",
            check_refs=(item.origin_check_ref,) if item.origin_check_ref else (),
            factor_refs=(item.fact_ref,) if item.fact_ref else (),
            evidence_refs=item.evidence_refs,
        )
        for index, item in enumerate(assessment.limitations)
    )

    result = SummaryV3(
        summary_id=summary_id,
        company_id=assessment.company_id,
        risk_assessment_id=assessment.assessment_id,
        summary_model_version=summary_model_version,
        projection_policy_version=projection_policy_version,
        generated_at=generated_at,
        overall_conclusion=SummaryConclusion(
            result=assessment.overall_result,
            wording_key="summary." + assessment.overall_result.value.lower(),
            positive_conclusion_allowed=assessment.mandatory_gate.allowed
            and not assessment.factors,
        ),
        key_reason_refs=key_reason_refs,
        completed_checks_without_adverse_finding=completed,
        confirmed_positive_facts=tuple(
            sorted(
                positive_facts,
                key=lambda item: (item.fact_code, item.origin_check_ref),
            )
        ),
        limitations=assessment.limitations,
        recommendations=tuple(unique_recommendations),
        traceability=tuple(traceability),
    )
    LOGGER.info(
        "summary_v3_generated",
        extra={
            "summary_id": summary_id,
            "risk_assessment_id": assessment.assessment_id,
            "summary_model_version": summary_model_version,
            "generated_at": generated_at.isoformat(),
            "limitation_count": len(result.limitations),
            "recommendation_count": len(result.recommendations),
        },
    )
    return result
