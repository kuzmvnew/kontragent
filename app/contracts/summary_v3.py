"""Pure, structured projection contracts for persisted Risk v3 results."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.contracts.decision import ContractModel
from app.contracts.risk_v3 import Limitation, OverallRiskResult


class RecommendationOriginType(StrEnum):
    FACTOR = "FACTOR"
    LIMITATION = "LIMITATION"


class CompletedCheckWithoutAdverseFinding(ContractModel):
    check_ref: str = Field(min_length=1, max_length=200)
    capability_code: str = Field(min_length=1, max_length=120)
    evidence_refs: tuple[str, ...] = Field(min_length=1)


class ConfirmedPositiveFact(ContractModel):
    fact_code: str = Field(min_length=1, max_length=120)
    origin_check_ref: str = Field(min_length=1, max_length=200)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


class Recommendation(ContractModel):
    recommendation_code: str = Field(min_length=1, max_length=120)
    origin_type: RecommendationOriginType
    origin_ref: str = Field(min_length=1, max_length=200)
    action_class: str = Field(min_length=1, max_length=80)
    parameters: dict[str, Any] = Field(default_factory=dict)


class SummaryConclusion(ContractModel):
    result: OverallRiskResult
    wording_key: str = Field(min_length=1, max_length=160)
    positive_conclusion_allowed: bool


class TraceabilityEntry(ContractModel):
    output_ref: str = Field(min_length=1, max_length=200)
    check_refs: tuple[str, ...] = ()
    factor_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_input_reference(self) -> "TraceabilityEntry":
        if not (self.check_refs or self.factor_refs or self.evidence_refs):
            raise ValueError("Summary traceability requires an input reference")
        return self


class SummaryV3(ContractModel):
    summary_id: str = Field(min_length=1, max_length=36)
    company_id: int = Field(gt=0)
    risk_assessment_id: str = Field(min_length=1, max_length=36)
    summary_model_version: str = Field(min_length=1, max_length=40)
    projection_policy_version: str = Field(min_length=1, max_length=80)
    generated_at: datetime
    overall_conclusion: SummaryConclusion
    key_reason_refs: tuple[str, ...]
    completed_checks_without_adverse_finding: tuple[
        CompletedCheckWithoutAdverseFinding, ...
    ]
    confirmed_positive_facts: tuple[ConfirmedPositiveFact, ...]
    limitations: tuple[Limitation, ...]
    recommendations: tuple[Recommendation, ...]
    traceability: tuple[TraceabilityEntry, ...]

    @field_validator("generated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_projection(self) -> "SummaryV3":
        if self.overall_conclusion.positive_conclusion_allowed != (
            self.overall_conclusion.result
            == OverallRiskResult.NO_ADVERSE_FACTORS_AFTER_MANDATORY_GATE
        ):
            raise ValueError("Summary cannot implement a second positive gate")
        if len(self.key_reason_refs) != len(set(self.key_reason_refs)):
            raise ValueError("Key reason refs must be unique")
        return self
