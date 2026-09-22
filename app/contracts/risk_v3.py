"""Internal, score-free contracts for normalized Risk v3.

Risk v3 deliberately models evidence quality independently from adverse facts.
The module has no public API dependencies and contains no aggregate score.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.contracts.decision import ContractModel


class SubjectScope(StrEnum):
    LEGAL_ENTITY = "LEGAL_ENTITY"
    INDIVIDUAL_ENTREPRENEUR = "INDIVIDUAL_ENTREPRENEUR"
    UNKNOWN = "UNKNOWN"


class AssessmentStatus(StrEnum):
    CALCULATED = "CALCULATED"
    UNSUPPORTED_SUBJECT = "UNSUPPORTED_SUBJECT"


class Applicability(StrEnum):
    APPLICABLE = "APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    APPLICABILITY_UNKNOWN = "APPLICABILITY_UNKNOWN"


class Observation(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"


class Execution(StrEnum):
    CHECKED = "CHECKED"
    NOT_CHECKED = "NOT_CHECKED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    PARSING_ERROR = "PARSING_ERROR"


class Freshness(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class ResolutionState(StrEnum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


class ScopeCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class SourceClass(StrEnum):
    OFFICIAL_DIRECT = "OFFICIAL_DIRECT"
    OFFICIAL_DOWNLOADED_DATASET = "OFFICIAL_DOWNLOADED_DATASET"
    AUTHORIZED_BRIDGE = "AUTHORIZED_BRIDGE"
    DISCOVERY_ONLY = "DISCOVERY_ONLY"


class TemporalKind(StrEnum):
    CURRENT_STATE = "CURRENT_STATE"
    HISTORICAL_EVENT = "HISTORICAL_EVENT"


class BlockingReason(StrEnum):
    NOT_CHECKED = "NOT_CHECKED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    PARSING_ERROR = "PARSING_ERROR"
    OBSERVATION_UNKNOWN = "OBSERVATION_UNKNOWN"
    APPLICABILITY_UNKNOWN = "APPLICABILITY_UNKNOWN"
    STALE_CURRENT_STATE = "STALE_CURRENT_STATE"
    FRESHNESS_UNKNOWN = "FRESHNESS_UNKNOWN"
    PARTIAL_SCOPE = "PARTIAL_SCOPE"
    SCOPE_UNKNOWN = "SCOPE_UNKNOWN"
    NEGATIVE_CLOSURE_NOT_PROVEN = "NEGATIVE_CLOSURE_NOT_PROVEN"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    EMPTY_MANDATORY_SET = "EMPTY_MANDATORY_SET"


class RiskSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class MaterialityState(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Recency(StrEnum):
    CURRENT = "CURRENT"
    RECENT = "RECENT"
    HISTORICAL = "HISTORICAL"
    UNKNOWN = "UNKNOWN"


class Actionability(StrEnum):
    IMMEDIATE = "IMMEDIATE"
    REVIEW = "REVIEW"
    MONITOR = "MONITOR"
    NONE = "NONE"


class OverallRiskResult(StrEnum):
    HARD_BLOCKER_PRESENT = "HARD_BLOCKER_PRESENT"
    RISK_FACTORS_PRESENT = "RISK_FACTORS_PRESENT"
    NO_ADVERSE_FACTORS_AFTER_MANDATORY_GATE = (
        "NO_ADVERSE_FACTORS_AFTER_MANDATORY_GATE"
    )
    INCOMPLETE_NO_POSITIVE_CONCLUSION = "INCOMPLETE_NO_POSITIVE_CONCLUSION"


class SubjectIdentity(ContractModel):
    company_id: int = Field(gt=0)
    inn: str | None = Field(default=None, min_length=10, max_length=12)
    ogrn: str | None = Field(default=None, min_length=13, max_length=15)


class Limitation(ContractModel):
    limitation_code: str = Field(min_length=1, max_length=120)
    origin_check_ref: str | None = Field(default=None, min_length=1, max_length=200)
    fact_ref: str | None = Field(default=None, min_length=1, max_length=200)
    parameters: dict[str, Any] = Field(default_factory=dict)
    blocks_positive_conclusion: bool
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_origin(self) -> "Limitation":
        if not (self.origin_check_ref or self.fact_ref):
            raise ValueError("A limitation requires a check or fact origin")
        return self


class ApplicabilityDecision(ContractModel):
    """Provenance for a capability-specific applicability rule decision."""

    rule_id: str = Field(min_length=1, max_length=160)
    rule_version: str = Field(min_length=1, max_length=40)
    subject_scope: SubjectScope
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    source_classes: tuple[SourceClass, ...] = Field(min_length=1)
    based_on_data_absence: bool = False
    decided_at: datetime

    @field_validator("evidence_refs", "source_refs")
    @classmethod
    def require_provenance_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("Applicability provenance refs must be non-empty")
        return value

    @field_validator("decided_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Applicability decision timestamps must include a timezone")
        return value


class NormalizedEvidenceCandidate(ContractModel):
    candidate_ref: str = Field(min_length=1, max_length=200)
    capability_code: str = Field(min_length=1, max_length=120)
    fact_identity: str = Field(min_length=1, max_length=200)
    company_id: int = Field(gt=0)
    subject_identity: SubjectIdentity
    source_code: str = Field(min_length=1, max_length=120)
    source_class: SourceClass
    evidence_refs: tuple[str, ...]
    exact_identity_match: bool
    applicability: Applicability
    applicability_decision: ApplicabilityDecision | None = None
    observation: Observation
    execution: Execution
    freshness: Freshness
    scope: ScopeCompleteness
    scope_details: dict[str, Any] = Field(default_factory=dict)
    temporal_kind: TemporalKind
    source_as_of: datetime | None = None
    effective_at: datetime | None = None
    retrieved_at: datetime | None = None
    checked_at: datetime | None = None
    negative_closure_capable: bool = False
    fact_payload: dict[str, Any] = Field(default_factory=dict)
    limitations: tuple[Limitation, ...] = ()

    @field_validator("source_as_of", "effective_at", "retrieved_at", "checked_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Evidence timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_axes(self) -> "NormalizedEvidenceCandidate":
        if self.subject_identity.company_id != self.company_id:
            raise ValueError("Evidence subject and company_id must match")
        if self.observation in {Observation.FOUND, Observation.NOT_FOUND}:
            if self.applicability != Applicability.APPLICABLE:
                raise ValueError("Observed facts require APPLICABLE")
            if self.execution != Execution.CHECKED:
                raise ValueError("FOUND/NOT_FOUND require CHECKED execution")
            if not self.evidence_refs:
                raise ValueError("FOUND/NOT_FOUND require evidence refs")
        if self.observation == Observation.NOT_FOUND and not self.negative_closure_capable:
            raise ValueError("NOT_FOUND requires proven negative-closure capability")
        if self.execution in {
            Execution.NOT_CHECKED,
            Execution.SOURCE_UNAVAILABLE,
            Execution.TIMEOUT,
            Execution.PARSING_ERROR,
        } and self.observation != Observation.UNKNOWN:
            raise ValueError("Incomplete execution cannot produce FOUND or NOT_FOUND")
        if self.applicability == Applicability.NOT_APPLICABLE:
            if self.observation != Observation.UNKNOWN:
                raise ValueError("NOT_APPLICABLE is not an observation")
            if self.execution != Execution.CHECKED:
                raise ValueError("NOT_APPLICABLE is an applicability result, not failure")
        if self.applicability == Applicability.APPLICABILITY_UNKNOWN:
            if self.observation != Observation.UNKNOWN:
                raise ValueError("Unknown applicability cannot produce an observation")
        if self.observation != Observation.FOUND and self.fact_payload:
            raise ValueError("Only FOUND evidence may carry a fact payload")
        return self


class SubstitutionMetadata(ContractModel):
    substituted: bool
    source_code: str | None = None
    source_class: SourceClass | None = None
    policy_basis: str | None = None

    @model_validator(mode="after")
    def require_substitution_provenance(self) -> "SubstitutionMetadata":
        if self.substituted and not (
            self.source_code and self.source_class and self.policy_basis
        ):
            raise ValueError("Bridge substitution requires structured provenance")
        return self


class ResolvedCheckResult(ContractModel):
    check_ref: str = Field(min_length=1, max_length=200)
    capability_code: str = Field(min_length=1, max_length=120)
    fact_identity: str = Field(min_length=1, max_length=200)
    company_id: int = Field(gt=0)
    applicability: Applicability
    applicability_decision: ApplicabilityDecision | None = None
    observation: Observation
    execution: Execution
    freshness: Freshness
    scope: ScopeCompleteness
    scope_details: dict[str, Any] = Field(default_factory=dict)
    temporal_kind: TemporalKind
    resolution_state: ResolutionState
    selected_evidence_refs: tuple[str, ...] = ()
    candidate_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    substitution: SubstitutionMetadata = Field(
        default_factory=lambda: SubstitutionMetadata(substituted=False)
    )
    conflict_refs: tuple[str, ...] = ()
    negative_closure_proven: bool = False
    fact_payload: dict[str, Any] = Field(default_factory=dict)
    source_as_of: datetime | None = None
    effective_at: datetime | None = None
    checked_at: datetime | None = None
    limitations: tuple[Limitation, ...] = ()
    resolution_policy_version: str = Field(min_length=1, max_length=80)

    @field_validator("source_as_of", "effective_at", "checked_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Resolved-check timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_resolution(self) -> "ResolvedCheckResult":
        if self.resolution_state == ResolutionState.CONFLICTING_EVIDENCE:
            if self.observation != Observation.UNKNOWN or len(self.conflict_refs) < 2:
                raise ValueError("Conflicts require UNKNOWN and competing evidence refs")
        elif self.conflict_refs:
            raise ValueError("conflict_refs are only valid for conflicting evidence")

        if self.resolution_state == ResolutionState.RESOLVED:
            if self.applicability == Applicability.APPLICABILITY_UNKNOWN:
                raise ValueError("Unknown applicability cannot be resolved")
            if (
                self.applicability == Applicability.NOT_APPLICABLE
                and self.applicability_decision is None
            ):
                raise ValueError(
                    "Resolved NOT_APPLICABLE requires a validated applicability decision"
                )
            if self.applicability == Applicability.APPLICABLE:
                if self.execution != Execution.CHECKED:
                    raise ValueError("Resolved applicable checks require CHECKED")
                if self.observation not in {Observation.FOUND, Observation.NOT_FOUND}:
                    raise ValueError("Resolved applicable checks require an observation")
                if not self.selected_evidence_refs:
                    raise ValueError("Resolved observations require selected evidence refs")
                if self.scope != ScopeCompleteness.COMPLETE:
                    raise ValueError("Partial scope cannot be resolved")
                if (
                    self.temporal_kind == TemporalKind.CURRENT_STATE
                    and self.freshness != Freshness.CURRENT
                ):
                    raise ValueError("Current-state checks require current evidence")
                if (
                    self.observation == Observation.NOT_FOUND
                    and not self.negative_closure_proven
                ):
                    raise ValueError("Resolved NOT_FOUND requires negative closure")
        if self.observation == Observation.NOT_FOUND and not self.negative_closure_proven:
            raise ValueError("NOT_FOUND cannot survive without negative closure")
        if self.observation != Observation.FOUND and self.fact_payload:
            raise ValueError("Only FOUND checks may carry a fact payload")
        return self


class CoverageSnapshot(ContractModel):
    applicable_codes: tuple[str, ...]
    resolved_codes: tuple[str, ...]
    unresolved_codes: tuple[str, ...]
    partial_codes: tuple[str, ...]
    applicability_unknown_codes: tuple[str, ...]
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    percentage: Decimal = Field(ge=0, le=100)
    mandatory_applicable_codes: tuple[str, ...]
    mandatory_resolved_codes: tuple[str, ...]
    mandatory_numerator: int = Field(ge=0)
    mandatory_denominator: int = Field(ge=0)
    calculated_at: datetime
    coverage_policy_version: str = Field(min_length=1, max_length=80)

    @field_validator("calculated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("calculated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> "CoverageSnapshot":
        code_fields = (
            self.applicable_codes,
            self.resolved_codes,
            self.unresolved_codes,
            self.partial_codes,
            self.applicability_unknown_codes,
            self.mandatory_applicable_codes,
            self.mandatory_resolved_codes,
        )
        if any(tuple(sorted(set(codes))) != codes for codes in code_fields):
            raise ValueError("Coverage code lists must be unique and sorted")
        applicable = set(self.applicable_codes)
        resolved = set(self.resolved_codes)
        unresolved = set(self.unresolved_codes)
        if not resolved <= applicable or not unresolved <= applicable:
            raise ValueError("Resolved/unresolved codes must be applicable")
        if resolved & unresolved or resolved | unresolved != applicable:
            raise ValueError("Applicable checks must partition into resolved/unresolved")
        if not set(self.partial_codes) <= unresolved:
            raise ValueError("Partial checks must be unresolved")
        if self.numerator != len(resolved) or self.denominator != len(applicable):
            raise ValueError("Coverage counts must match persisted code lists")
        expected = (
            Decimal("0.00")
            if self.denominator == 0
            else (Decimal(self.numerator) * 100 / Decimal(self.denominator)).quantize(
                Decimal("0.01")
            )
        )
        if self.percentage != expected:
            raise ValueError("Coverage percentage must be derived from X/Y")
        if self.mandatory_numerator != len(self.mandatory_resolved_codes):
            raise ValueError("Mandatory numerator must match code list")
        if self.mandatory_denominator != len(self.mandatory_applicable_codes):
            raise ValueError("Mandatory denominator must match code list")
        if not set(self.mandatory_resolved_codes) <= set(
            self.mandatory_applicable_codes
        ):
            raise ValueError("Mandatory resolved codes must be mandatory applicable")
        return self


class BlockingCheck(ContractModel):
    capability_code: str = Field(min_length=1, max_length=120)
    reasons: tuple[BlockingReason, ...] = Field(min_length=1)
    check_ref: str = Field(min_length=1, max_length=200)


class MandatoryGateResult(ContractModel):
    allowed: bool
    policy_version: str = Field(min_length=1, max_length=80)
    mandatory_applicable_codes: tuple[str, ...]
    resolved_codes: tuple[str, ...]
    blocking_checks: tuple[BlockingCheck, ...]
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_gate(self) -> "MandatoryGateResult":
        if tuple(sorted(set(self.mandatory_applicable_codes))) != self.mandatory_applicable_codes:
            raise ValueError("Mandatory applicable codes must be unique and sorted")
        if tuple(sorted(set(self.resolved_codes))) != self.resolved_codes:
            raise ValueError("Resolved codes must be unique and sorted")
        empty_guard = any(
            BlockingReason.EMPTY_MANDATORY_SET in item.reasons
            for item in self.blocking_checks
        )
        if not self.mandatory_applicable_codes and not empty_guard:
            raise ValueError("An empty mandatory set must fail closed")
        complete = bool(self.mandatory_applicable_codes) and (
            set(self.resolved_codes) == set(self.mandatory_applicable_codes)
        )
        if self.allowed != (complete and not self.blocking_checks):
            raise ValueError("allowed must reflect one canonical mandatory gate")
        return self


class BusinessMateriality(ContractModel):
    state: MaterialityState
    numerator: Decimal | None = None
    denominator: Decimal | None = None
    ratio: Decimal | None = None
    unit: str | None = Field(default=None, max_length=40)
    calculation: str | None = Field(default=None, max_length=500)
    period: str | None = Field(default=None, max_length=80)
    rule_basis: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_known_materiality(self) -> "BusinessMateriality":
        numeric = (self.numerator, self.denominator, self.ratio)
        if self.state == MaterialityState.NOT_APPLICABLE and any(
            value is not None for value in numeric
        ):
            raise ValueError("NOT_APPLICABLE materiality cannot carry calculations")
        if self.state == MaterialityState.KNOWN and not (
            self.calculation and self.rule_basis
        ):
            raise ValueError("Known materiality requires calculation and rule basis")
        return self


class RiskFactor(ContractModel):
    factor_ref: str = Field(min_length=1, max_length=200)
    factor_code: str = Field(min_length=1, max_length=120)
    underlying_check_ref: str | None = Field(default=None, min_length=1, max_length=200)
    fact_ref: str | None = Field(default=None, min_length=1, max_length=200)
    severity: RiskSeverity
    hard_blocker: bool
    business_materiality: BusinessMateriality
    recency: Recency
    actionability: Actionability
    rule_id: str = Field(min_length=1, max_length=160)
    rule_version: str = Field(min_length=1, max_length=40)
    recommendation_code: str = Field(min_length=1, max_length=120)
    wording_key: str = Field(min_length=1, max_length=160)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    source_as_of: datetime | None = None
    effective_at: datetime | None = None
    status_semantics: str = Field(min_length=1, max_length=500)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_as_of", "effective_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Risk-factor timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def require_fact_origin(self) -> "RiskFactor":
        if not (self.underlying_check_ref or self.fact_ref):
            raise ValueError("Risk factors require a check or fact origin")
        return self


class RiskAssessmentV3(ContractModel):
    assessment_id: str = Field(min_length=1, max_length=36)
    company_id: int = Field(gt=0)
    subject_scope: SubjectScope
    status: AssessmentStatus = AssessmentStatus.CALCULATED
    risk_model_version: str = Field(min_length=1, max_length=40)
    ruleset_version: str = Field(min_length=1, max_length=80)
    coverage_policy_version: str = Field(min_length=1, max_length=80)
    applicability_policy_version: str = Field(min_length=1, max_length=80)
    source_resolution_policy_version: str = Field(min_length=1, max_length=80)
    freshness_policy_version: str = Field(min_length=1, max_length=80)
    input_hash: str = Field(min_length=64, max_length=64)
    calculated_at: datetime
    evidence_snapshot: tuple[NormalizedEvidenceCandidate, ...]
    resolved_checks: tuple[ResolvedCheckResult, ...]
    factors: tuple[RiskFactor, ...]
    coverage_snapshot: CoverageSnapshot
    mandatory_gate: MandatoryGateResult
    limitations: tuple[Limitation, ...]
    overall_result: OverallRiskResult

    @field_validator("calculated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("calculated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> "RiskAssessmentV3":
        if self.subject_scope != SubjectScope.LEGAL_ENTITY:
            raise ValueError("RiskAssessmentV3 currently supports legal entities only")
        expected = (
            OverallRiskResult.HARD_BLOCKER_PRESENT
            if any(item.hard_blocker for item in self.factors)
            else OverallRiskResult.RISK_FACTORS_PRESENT
            if self.factors
            else OverallRiskResult.NO_ADVERSE_FACTORS_AFTER_MANDATORY_GATE
            if self.mandatory_gate.allowed
            else OverallRiskResult.INCOMPLETE_NO_POSITIVE_CONCLUSION
        )
        if self.overall_result != expected:
            raise ValueError("Overall result must follow the categorical risk policy")
        return self


class UnsupportedSubjectOutcome(ContractModel):
    status: AssessmentStatus = AssessmentStatus.UNSUPPORTED_SUBJECT
    company_id: int = Field(gt=0)
    subject_scope: SubjectScope
    reason_code: str = "SUBJECT_SCOPE_NOT_SUPPORTED"
    applicability_policy_version: str = Field(min_length=1, max_length=80)
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluated_at must include a timezone")
        return value
