"""Versioned, score-free semantic contracts for a future Risk v4 migration.

These contracts deliberately do not contain a numeric NEXT Index.  They model
the traceable chain between resolved evidence and client-facing meaning while
Risk v3 remains the production decision engine.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.contracts.decision import ContractModel


SEMANTIC_CONTRACT_VERSION = "semantic-v4-foundation.1"
PUBLIC_NEXT_INDEX_ENABLED = False


class SemanticCategory(StrEnum):
    REGISTRATION = "REGISTRATION"
    BANKRUPTCY = "BANKRUPTCY"
    ENFORCEMENT = "ENFORCEMENT"
    TAX = "TAX"
    FINANCE = "FINANCE"
    COURTS = "COURTS"
    REGULATORY = "REGULATORY"
    MANAGEMENT = "MANAGEMENT"
    LICENCE = "LICENCE"
    COVERAGE = "COVERAGE"
    OTHER = "OTHER"


class SemanticSeverity(StrEnum):
    INFORMATION = "INFORMATION"
    ATTENTION = "ATTENTION"
    SIGNIFICANT = "SIGNIFICANT"
    CRITICAL = "CRITICAL"


class SemanticState(StrEnum):
    CURRENT = "CURRENT"
    HISTORICAL = "HISTORICAL"
    UNKNOWN = "UNKNOWN"


class SourceMetadata(ContractModel):
    source_ref: str = Field(min_length=1, max_length=200)
    source_family: str = Field(min_length=1, max_length=120)
    capability: str = Field(min_length=1, max_length=120)
    risk_role: str = Field(min_length=1, max_length=120)
    positive_role: str = Field(min_length=1, max_length=120)
    coverage_role: str = Field(min_length=1, max_length=120)
    applicability: str = Field(min_length=1, max_length=120)
    freshness: str = Field(min_length=1, max_length=120)
    precedence: int = Field(ge=0)
    publicability: str = Field(min_length=1, max_length=120)
    impact_priority: int = Field(ge=0)


class ResolvedCheck(ContractModel):
    check_ref: str = Field(min_length=1, max_length=200)
    capability: str = Field(min_length=1, max_length=120)
    fact_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = Field(min_length=1)
    source_dates: tuple[datetime, ...] = ()
    applicable: bool | None
    completed: bool
    negative_closure_proven: bool = False
    current_state: str = Field(min_length=1, max_length=120)
    ruleset_version: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_negative_closure(self) -> "ResolvedCheck":
        if self.negative_closure_proven and not self.completed:
            raise ValueError("negative closure requires a completed check")
        return self


class Metric(ContractModel):
    metric_ref: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=160)
    fact_refs: tuple[str, ...] = Field(min_length=1)
    value: Decimal
    unit: str = Field(min_length=1, max_length=40)
    denominator: Decimal | None = None
    measured_at: datetime | None = None


class ChangeEvent(ContractModel):
    event_ref: str = Field(min_length=1, max_length=200)
    fact_refs: tuple[str, ...] = Field(min_length=1)
    previous_state: str | None = Field(default=None, max_length=240)
    current_state: str = Field(min_length=1, max_length=240)
    change: str = Field(min_length=1, max_length=500)
    effective_at: datetime
    detected_at: datetime


class Factor(ContractModel):
    factor_ref: str = Field(min_length=1, max_length=200)
    factor_code: str = Field(min_length=1, max_length=120)
    category: SemanticCategory
    severity: SemanticSeverity
    fact_refs: tuple[str, ...] = Field(min_length=1)
    metric_refs: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = Field(min_length=1)
    materiality: str | None = Field(default=None, max_length=500)
    confidence: Decimal = Field(ge=0, le=1)
    ruleset_version: str = Field(min_length=1, max_length=120)


class Meaning(ContractModel):
    meaning_id: str = Field(min_length=1, max_length=200)
    category: SemanticCategory
    severity: SemanticSeverity
    factor_refs: tuple[str, ...] = ()
    fact_refs: tuple[str, ...] = ()
    metric_refs: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    current_state: SemanticState
    previous_state: str | None = Field(default=None, max_length=240)
    change: str | None = Field(default=None, max_length=500)
    trend: str | None = Field(default=None, max_length=160)
    frequency: str | None = Field(default=None, max_length=160)
    recency: str | None = Field(default=None, max_length=160)
    duration: str | None = Field(default=None, max_length=160)
    materiality: str | None = Field(default=None, max_length=500)
    counter_evidence: tuple[str, ...] = ()
    confidence: Decimal = Field(ge=0, le=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    source_dates: tuple[datetime, ...] = ()
    client_headline: str = Field(min_length=1, max_length=500)
    client_short_explanation: str = Field(min_length=1, max_length=1200)
    client_full_explanation: str = Field(min_length=1, max_length=3000)
    client_meaning: str = Field(min_length=1, max_length=2000)
    what_it_does_not_mean: str = Field(min_length=1, max_length=1600)
    recommendation_effect: str | None = Field(default=None, max_length=1600)
    ruleset_version: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def require_traceability(self) -> "Meaning":
        if not (self.factor_refs or self.fact_refs or self.metric_refs or self.event_refs):
            raise ValueError("a meaning requires a factor, fact, metric, or event origin")
        return self


class Coverage(ContractModel):
    coverage_ref: str = Field(min_length=1, max_length=200)
    completed_check_refs: tuple[str, ...]
    unresolved_check_refs: tuple[str, ...]
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    completeness: Decimal = Field(ge=0, le=1)
    limitations: tuple[str, ...] = ()
    calculated_at: datetime
    ruleset_version: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_counts(self) -> "Coverage":
        if self.numerator > self.denominator:
            raise ValueError("coverage numerator cannot exceed denominator")
        expected = Decimal(0) if self.denominator == 0 else Decimal(self.numerator) / Decimal(self.denominator)
        if self.completeness != expected:
            raise ValueError("coverage completeness must match numerator/denominator")
        return self


class Explanation(ContractModel):
    explanation_ref: str = Field(min_length=1, max_length=200)
    meaning_id: str = Field(min_length=1, max_length=200)
    headline: str = Field(min_length=1, max_length=500)
    short_explanation: str = Field(min_length=1, max_length=1200)
    full_explanation: str = Field(min_length=1, max_length=3000)
    effect_on_conclusion: str = Field(min_length=1, max_length=1600)
    what_remains_unknown: str | None = Field(default=None, max_length=1600)


class Recommendation(ContractModel):
    recommendation_ref: str = Field(min_length=1, max_length=200)
    meaning_id: str = Field(min_length=1, max_length=200)
    recommendation_code: str = Field(min_length=1, max_length=120)
    action: str = Field(min_length=1, max_length=1600)
    rationale: str = Field(min_length=1, max_length=1600)
    effect: str = Field(min_length=1, max_length=1600)
    supported_by_refs: tuple[str, ...] = Field(min_length=1)
    ruleset_version: str = Field(min_length=1, max_length=120)


class SemanticEnvelope(ContractModel):
    contract_version: str = Field(default=SEMANTIC_CONTRACT_VERSION)
    company_id: int = Field(gt=0)
    resolved_checks: tuple[ResolvedCheck, ...]
    metrics: tuple[Metric, ...] = ()
    events: tuple[ChangeEvent, ...] = ()
    factors: tuple[Factor, ...] = ()
    meanings: tuple[Meaning, ...] = ()
    coverage: Coverage
    explanations: tuple[Explanation, ...] = ()
    recommendations: tuple[Recommendation, ...] = ()
    internal_extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "resolved_checks", "metrics", "events", "factors", "meanings",
        "explanations", "recommendations",
    )
    @classmethod
    def require_unique_objects(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        identities = [
            next(
                str(getattr(item, field))
                for field in (
                    "check_ref", "metric_ref", "event_ref", "factor_ref",
                    "meaning_id", "explanation_ref", "recommendation_ref",
                )
                if hasattr(item, field)
            )
            for item in value
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("semantic object identities must be unique")
        return value
