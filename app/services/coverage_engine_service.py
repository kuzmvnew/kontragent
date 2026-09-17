"""Coverage Engine v2: weighted completeness independent of business risk."""

from __future__ import annotations

from collections.abc import Mapping

from app.contracts.risk import RiskProfile
from app.contracts.source_architecture import (
    CoverageAssessmentV2,
    CoverageBreakdown,
    NormalizedCheckResult,
    NormalizedResultStatus,
    SourceClass,
)
from app.services.source_capability_catalog import CATALOG, SourceCapabilityCatalog


def build_coverage_v2(
    resolved: Mapping[str, NormalizedCheckResult], *, profile: RiskProfile,
    catalog: SourceCapabilityCatalog = CATALOG,
) -> CoverageAssessmentV2:
    capabilities = catalog.for_profile(profile)
    mandatory_capabilities = tuple(
        item for item in capabilities if profile in item.mandatory_for_profiles
    )
    workflow_unattempted = tuple(
        item.capability_id
        for item in mandatory_capabilities
        if item.capability_id not in resolved
    )
    workflow_total = len(mandatory_capabilities)
    workflow_completed = workflow_total - len(workflow_unattempted)
    workflow_percent = (
        round(100 * workflow_completed / workflow_total) if workflow_total else 100
    )
    applicable = []
    not_applicable = []
    for capability in capabilities:
        result = resolved.get(capability.capability_id)
        if result and result.result == NormalizedResultStatus.NOT_APPLICABLE:
            not_applicable.append(capability.capability_id)
        else:
            applicable.append(capability)
    denominator = sum(item.coverage_weight for item in applicable)
    numerator = 0.0
    mandatory_denominator = 0.0
    mandatory_numerator = 0.0
    resolved_codes, unresolved, partial = [], [], []
    breakdown = {"official_direct":0,"official_datasets":0,"authorized_bridge":0,"partial":0,"unresolved":0,"not_applicable":len(not_applicable)}
    for capability in applicable:
        item = resolved.get(capability.capability_id)
        mandatory = profile in capability.mandatory_for_profiles
        if mandatory:
            mandatory_denominator += capability.coverage_weight
        factor = 0.0
        if item and item.result in {NormalizedResultStatus.FOUND, NormalizedResultStatus.NOT_FOUND}:
            bridge_valid = item.source_class != SourceClass.AUTHORIZED_BRIDGE or (
                capability.bridge_allowed and (
                    item.result != NormalizedResultStatus.NOT_FOUND or capability.negative_bridge_allowed
                )
            )
            if item.source_class != SourceClass.DISCOVERY_ONLY and bridge_valid:
                factor = item.coverage
        elif item and item.result == NormalizedResultStatus.PARTIAL:
            factor = item.coverage
        numerator += capability.coverage_weight * factor
        if mandatory:
            mandatory_numerator += capability.coverage_weight * factor
        if factor >= 1:
            resolved_codes.append(capability.capability_id)
            key = {
                SourceClass.OFFICIAL_DIRECT:"official_direct",
                SourceClass.OFFICIAL_DOWNLOADED_DATASET:"official_datasets",
                SourceClass.AUTHORIZED_BRIDGE:"authorized_bridge",
            }.get(item.source_class)
            if key: breakdown[key] += 1
        elif factor > 0:
            partial.append(capability.capability_id); breakdown["partial"] += 1
        else:
            unresolved.append(capability.capability_id); breakdown["unresolved"] += 1
    coverage = round(100 * numerator / denominator) if denominator else 100
    mandatory_score = round(100 * mandatory_numerator / mandatory_denominator) if mandatory_denominator else 100
    mandatory_unresolved = [
        item.capability_id for item in applicable
        if profile in item.mandatory_for_profiles and item.capability_id not in resolved_codes
    ]
    return CoverageAssessmentV2(
        profile=profile, coverage_score=coverage, mandatory_score=mandatory_score,
        mandatory_hard_checks_resolved=not mandatory_unresolved,
        workflow_completion_percent=workflow_percent,
        workflow_completed=workflow_completed,
        workflow_total=workflow_total,
        workflow_unattempted_capabilities=workflow_unattempted,
        resolved_capabilities=tuple(resolved_codes), unresolved_capabilities=tuple(unresolved),
        partial_capabilities=tuple(partial), not_applicable_capabilities=tuple(not_applicable),
        breakdown=CoverageBreakdown(**breakdown),
        calculation=f"round(100 × {numerator:g} / {denominator:g}) = {coverage}",
    )
