"""Strict contracts for deterministic, traceable Summary Engine output."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.contracts.decision import ContractModel
from app.contracts.risk import ChangeOrigin, RiskOverallStatus, RiskSeverity


class SummaryMode(StrEnum):
    PUBLIC = "PUBLIC"
    USER = "USER"
    DUE_DILIGENCE = "DUE_DILIGENCE"
    MONITORING = "MONITORING"
    BULK = "BULK"
    PERSON = "PERSON"


class SummaryStatementKind(StrEnum):
    CONCLUSION = "CONCLUSION"
    FACTOR = "FACTOR"
    CHANGE = "CHANGE"
    LIMITATION = "LIMITATION"
    RECOMMENDATION = "RECOMMENDATION"


class SummaryStatement(ContractModel):
    statement_id: str = Field(min_length=1, max_length=200)
    summary_id: str = Field(min_length=1, max_length=36)
    risk_assessment_id: str = Field(min_length=1, max_length=36)
    kind: SummaryStatementKind
    text: str = Field(min_length=1, max_length=5000)
    signal_ids: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    metric_ids: tuple[str, ...] = ()
    rule_id: str | None = None
    rule_version: str | None = None
    source_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    data_dates: tuple[datetime, ...] = ()
    calculation: str | None = None
    severity: RiskSeverity = RiskSeverity.NONE
    hard_blocker: bool = False
    public_visible: bool = False
    summary_engine_version: str

    @field_validator("data_dates")
    @classmethod
    def require_timezone(cls, values: tuple[datetime, ...]) -> tuple[datetime, ...]:
        if any(value.tzinfo is None or value.utcoffset() is None for value in values):
            raise ValueError("Summary statement timestamps must include a timezone")
        return values

    @model_validator(mode="after")
    def require_traceability(self) -> "SummaryStatement":
        if self.kind != SummaryStatementKind.CONCLUSION and not (
            self.signal_ids or self.evidence_refs
        ):
            raise ValueError("Meaningful summary statements require signal or evidence refs")
        if (self.rule_id is None) != (self.rule_version is None):
            raise ValueError("rule_id and rule_version must be provided together")
        return self


class SummaryTextBlocks(ContractModel):
    short_conclusion: SummaryStatement
    main_factors: tuple[SummaryStatement, ...] = ()
    changes: tuple[SummaryStatement, ...] = ()
    limitations: tuple[SummaryStatement, ...] = ()
    recommendations: tuple[SummaryStatement, ...] = ()


class MonitoringComparison(ContractModel):
    previous_risk_assessment_id: str | None = None
    current_risk_assessment_id: str
    change_origin: ChangeOrigin
    company_change: bool
    methodology_only: bool


class SummaryResult(ContractModel):
    summary_id: str
    company_id: int
    company_inn: str
    risk_assessment_id: str
    mode: SummaryMode
    summary_engine_version: str
    risk_engine_version: str
    ruleset_version: str
    generated_at: datetime
    overall_status: RiskOverallStatus
    overall_label: str = ""
    text_blocks: SummaryTextBlocks
    explainability: tuple[SummaryStatement, ...]
    completeness: dict[str, Any]
    comparison: MonitoringComparison | None = None
    compact_text: str | None = None
    public_projection_approved: bool = False
    reused: bool = False

    @field_validator("generated_at")
    @classmethod
    def require_generated_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_statement_ownership(self) -> "SummaryResult":
        if any(
            item.summary_id != self.summary_id
            or item.risk_assessment_id != self.risk_assessment_id
            or item.summary_engine_version != self.summary_engine_version
            for item in self.explainability
        ):
            raise ValueError("Explainability statements must belong to this summary")
        return self
