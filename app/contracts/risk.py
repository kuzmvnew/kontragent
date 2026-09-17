"""Strict public contracts for the explainable Risk Engine."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.contracts.decision import ContractModel


class RiskProfile(StrEnum):
    GENERAL_LE = "GENERAL_LE"
    IP = "IP"
    NEW_COMPANY = "NEW_COMPANY"
    FINANCIAL_ORG = "FINANCIAL_ORG"
    NON_PROFIT = "NON_PROFIT"


class RiskCategory(StrEnum):
    REGISTRATION = "registration"
    OWNERSHIP_MANAGEMENT = "ownership_management"
    FINANCE = "finance"
    TAXES = "taxes"
    ENFORCEMENT = "enforcement"
    BANKRUPTCY = "bankruptcy"
    LITIGATION = "litigation"
    PROCUREMENT = "procurement"
    LICENCES_REGULATORY = "licences_regulatory"
    COMPLIANCE = "compliance"


class RiskSignalStatus(StrEnum):
    CONFIRMED_RISK = "CONFIRMED_RISK"
    WARNING = "WARNING"
    INFO = "INFO"
    NO_RISK_FOUND = "NO_RISK_FOUND"
    NOT_CHECKED = "NOT_CHECKED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    STALE = "STALE"
    PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
    DATA_QUALITY_REVIEW_REQUIRED = "DATA_QUALITY_REVIEW_REQUIRED"


class RiskSeverity(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RiskConfidence(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ApplicabilityStatus(StrEnum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN_UNAVAILABLE = "unknown_unavailable"


class RiskOverallStatus(StrEnum):
    NO_MATERIAL_RISKS = "NO_MATERIAL_RISKS"
    HIGH = "HIGH"
    NO_MATERIAL_SIGNALS = "NO_MATERIAL_SIGNALS"
    ATTENTION = "ATTENTION"
    CRITICAL = "CRITICAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ChangeOrigin(StrEnum):
    SOURCE_CHANGE = "SOURCE_CHANGE"
    RULESET_CHANGE = "RULESET_CHANGE"
    COVERAGE_CHANGE = "COVERAGE_CHANGE"
    DEAL_CONTEXT_CHANGE = "DEAL_CONTEXT_CHANGE"


class RiskRule(ContractModel):
    rule_id: str = Field(min_length=1, max_length=160)
    rule_version: str = Field(min_length=1, max_length=40)
    section_code: RiskCategory
    profile: tuple[RiskProfile, ...]
    applicability_conditions: dict[str, Any]
    required_facts: tuple[str, ...]
    calculation: str | None = None
    thresholds: dict[str, Any]
    severity: RiskSeverity
    hard_blocker: bool
    freshness_policy: dict[str, Any]
    materiality_policy: dict[str, Any]
    deal_context_policy: dict[str, Any]
    explanation_template: str
    recommendation: str
    public_visibility: bool
    valid_from: str
    valid_to: str | None = None


class RiskSignal(ContractModel):
    signal_code: str = Field(min_length=1, max_length=200)
    category: RiskCategory
    status: RiskSignalStatus
    severity: RiskSeverity
    confidence: RiskConfidence
    title: str = Field(min_length=1, max_length=500)
    explanation: str = Field(min_length=1, max_length=4000)
    source_code: str = Field(min_length=1, max_length=100)
    dataset_code: str = Field(min_length=1, max_length=100)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    observed_value: Any = None
    period: dict[str, Any] | None = None
    threshold: dict[str, Any] | None = None
    calculation: str | None = None
    source_as_of: datetime | None = None
    checked_at: datetime | None = None
    calculated_at: datetime
    coverage: str
    freshness: str
    rule_code: str = Field(min_length=1, max_length=160)
    rule_version: str = Field(min_length=1, max_length=40)

    @field_validator("source_as_of", "checked_at", "calculated_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Risk Engine timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_state_semantics(self) -> "RiskSignal":
        if self.status == RiskSignalStatus.NO_RISK_FOUND and self.coverage not in {
            "complete", "targeted_complete", "known_dataset"
        }:
            raise ValueError("NO_RISK_FOUND requires a successful check with known coverage")
        if self.status == RiskSignalStatus.NOT_APPLICABLE and self.severity != RiskSeverity.NONE:
            raise ValueError("NOT_APPLICABLE cannot carry risk severity")
        return self


class RiskSectionAssessment(ContractModel):
    section_code: RiskCategory
    applicability: ApplicabilityStatus
    status: RiskSignalStatus
    severity: RiskSeverity
    confidence: RiskConfidence
    headline: str
    explanation: str
    signal_codes: tuple[str, ...]
    completed_checks: int = Field(ge=0)
    unavailable_checks: int = Field(ge=0)
    not_checked_checks: int = Field(ge=0)
    stale_checks: int = Field(ge=0)
    partial_checks: int = Field(ge=0)


class RiskCompleteness(ContractModel):
    total_applicable_checks: int = Field(ge=0)
    completed: int = Field(ge=0)
    not_applicable: int = Field(ge=0)
    not_checked: int = Field(ge=0)
    unavailable: int = Field(ge=0)
    stale: int = Field(ge=0)
    partial: int = Field(ge=0)
    data_quality_review: int = Field(default=0, ge=0)
    core: dict[str, int]
    context: dict[str, int]


class RiskAssessmentResult(ContractModel):
    assessment_id: str
    company_id: int
    company_inn: str
    profile: RiskProfile
    risk_engine_version: str
    ruleset_version: str
    ruleset_hash: str
    calculated_at: datetime
    input_snapshot_refs: tuple[str, ...]
    signals: tuple[RiskSignal, ...]
    section_assessments: tuple[RiskSectionAssessment, ...]
    coverage: dict[str, Any]
    completeness: RiskCompleteness
    overall_status: RiskOverallStatus
    limitations: tuple[str, ...]
    change_origin: ChangeOrigin
    reused: bool = False
