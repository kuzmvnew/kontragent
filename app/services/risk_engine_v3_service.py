"""Pure normalized Risk v3 calculation and capability-specific resolution.

The service consumes a bounded tuple of already persisted evidence candidates.
It never imports providers, parsers, aggregators, or refresh services.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from time import monotonic
from typing import Any, Iterable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from app.contracts.risk_v3 import (
    Actionability,
    Applicability,
    ApplicabilityDecision,
    AssessmentStatus,
    BlockingCheck,
    BlockingReason,
    BusinessMateriality,
    CoverageSnapshot,
    Execution,
    Freshness,
    Limitation,
    MandatoryGateResult,
    MaterialityState,
    NormalizedEvidenceCandidate,
    Observation,
    OverallRiskResult,
    Recency,
    ResolvedCheckResult,
    ResolutionState,
    RiskAssessmentV3,
    RiskFactor,
    RiskSeverity,
    ScopeCompleteness,
    SourceClass,
    SubjectScope,
    SubstitutionMetadata,
    TemporalKind,
    UnsupportedSubjectOutcome,
)


LOGGER = logging.getLogger(__name__)
POLICY_PATH = Path(__file__).resolve().parents[1] / "risk_rules" / "v3.json"
SOURCE_PRECEDENCE = {
    SourceClass.OFFICIAL_DIRECT: 0,
    SourceClass.OFFICIAL_DOWNLOADED_DATASET: 1,
    SourceClass.AUTHORIZED_BRIDGE: 2,
    SourceClass.DISCOVERY_ONLY: 3,
}


@dataclass(frozen=True)
class CapabilityPolicy:
    code: str
    fact_identity: str
    mandatory_mode: str
    default_applicability: Applicability
    not_applicable_allowed: bool
    applicability_rule_id: str | None
    applicability_rule_version: str | None
    allowed_source_classes: frozenset[SourceClass]
    found_source_classes: frozenset[SourceClass]
    negative_closure_source_classes: frozenset[SourceClass]
    exact_identity_required: bool
    bridge_substitution_allowed: bool
    bridge_negative_closure_allowed: bool
    temporal_kind: TemporalKind
    expected_scope: ScopeCompleteness
    factor_rule: Mapping[str, Any] | None


@dataclass(frozen=True)
class RiskV3Policy:
    risk_model_version: str
    ruleset_version: str
    coverage_policy_version: str
    applicability_policy_version: str
    source_resolution_policy_version: str
    freshness_policy_version: str
    subject_scope: SubjectScope
    capabilities: tuple[CapabilityPolicy, ...]

    @property
    def by_code(self) -> dict[str, CapabilityPolicy]:
        return {item.code: item for item in self.capabilities}


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


@lru_cache(maxsize=1)
def load_risk_v3_policy() -> RiskV3Policy:
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    capabilities = tuple(
        CapabilityPolicy(
            code=item["code"],
            fact_identity=item["fact_identity"],
            mandatory_mode=item["mandatory_mode"],
            default_applicability=Applicability(item["default_applicability"]),
            not_applicable_allowed=bool(
                item.get("not_applicable_allowed", False)
            ),
            applicability_rule_id=item.get("applicability_rule_id"),
            applicability_rule_version=item.get("applicability_rule_version"),
            allowed_source_classes=frozenset(
                SourceClass(value) for value in item["allowed_source_classes"]
            ),
            found_source_classes=frozenset(
                SourceClass(value) for value in item["found_source_classes"]
            ),
            negative_closure_source_classes=frozenset(
                SourceClass(value)
                for value in item["negative_closure_source_classes"]
            ),
            exact_identity_required=bool(item["exact_identity_required"]),
            bridge_substitution_allowed=bool(
                item["bridge_substitution_allowed"]
            ),
            bridge_negative_closure_allowed=bool(
                item["bridge_negative_closure_allowed"]
            ),
            temporal_kind=TemporalKind(item["temporal_kind"]),
            expected_scope=ScopeCompleteness(item["expected_scope"]),
            factor_rule=item.get("factor_rule"),
        )
        for item in raw["capabilities"]
    )
    codes = [item.code for item in capabilities]
    if len(codes) != len(set(codes)):
        raise ValueError("Risk v3 capability policy contains duplicate codes")
    for item in capabilities:
        if item.not_applicable_allowed:
            if item.mandatory_mode != "IF_APPLICABLE":
                raise ValueError(
                    f"{item.code}: NOT_APPLICABLE is only valid for IF_APPLICABLE"
                )
            if not (item.applicability_rule_id and item.applicability_rule_version):
                raise ValueError(
                    f"{item.code}: NOT_APPLICABLE requires a versioned rule"
                )
        elif item.applicability_rule_id or item.applicability_rule_version:
            raise ValueError(
                f"{item.code}: applicability rule requires NOT_APPLICABLE permission"
            )
    return RiskV3Policy(
        risk_model_version=raw["risk_model_version"],
        ruleset_version=raw["ruleset_version"],
        coverage_policy_version=raw["coverage_policy_version"],
        applicability_policy_version=raw["applicability_policy_version"],
        source_resolution_policy_version=raw[
            "source_resolution_policy_version"
        ],
        freshness_policy_version=raw["freshness_policy_version"],
        subject_scope=SubjectScope(raw["subject_scope"]),
        capabilities=capabilities,
    )


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _limitation(
    code: str,
    check_ref: str,
    *,
    evidence_refs: Iterable[str] = (),
    blocks: bool = True,
    parameters: Mapping[str, Any] | None = None,
) -> Limitation:
    return Limitation(
        limitation_code=code,
        origin_check_ref=check_ref,
        parameters=dict(parameters or {}),
        blocks_positive_conclusion=blocks,
        evidence_refs=_unique(evidence_refs),
    )


def _latest_cohort(
    candidates: Sequence[NormalizedEvidenceCandidate],
) -> tuple[NormalizedEvidenceCandidate, ...]:
    """Return semantically comparable current candidates.

    A superseded observation with an older effective/source date is historical,
    not a conflict. Undated candidates remain comparable with one another.
    """

    dated = [
        (
            item.effective_at or item.source_as_of or item.checked_at,
            item,
        )
        for item in candidates
    ]
    concrete = [stamp for stamp, _ in dated if stamp is not None]
    if not concrete:
        return tuple(candidates)
    latest = max(concrete)
    return tuple(item for stamp, item in dated if stamp == latest)


def _candidate_rejection(
    item: NormalizedEvidenceCandidate,
    policy: CapabilityPolicy,
    check_ref: str,
) -> Limitation | None:
    if item.observation == Observation.NOT_FOUND:
        if (
            item.source_class not in policy.negative_closure_source_classes
            or not item.negative_closure_capable
            or (
                item.source_class == SourceClass.AUTHORIZED_BRIDGE
                and not policy.bridge_negative_closure_allowed
            )
        ):
            return _limitation(
                BlockingReason.NEGATIVE_CLOSURE_NOT_PROVEN.value,
                check_ref,
                evidence_refs=item.evidence_refs,
            )
    if item.source_class not in policy.allowed_source_classes:
        return _limitation(
            "SOURCE_CLASS_NOT_ALLOWED",
            check_ref,
            evidence_refs=item.evidence_refs,
            parameters={"source_class": item.source_class},
        )
    if policy.exact_identity_required and not item.exact_identity_match:
        return _limitation(
            "EXACT_IDENTITY_NOT_PROVEN",
            check_ref,
            evidence_refs=item.evidence_refs,
        )
    if (
        item.source_class == SourceClass.AUTHORIZED_BRIDGE
        and not policy.bridge_substitution_allowed
    ):
        return _limitation(
            "BRIDGE_SUBSTITUTION_NOT_ALLOWED",
            check_ref,
            evidence_refs=item.evidence_refs,
        )
    if (
        item.observation == Observation.FOUND
        and item.source_class not in policy.found_source_classes
    ):
        return _limitation(
            "SOURCE_CANNOT_PROVE_FOUND",
            check_ref,
            evidence_refs=item.evidence_refs,
        )
    return None


def _resolution_limitations(
    check_ref: str,
    *,
    applicability: Applicability,
    observation: Observation,
    execution: Execution,
    freshness: Freshness,
    scope: ScopeCompleteness,
    temporal_kind: TemporalKind,
    negative_closure_proven: bool,
    evidence_refs: Iterable[str],
) -> tuple[Limitation, ...]:
    limitations: list[Limitation] = []
    if applicability == Applicability.APPLICABILITY_UNKNOWN:
        limitations.append(
            _limitation(BlockingReason.APPLICABILITY_UNKNOWN.value, check_ref)
        )
    execution_reason = {
        Execution.NOT_CHECKED: BlockingReason.NOT_CHECKED,
        Execution.SOURCE_UNAVAILABLE: BlockingReason.SOURCE_UNAVAILABLE,
        Execution.TIMEOUT: BlockingReason.TIMEOUT,
        Execution.PARSING_ERROR: BlockingReason.PARSING_ERROR,
    }.get(execution)
    if execution_reason:
        limitations.append(_limitation(execution_reason.value, check_ref))
    elif applicability == Applicability.APPLICABLE and observation == Observation.UNKNOWN:
        limitations.append(
            _limitation(BlockingReason.OBSERVATION_UNKNOWN.value, check_ref)
        )
    if execution == Execution.CHECKED and scope == ScopeCompleteness.PARTIAL:
        limitations.append(
            _limitation(
                BlockingReason.PARTIAL_SCOPE.value,
                check_ref,
                evidence_refs=evidence_refs,
            )
        )
    elif (
        execution == Execution.CHECKED
        and scope == ScopeCompleteness.UNKNOWN
        and applicability == Applicability.APPLICABLE
    ):
        limitations.append(
            _limitation(
                BlockingReason.SCOPE_UNKNOWN.value,
                check_ref,
                evidence_refs=evidence_refs,
            )
        )
    if (
        execution == Execution.CHECKED
        and temporal_kind == TemporalKind.CURRENT_STATE
        and freshness == Freshness.STALE
    ):
        limitations.append(
            _limitation(
                BlockingReason.STALE_CURRENT_STATE.value,
                check_ref,
                evidence_refs=evidence_refs,
            )
        )
    elif (
        execution == Execution.CHECKED
        and temporal_kind == TemporalKind.CURRENT_STATE
        and freshness == Freshness.UNKNOWN
        and applicability == Applicability.APPLICABLE
    ):
        limitations.append(
            _limitation(
                BlockingReason.FRESHNESS_UNKNOWN.value,
                check_ref,
                evidence_refs=evidence_refs,
            )
        )
    if observation == Observation.NOT_FOUND and not negative_closure_proven:
        limitations.append(
            _limitation(
                BlockingReason.NEGATIVE_CLOSURE_NOT_PROVEN.value,
                check_ref,
                evidence_refs=evidence_refs,
            )
        )
    return tuple(limitations)


def _dedupe_limitations(values: Iterable[Limitation]) -> tuple[Limitation, ...]:
    unique: dict[str, Limitation] = {}
    for value in values:
        unique.setdefault(_canonical(value.model_dump(mode="json")), value)
    return tuple(sorted(unique.values(), key=lambda item: (
        item.limitation_code,
        item.origin_check_ref or "",
        item.fact_ref or "",
    )))


def _unresolved_without_evidence(
    company_id: int,
    policy: CapabilityPolicy,
    resolution_policy_version: str,
) -> ResolvedCheckResult:
    check_ref = f"risk-v3:{company_id}:{policy.code}"
    limitations = _resolution_limitations(
        check_ref,
        applicability=policy.default_applicability,
        observation=Observation.UNKNOWN,
        execution=Execution.NOT_CHECKED,
        freshness=Freshness.UNKNOWN,
        scope=ScopeCompleteness.UNKNOWN,
        temporal_kind=policy.temporal_kind,
        negative_closure_proven=False,
        evidence_refs=(),
    )
    return ResolvedCheckResult(
        check_ref=check_ref,
        capability_code=policy.code,
        fact_identity=policy.fact_identity,
        company_id=company_id,
        applicability=policy.default_applicability,
        observation=Observation.UNKNOWN,
        execution=Execution.NOT_CHECKED,
        freshness=Freshness.UNKNOWN,
        scope=ScopeCompleteness.UNKNOWN,
        temporal_kind=policy.temporal_kind,
        resolution_state=ResolutionState.UNRESOLVED,
        limitations=limitations,
        resolution_policy_version=resolution_policy_version,
    )


def _validate_applicability_candidate(
    item: NormalizedEvidenceCandidate,
    policy: CapabilityPolicy,
    subject_scope: SubjectScope | None,
) -> NormalizedEvidenceCandidate:
    """Fail closed before applicability can affect coverage or the gate."""

    if item.applicability != Applicability.NOT_APPLICABLE:
        return item

    decision = item.applicability_decision
    failures: list[str] = []
    if not policy.not_applicable_allowed:
        failures.append("CAPABILITY_POLICY_DISALLOWS_NOT_APPLICABLE")
    if subject_scope is None or subject_scope != SubjectScope.LEGAL_ENTITY:
        failures.append("SUBJECT_SCOPE_NOT_CONFIRMED")
    if decision is None:
        failures.append("APPLICABILITY_DECISION_MISSING")
    else:
        if decision.subject_scope != subject_scope:
            failures.append("SUBJECT_SCOPE_MISMATCH")
        if not (
            policy.applicability_rule_id
            and policy.applicability_rule_version
            and decision.rule_id == policy.applicability_rule_id
            and decision.rule_version == policy.applicability_rule_version
        ):
            failures.append("APPLICABILITY_RULE_NOT_PROVEN")
        if decision.based_on_data_absence:
            failures.append("DECISION_BASED_ON_DATA_ABSENCE")
        if all(
            source_class == SourceClass.DISCOVERY_ONLY
            for source_class in decision.source_classes
        ):
            failures.append("DISCOVERY_ONLY_PROVENANCE")

    if not failures:
        return item

    evidence_refs = (
        (*item.evidence_refs, *decision.evidence_refs)
        if decision is not None
        else item.evidence_refs
    )
    limitation = _limitation(
        BlockingReason.APPLICABILITY_UNKNOWN.value,
        item.candidate_ref,
        evidence_refs=evidence_refs,
        parameters={"validation_failures": tuple(sorted(set(failures)))},
    )
    return item.model_copy(
        update={
            "applicability": Applicability.APPLICABILITY_UNKNOWN,
            "applicability_decision": None,
            "observation": Observation.UNKNOWN,
            "freshness": Freshness.UNKNOWN,
            "scope": ScopeCompleteness.UNKNOWN,
            "fact_payload": {},
            "limitations": (*item.limitations, limitation),
        }
    )


def _resolve_capability(
    company_id: int,
    policy: CapabilityPolicy,
    candidates: Sequence[NormalizedEvidenceCandidate],
    resolution_policy_version: str,
) -> ResolvedCheckResult:
    if not candidates:
        return _unresolved_without_evidence(
            company_id, policy, resolution_policy_version
        )

    check_ref = f"risk-v3:{company_id}:{policy.code}"
    ordered = tuple(sorted(candidates, key=lambda item: item.candidate_ref))
    candidate_refs = _unique(item.candidate_ref for item in ordered)
    all_evidence_refs = _unique(
        ref for item in ordered for ref in item.evidence_refs
    )
    all_limitations = [
        limitation.model_copy(
            update={
                "origin_check_ref": check_ref,
                "parameters": {
                    **limitation.parameters,
                    "candidate_origin_ref": limitation.origin_check_ref,
                },
            }
        )
        for item in ordered
        for limitation in item.limitations
    ]

    applicability_values = {item.applicability for item in ordered}
    if (
        Applicability.APPLICABLE in applicability_values
        and Applicability.NOT_APPLICABLE in applicability_values
    ):
        conflict_refs = all_evidence_refs or candidate_refs
        limitation = _limitation(
            BlockingReason.CONFLICTING_EVIDENCE.value,
            check_ref,
            evidence_refs=conflict_refs,
            parameters={"axis": "applicability"},
        )
        return ResolvedCheckResult(
            check_ref=check_ref,
            capability_code=policy.code,
            fact_identity=policy.fact_identity,
            company_id=company_id,
            applicability=Applicability.APPLICABILITY_UNKNOWN,
            observation=Observation.UNKNOWN,
            execution=Execution.CHECKED,
            freshness=Freshness.UNKNOWN,
            scope=ScopeCompleteness.UNKNOWN,
            temporal_kind=policy.temporal_kind,
            resolution_state=ResolutionState.CONFLICTING_EVIDENCE,
            candidate_refs=candidate_refs,
            conflict_refs=conflict_refs,
            limitations=_dedupe_limitations((*all_limitations, limitation)),
            resolution_policy_version=resolution_policy_version,
        )

    applicable = [
        item for item in ordered if item.applicability == Applicability.APPLICABLE
    ]
    if not applicable:
        if applicability_values == {Applicability.NOT_APPLICABLE}:
            decisions = tuple(
                item.applicability_decision
                for item in ordered
                if item.applicability_decision is not None
            )
            if len(decisions) != len(ordered):
                raise ValueError(
                    "Validated NOT_APPLICABLE candidates require provenance"
                )
            applicability_decision = ApplicabilityDecision(
                rule_id=policy.applicability_rule_id or "",
                rule_version=policy.applicability_rule_version or "",
                subject_scope=decisions[0].subject_scope,
                evidence_refs=_unique(
                    ref for decision in decisions for ref in decision.evidence_refs
                ),
                source_refs=_unique(
                    ref for decision in decisions for ref in decision.source_refs
                ),
                source_classes=tuple(
                    sorted(
                        {
                            source_class
                            for decision in decisions
                            for source_class in decision.source_classes
                        },
                        key=lambda value: value.value,
                    )
                ),
                based_on_data_absence=False,
                decided_at=max(decision.decided_at for decision in decisions),
            )
            return ResolvedCheckResult(
                check_ref=check_ref,
                capability_code=policy.code,
                fact_identity=policy.fact_identity,
                company_id=company_id,
                applicability=Applicability.NOT_APPLICABLE,
                applicability_decision=applicability_decision,
                observation=Observation.UNKNOWN,
                execution=Execution.CHECKED,
                freshness=Freshness.UNKNOWN,
                scope=ScopeCompleteness.COMPLETE,
                temporal_kind=policy.temporal_kind,
                resolution_state=ResolutionState.RESOLVED,
                selected_evidence_refs=_unique(
                    (*all_evidence_refs, *applicability_decision.evidence_refs)
                ),
                candidate_refs=candidate_refs,
                source_refs=_unique(
                    (
                        *(item.source_code for item in ordered),
                        *applicability_decision.source_refs,
                    )
                ),
                checked_at=max(
                    (item.checked_at for item in ordered if item.checked_at),
                    default=None,
                ),
                limitations=_dedupe_limitations(all_limitations),
                resolution_policy_version=resolution_policy_version,
            )
        applicability = Applicability.APPLICABILITY_UNKNOWN
        execution = next(
            (
                item.execution
                for item in ordered
                if item.execution != Execution.CHECKED
            ),
            Execution.CHECKED,
        )
        limitations = _resolution_limitations(
            check_ref,
            applicability=applicability,
            observation=Observation.UNKNOWN,
            execution=execution,
            freshness=Freshness.UNKNOWN,
            scope=ScopeCompleteness.UNKNOWN,
            temporal_kind=policy.temporal_kind,
            negative_closure_proven=False,
            evidence_refs=all_evidence_refs,
        )
        return ResolvedCheckResult(
            check_ref=check_ref,
            capability_code=policy.code,
            fact_identity=policy.fact_identity,
            company_id=company_id,
            applicability=applicability,
            observation=Observation.UNKNOWN,
            execution=execution,
            freshness=Freshness.UNKNOWN,
            scope=ScopeCompleteness.UNKNOWN,
            temporal_kind=policy.temporal_kind,
            resolution_state=ResolutionState.UNRESOLVED,
            candidate_refs=candidate_refs,
            source_refs=_unique(item.source_code for item in ordered),
            limitations=_dedupe_limitations((*all_limitations, *limitations)),
            resolution_policy_version=resolution_policy_version,
        )

    usable: list[NormalizedEvidenceCandidate] = []
    rejection_limitations: list[Limitation] = []
    for item in applicable:
        rejection = _candidate_rejection(item, policy, check_ref)
        if rejection is not None:
            rejection_limitations.append(rejection)
        elif item.observation in {Observation.FOUND, Observation.NOT_FOUND}:
            usable.append(item)

    cohort = _latest_cohort(usable)
    if cohort:
        semantic_values = {
            (item.observation.value, _canonical(item.fact_payload)) for item in cohort
        }
        if len(semantic_values) > 1:
            conflict_refs = _unique(
                ref for item in cohort for ref in item.evidence_refs
            ) or _unique(item.candidate_ref for item in cohort)
            limitation = _limitation(
                BlockingReason.CONFLICTING_EVIDENCE.value,
                check_ref,
                evidence_refs=conflict_refs,
            )
            return ResolvedCheckResult(
                check_ref=check_ref,
                capability_code=policy.code,
                fact_identity=policy.fact_identity,
                company_id=company_id,
                applicability=Applicability.APPLICABLE,
                observation=Observation.UNKNOWN,
                execution=Execution.CHECKED,
                freshness=Freshness.UNKNOWN,
                scope=ScopeCompleteness.UNKNOWN,
                temporal_kind=policy.temporal_kind,
                resolution_state=ResolutionState.CONFLICTING_EVIDENCE,
                candidate_refs=candidate_refs,
                source_refs=_unique(item.source_code for item in cohort),
                conflict_refs=conflict_refs,
                checked_at=max(
                    (item.checked_at for item in cohort if item.checked_at),
                    default=None,
                ),
                limitations=_dedupe_limitations(
                    (*all_limitations, *rejection_limitations, limitation)
                ),
                resolution_policy_version=resolution_policy_version,
            )

        # Precedence selects representation only after semantic equivalence is
        # proven. Incompatible authoritative facts take the conflict path above.
        representative = min(
            cohort,
            key=lambda item: (
                SOURCE_PRECEDENCE[item.source_class],
                item.candidate_ref,
            ),
        )
        observation = representative.observation
        evidence_refs = _unique(
            ref for item in cohort for ref in item.evidence_refs
        )
        source_refs = _unique(item.source_code for item in cohort)
        scope = (
            ScopeCompleteness.COMPLETE
            if any(item.scope == ScopeCompleteness.COMPLETE for item in cohort)
            else ScopeCompleteness.PARTIAL
            if any(item.scope == ScopeCompleteness.PARTIAL for item in cohort)
            else ScopeCompleteness.UNKNOWN
        )
        freshness = (
            Freshness.CURRENT
            if any(item.freshness == Freshness.CURRENT for item in cohort)
            else Freshness.STALE
            if any(item.freshness == Freshness.STALE for item in cohort)
            else Freshness.UNKNOWN
        )
        negative_closure = observation == Observation.NOT_FOUND and any(
            item.negative_closure_capable
            and item.source_class in policy.negative_closure_source_classes
            for item in cohort
        )
        current_enough = (
            policy.temporal_kind == TemporalKind.HISTORICAL_EVENT
            or freshness == Freshness.CURRENT
        )
        resolved = (
            scope == policy.expected_scope
            and current_enough
            and (observation != Observation.NOT_FOUND or negative_closure)
        )
        bridge_only = all(
            item.source_class == SourceClass.AUTHORIZED_BRIDGE for item in cohort
        )
        substitution = SubstitutionMetadata(
            substituted=bridge_only,
            source_code=representative.source_code if bridge_only else None,
            source_class=representative.source_class if bridge_only else None,
            policy_basis=(
                resolution_policy_version + ":" + policy.code
                if bridge_only
                else None
            ),
        )
        bridge_limitation = (
            _limitation(
                "AUTHORIZED_BRIDGE_SUBSTITUTION",
                check_ref,
                evidence_refs=evidence_refs,
                blocks=False,
                parameters={"source_code": representative.source_code},
            )
            if bridge_only
            else None
        )
        generated = _resolution_limitations(
            check_ref,
            applicability=Applicability.APPLICABLE,
            observation=observation,
            execution=Execution.CHECKED,
            freshness=freshness,
            scope=scope,
            temporal_kind=policy.temporal_kind,
            negative_closure_proven=negative_closure,
            evidence_refs=evidence_refs,
        )
        limitation_values = [
            *all_limitations,
            *rejection_limitations,
            *generated,
        ]
        if bridge_limitation:
            limitation_values.append(bridge_limitation)
        return ResolvedCheckResult(
            check_ref=check_ref,
            capability_code=policy.code,
            fact_identity=policy.fact_identity,
            company_id=company_id,
            applicability=Applicability.APPLICABLE,
            observation=observation,
            execution=Execution.CHECKED,
            freshness=freshness,
            scope=scope,
            scope_details=representative.scope_details,
            temporal_kind=policy.temporal_kind,
            resolution_state=(
                ResolutionState.RESOLVED if resolved else ResolutionState.UNRESOLVED
            ),
            selected_evidence_refs=evidence_refs,
            candidate_refs=candidate_refs,
            source_refs=source_refs,
            substitution=substitution,
            negative_closure_proven=negative_closure,
            fact_payload=representative.fact_payload,
            source_as_of=max(
                (item.source_as_of for item in cohort if item.source_as_of),
                default=None,
            ),
            effective_at=max(
                (item.effective_at for item in cohort if item.effective_at),
                default=None,
            ),
            checked_at=max(
                (item.checked_at for item in cohort if item.checked_at),
                default=None,
            ),
            limitations=_dedupe_limitations(limitation_values),
            resolution_policy_version=resolution_policy_version,
        )

    execution_order = {
        Execution.SOURCE_UNAVAILABLE: 0,
        Execution.TIMEOUT: 1,
        Execution.PARSING_ERROR: 2,
        Execution.NOT_CHECKED: 3,
        Execution.CHECKED: 4,
    }
    representative = min(
        applicable,
        key=lambda item: (execution_order[item.execution], item.candidate_ref),
    )
    generated = _resolution_limitations(
        check_ref,
        applicability=Applicability.APPLICABLE,
        observation=Observation.UNKNOWN,
        execution=representative.execution,
        freshness=representative.freshness,
        scope=representative.scope,
        temporal_kind=policy.temporal_kind,
        negative_closure_proven=False,
        evidence_refs=all_evidence_refs,
    )
    return ResolvedCheckResult(
        check_ref=check_ref,
        capability_code=policy.code,
        fact_identity=policy.fact_identity,
        company_id=company_id,
        applicability=Applicability.APPLICABLE,
        observation=Observation.UNKNOWN,
        execution=representative.execution,
        freshness=representative.freshness,
        scope=representative.scope,
        scope_details=representative.scope_details,
        temporal_kind=policy.temporal_kind,
        resolution_state=ResolutionState.UNRESOLVED,
        candidate_refs=candidate_refs,
        source_refs=_unique(item.source_code for item in applicable),
        checked_at=max(
            (item.checked_at for item in applicable if item.checked_at),
            default=None,
        ),
        limitations=_dedupe_limitations(
            (*all_limitations, *rejection_limitations, *generated)
        ),
        resolution_policy_version=resolution_policy_version,
    )


def resolve_evidence_candidates(
    candidates: Sequence[NormalizedEvidenceCandidate],
    *,
    company_id: int,
    policy: RiskV3Policy | None = None,
    subject_scope: SubjectScope | None = None,
) -> tuple[ResolvedCheckResult, ...]:
    """Resolve evidence by capability semantics, never by rank alone."""

    policy = policy or load_risk_v3_policy()
    if any(item.company_id != company_id for item in candidates):
        raise ValueError("All evidence candidates must belong to one company")
    unknown_codes = sorted(
        {item.capability_code for item in candidates} - set(policy.by_code)
    )
    if unknown_codes:
        raise ValueError("Unknown Risk v3 capabilities: " + ", ".join(unknown_codes))
    grouped = {
        item.code: tuple(
            _validate_applicability_candidate(candidate, item, subject_scope)
            for candidate in candidates
            if candidate.capability_code == item.code
        )
        for item in policy.capabilities
    }
    return tuple(
        _resolve_capability(
            company_id,
            item,
            grouped[item.code],
            policy.source_resolution_policy_version,
        )
        for item in sorted(policy.capabilities, key=lambda value: value.code)
    )


def _coverage(
    checks: Sequence[ResolvedCheckResult],
    policy: RiskV3Policy,
    calculated_at: datetime,
) -> CoverageSnapshot:
    policies = policy.by_code
    applicable = _unique(
        item.capability_code
        for item in checks
        if item.applicability == Applicability.APPLICABLE
        or policies[item.capability_code].mandatory_mode == "ALWAYS"
        or (
            item.applicability == Applicability.APPLICABILITY_UNKNOWN
            and policies[item.capability_code].mandatory_mode == "IF_APPLICABLE"
        )
    )
    resolved = _unique(
        item.capability_code
        for item in checks
        if item.applicability == Applicability.APPLICABLE
        and item.resolution_state == ResolutionState.RESOLVED
    )
    unresolved = tuple(sorted(set(applicable) - set(resolved)))
    partial = _unique(
        item.capability_code
        for item in checks
        if item.applicability == Applicability.APPLICABLE
        and item.scope == ScopeCompleteness.PARTIAL
    )
    applicability_unknown = _unique(
        item.capability_code
        for item in checks
        if item.applicability == Applicability.APPLICABILITY_UNKNOWN
    )
    mandatory_applicable = _unique(
        item.capability_code
        for item in checks
        if policies[item.capability_code].mandatory_mode == "ALWAYS"
        or (
            policies[item.capability_code].mandatory_mode == "IF_APPLICABLE"
            and item.applicability
            in {Applicability.APPLICABLE, Applicability.APPLICABILITY_UNKNOWN}
        )
    )
    mandatory_resolved = tuple(
        code for code in mandatory_applicable if code in set(resolved)
    )
    numerator = len(resolved)
    denominator = len(applicable)
    percentage = (
        Decimal("0.00")
        if denominator == 0
        else (Decimal(numerator) * 100 / Decimal(denominator)).quantize(
            Decimal("0.01")
        )
    )
    return CoverageSnapshot(
        applicable_codes=applicable,
        resolved_codes=resolved,
        unresolved_codes=unresolved,
        partial_codes=partial,
        applicability_unknown_codes=applicability_unknown,
        numerator=numerator,
        denominator=denominator,
        percentage=percentage,
        mandatory_applicable_codes=mandatory_applicable,
        mandatory_resolved_codes=mandatory_resolved,
        mandatory_numerator=len(mandatory_resolved),
        mandatory_denominator=len(mandatory_applicable),
        calculated_at=calculated_at,
        coverage_policy_version=policy.coverage_policy_version,
    )


def _blocking_reasons(check: ResolvedCheckResult) -> tuple[BlockingReason, ...]:
    if check.resolution_state == ResolutionState.CONFLICTING_EVIDENCE:
        return (BlockingReason.CONFLICTING_EVIDENCE,)
    reasons: list[BlockingReason] = []
    if check.applicability == Applicability.APPLICABILITY_UNKNOWN:
        reasons.append(BlockingReason.APPLICABILITY_UNKNOWN)
    execution_reason = {
        Execution.NOT_CHECKED: BlockingReason.NOT_CHECKED,
        Execution.SOURCE_UNAVAILABLE: BlockingReason.SOURCE_UNAVAILABLE,
        Execution.TIMEOUT: BlockingReason.TIMEOUT,
        Execution.PARSING_ERROR: BlockingReason.PARSING_ERROR,
    }.get(check.execution)
    if execution_reason:
        reasons.append(execution_reason)
    elif (
        check.applicability == Applicability.APPLICABLE
        and check.observation == Observation.UNKNOWN
    ):
        if any(
            item.limitation_code
            == BlockingReason.NEGATIVE_CLOSURE_NOT_PROVEN.value
            for item in check.limitations
        ):
            reasons.append(BlockingReason.NEGATIVE_CLOSURE_NOT_PROVEN)
        else:
            reasons.append(BlockingReason.OBSERVATION_UNKNOWN)
    if (
        check.execution == Execution.CHECKED
        and check.temporal_kind == TemporalKind.CURRENT_STATE
        and check.freshness == Freshness.STALE
    ):
        reasons.append(BlockingReason.STALE_CURRENT_STATE)
    elif (
        check.execution == Execution.CHECKED
        and check.temporal_kind == TemporalKind.CURRENT_STATE
        and check.freshness == Freshness.UNKNOWN
        and check.applicability == Applicability.APPLICABLE
    ):
        reasons.append(BlockingReason.FRESHNESS_UNKNOWN)
    if check.execution == Execution.CHECKED and check.scope == ScopeCompleteness.PARTIAL:
        reasons.append(BlockingReason.PARTIAL_SCOPE)
    elif (
        check.execution == Execution.CHECKED
        and check.scope == ScopeCompleteness.UNKNOWN
        and check.applicability == Applicability.APPLICABLE
    ):
        reasons.append(BlockingReason.SCOPE_UNKNOWN)
    if (
        check.observation == Observation.NOT_FOUND
        and not check.negative_closure_proven
    ):
        reasons.append(BlockingReason.NEGATIVE_CLOSURE_NOT_PROVEN)
    order = {value: index for index, value in enumerate(BlockingReason)}
    return tuple(sorted(set(reasons), key=order.__getitem__))


def _mandatory_gate(
    checks: Sequence[ResolvedCheckResult],
    policy: RiskV3Policy,
    evaluated_at: datetime,
) -> MandatoryGateResult:
    policies = policy.by_code
    mandatory = [
        item
        for item in checks
        if policies[item.capability_code].mandatory_mode == "ALWAYS"
        or (
            policies[item.capability_code].mandatory_mode == "IF_APPLICABLE"
            and item.applicability
            in {Applicability.APPLICABLE, Applicability.APPLICABILITY_UNKNOWN}
        )
    ]
    mandatory_applicable = _unique(
        item.capability_code
        for item in mandatory
    )
    resolved = _unique(
        item.capability_code
        for item in mandatory
        if item.applicability == Applicability.APPLICABLE
        and item.resolution_state == ResolutionState.RESOLVED
    )
    blocking_values: list[BlockingCheck] = []
    for item in sorted(mandatory, key=lambda value: value.capability_code):
        reasons = _blocking_reasons(item)
        if (
            policies[item.capability_code].mandatory_mode == "ALWAYS"
            and item.applicability == Applicability.NOT_APPLICABLE
        ):
            reasons = tuple(
                sorted(
                    {*reasons, BlockingReason.APPLICABILITY_UNKNOWN},
                    key=list(BlockingReason).index,
                )
            )
        if reasons:
            blocking_values.append(
                BlockingCheck(
                    capability_code=item.capability_code,
                    reasons=reasons,
                    check_ref=item.check_ref,
                )
            )
    if not mandatory_applicable:
        company_id = checks[0].company_id if checks else "unknown"
        blocking_values.append(
            BlockingCheck(
                capability_code="__mandatory_policy__",
                reasons=(BlockingReason.EMPTY_MANDATORY_SET,),
                check_ref=f"risk-v3:{company_id}:mandatory-policy",
            )
        )
    blocking = tuple(blocking_values)
    return MandatoryGateResult(
        allowed=(
            bool(mandatory_applicable)
            and set(resolved) == set(mandatory_applicable)
            and not blocking
        ),
        policy_version=policy.applicability_policy_version,
        mandatory_applicable_codes=mandatory_applicable,
        resolved_codes=resolved,
        blocking_checks=blocking,
        evaluated_at=evaluated_at,
    )


def _path_value(payload: Mapping[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _materiality(payload: Mapping[str, Any]) -> BusinessMateriality:
    raw = payload.get("materiality")
    if not isinstance(raw, Mapping):
        return BusinessMateriality(state=MaterialityState.UNKNOWN)
    try:
        return BusinessMateriality.model_validate(raw)
    except ValueError:
        return BusinessMateriality(state=MaterialityState.UNKNOWN)


def _factors(
    checks: Sequence[ResolvedCheckResult],
    policy: RiskV3Policy,
) -> tuple[RiskFactor, ...]:
    policies = policy.by_code
    factors: list[RiskFactor] = []
    for check in checks:
        rule = policies[check.capability_code].factor_rule
        if (
            not rule
            or check.observation != Observation.FOUND
            or check.resolution_state == ResolutionState.CONFLICTING_EVIDENCE
        ):
            continue
        condition = rule["condition"]
        if _path_value(check.fact_payload, condition["path"]) != condition["equals"]:
            continue
        recency = (
            Recency.HISTORICAL
            if check.temporal_kind == TemporalKind.HISTORICAL_EVENT
            else Recency.CURRENT
            if check.freshness == Freshness.CURRENT
            else Recency.UNKNOWN
        )
        factor_ref = f"{check.check_ref}:factor:{rule['factor_code']}"
        factors.append(
            RiskFactor(
                factor_ref=factor_ref,
                factor_code=rule["factor_code"],
                underlying_check_ref=check.check_ref,
                severity=RiskSeverity(rule["severity"]),
                hard_blocker=bool(rule["hard_blocker"]),
                business_materiality=_materiality(check.fact_payload),
                recency=recency,
                actionability=Actionability(rule["actionability"]),
                rule_id=rule["rule_id"],
                rule_version=rule["rule_version"],
                recommendation_code=rule["recommendation_code"],
                wording_key=rule["wording_key"],
                evidence_refs=check.selected_evidence_refs,
                source_refs=check.source_refs,
                source_as_of=check.source_as_of,
                effective_at=check.effective_at,
                status_semantics="confirmed adverse fact; independent of coverage",
                parameters={
                    key: value
                    for key, value in check.fact_payload.items()
                    if key not in {"materiality"}
                },
            )
        )
    return tuple(sorted(factors, key=lambda item: item.factor_code))


def calculate_risk_v3(
    candidates: Sequence[NormalizedEvidenceCandidate],
    *,
    company_id: int,
    subject_scope: SubjectScope,
    calculated_at: datetime | None = None,
    policy: RiskV3Policy | None = None,
) -> RiskAssessmentV3 | UnsupportedSubjectOutcome:
    """Calculate one immutable Risk v3 result from a bounded snapshot."""

    started = monotonic()
    policy = policy or load_risk_v3_policy()
    calculated_at = calculated_at or datetime.now(timezone.utc)
    if calculated_at.tzinfo is None or calculated_at.utcoffset() is None:
        raise ValueError("calculated_at must include a timezone")
    if subject_scope != SubjectScope.LEGAL_ENTITY:
        return UnsupportedSubjectOutcome(
            company_id=company_id,
            subject_scope=subject_scope,
            applicability_policy_version=policy.applicability_policy_version,
            evaluated_at=calculated_at,
        )

    snapshot = tuple(
        sorted(candidates, key=lambda item: (item.capability_code, item.candidate_ref))
    )
    identity_payload = {
        "company_id": company_id,
        "subject_scope": subject_scope,
        "versions": {
            "risk_model_version": policy.risk_model_version,
            "ruleset_version": policy.ruleset_version,
            "coverage_policy_version": policy.coverage_policy_version,
            "applicability_policy_version": policy.applicability_policy_version,
            "source_resolution_policy_version": policy.source_resolution_policy_version,
            "freshness_policy_version": policy.freshness_policy_version,
        },
        "evidence": [item.model_dump(mode="json") for item in snapshot],
    }
    input_hash = hashlib.sha256(_canonical(identity_payload).encode()).hexdigest()
    assessment_id = str(uuid5(NAMESPACE_URL, "next.company:risk-v3:" + input_hash))
    checks = resolve_evidence_candidates(
        snapshot,
        company_id=company_id,
        policy=policy,
        subject_scope=subject_scope,
    )
    coverage = _coverage(checks, policy, calculated_at)
    gate = _mandatory_gate(checks, policy, calculated_at)
    factors = _factors(checks, policy)
    limitations = _dedupe_limitations(
        limitation for item in checks for limitation in item.limitations
    )
    overall = (
        OverallRiskResult.HARD_BLOCKER_PRESENT
        if any(item.hard_blocker for item in factors)
        else OverallRiskResult.RISK_FACTORS_PRESENT
        if factors
        else OverallRiskResult.NO_ADVERSE_FACTORS_AFTER_MANDATORY_GATE
        if gate.allowed
        else OverallRiskResult.INCOMPLETE_NO_POSITIVE_CONCLUSION
    )
    result = RiskAssessmentV3(
        assessment_id=assessment_id,
        company_id=company_id,
        subject_scope=subject_scope,
        status=AssessmentStatus.CALCULATED,
        risk_model_version=policy.risk_model_version,
        ruleset_version=policy.ruleset_version,
        coverage_policy_version=policy.coverage_policy_version,
        applicability_policy_version=policy.applicability_policy_version,
        source_resolution_policy_version=policy.source_resolution_policy_version,
        freshness_policy_version=policy.freshness_policy_version,
        input_hash=input_hash,
        calculated_at=calculated_at,
        evidence_snapshot=snapshot,
        resolved_checks=checks,
        factors=factors,
        coverage_snapshot=coverage,
        mandatory_gate=gate,
        limitations=limitations,
        overall_result=overall,
    )
    LOGGER.info(
        "risk_v3_calculated",
        extra={
            "assessment_id": assessment_id,
            "company_id": company_id,
            "risk_model_version": policy.risk_model_version,
            "ruleset_version": policy.ruleset_version,
            "calculation_duration_ms": round((monotonic() - started) * 1000, 3),
            "applicable_count": coverage.denominator,
            "resolved_count": coverage.numerator,
            "mandatory_unresolved_count": len(gate.blocking_checks),
            "incomplete_count": len(coverage.unresolved_codes),
            "conflict_count": sum(
                item.resolution_state == ResolutionState.CONFLICTING_EVIDENCE
                for item in checks
            ),
        },
    )
    return result


# Explicit name for callers that prefer the architectural term.
build_risk_assessment_v3 = calculate_risk_v3
