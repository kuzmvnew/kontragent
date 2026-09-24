"""Strict contracts for the bounded S02 tax-debt vertical slice.

These contracts are internal/card-ready models.  They do not change the HTTP
API and deliberately keep operational RAW references out of the public
projection.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from app.contracts.decision import ContractModel
from app.contracts.risk_v3 import (
    Freshness,
    Observation,
    OverallRiskResult,
    ResolutionState,
    RiskSeverity,
)
from app.sources.fns_tax_debt import (
    DATASET_CODE,
    FACT_CODE,
    OFFICIAL_SOURCE_PAGE,
    SOURCE_ID,
)


class S02FactState(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    STALE_DATA = "STALE_DATA"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class S02SourceAttribution(ContractModel):
    source_id: str = SOURCE_ID
    source_code: str = DATASET_CODE
    official_source: str = OFFICIAL_SOURCE_PAGE
    source_reference: str | None = None

    @model_validator(mode="after")
    def require_s02(self) -> "S02SourceAttribution":
        if self.source_id != SOURCE_ID or self.source_code != DATASET_CODE:
            raise ValueError("S02 fact must retain the canonical source identity")
        return self


class S02TaxDebtFreshness(ContractModel):
    status: Freshness
    data_as_of: date | None = None
    source_as_of: datetime | None = None
    retrieved_at: datetime | None = None
    checked_at: datetime
    official_actual_until: date | None = None
    reason: str | None = Field(default=None, max_length=160)

    @field_validator("source_as_of", "retrieved_at", "checked_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("S02 freshness timestamps must include a timezone")
        return value


class S02TaxDebtProvenance(ContractModel):
    source_id: str = SOURCE_ID
    dataset_code: str = DATASET_CODE
    official_source: str = OFFICIAL_SOURCE_PAGE
    artifact_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    artifact_reference: str | None = None
    worker_run_id: str | None = Field(default=None, max_length=100)
    source_document_id: str | None = Field(default=None, max_length=255)
    source_member: str | None = Field(default=None, max_length=500)
    record_hash: str | None = Field(default=None, min_length=64, max_length=64)
    source_as_of: datetime | None = None
    retrieved_at: datetime | None = None
    parser_version: str | None = Field(default=None, max_length=80)
    normalization_version: str | None = Field(default=None, max_length=80)
    matching_method: str | None = Field(default=None, max_length=80)

    @field_validator("source_as_of", "retrieved_at", mode="before")
    @classmethod
    def parse_timestamp(cls, value):
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @field_validator("source_as_of", "retrieved_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("S02 provenance timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def require_s02(self) -> "S02TaxDebtProvenance":
        if self.source_id != SOURCE_ID or self.dataset_code != DATASET_CODE:
            raise ValueError("S02 provenance must retain source and dataset identity")
        return self


class S02TaxDebtFact(ContractModel):
    fact_ref: str = Field(min_length=1, max_length=200)
    fact_code: str = FACT_CODE
    company_id: int = Field(gt=0)
    state: S02FactState
    amount: Decimal | None = Field(default=None, ge=0)
    amount_as_of_date: date | None = None
    total_arrears: Decimal | None = Field(default=None, ge=0)
    total_penalties: Decimal | None = Field(default=None, ge=0)
    total_fines: Decimal | None = Field(default=None, ge=0)
    has_debt: bool | None = None
    source: S02SourceAttribution
    freshness: S02TaxDebtFreshness
    provenance: S02TaxDebtProvenance
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = ()

    @field_validator("evidence_refs", "limitations")
    @classmethod
    def unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("S02 references and limitations must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("S02 references and limitations must be unique")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> "S02TaxDebtFact":
        if self.fact_code != FACT_CODE:
            raise ValueError("Unexpected S02 fact code")
        if self.state == S02FactState.FOUND:
            if self.amount is None or self.amount_as_of_date is None:
                raise ValueError("FOUND requires amount and amount_as_of_date")
            if self.freshness.status != Freshness.CURRENT:
                raise ValueError("FOUND requires current evidence")
            if self.has_debt != (self.amount > 0):
                raise ValueError("has_debt must be derived from amount")
            parts = (self.total_arrears, self.total_penalties, self.total_fines)
            if any(value is None for value in parts):
                raise ValueError("FOUND requires the complete amount breakdown")
            if sum(parts, Decimal("0")) != self.amount:
                raise ValueError("S02 amount must equal arrears + penalties + fines")
        elif self.state == S02FactState.NOT_FOUND:
            if self.freshness.status != Freshness.CURRENT:
                raise ValueError("NOT_FOUND requires current evidence")
            if self.amount is not None or self.has_debt is not False:
                raise ValueError("NOT_FOUND cannot carry an amount or debt signal")
            if self.amount_as_of_date is None:
                raise ValueError("NOT_FOUND requires the checked snapshot date")
        else:
            if self.amount is not None or self.has_debt is not None:
                raise ValueError("Unavailable or stale S02 data cannot carry debt")
            if not self.limitations:
                raise ValueError("Unavailable or stale S02 data requires a limitation")
            expected = (
                Freshness.STALE
                if self.state == S02FactState.STALE_DATA
                else Freshness.UNKNOWN
            )
            if self.freshness.status != expected:
                raise ValueError("S02 state and freshness status disagree")
        if (
            self.amount_as_of_date is not None
            and self.freshness.data_as_of is not None
            and self.amount_as_of_date != self.freshness.data_as_of
        ):
            raise ValueError("Fact date must equal the S02 freshness data date")
        return self


class S02PublicEvidence(ContractModel):
    fact_ref: str = Field(min_length=1, max_length=200)
    fact_code: str = FACT_CODE
    state: S02FactState
    amount: Decimal | None = Field(default=None, ge=0)
    amount_as_of_date: date | None = None
    total_arrears: Decimal | None = Field(default=None, ge=0)
    total_penalties: Decimal | None = Field(default=None, ge=0)
    total_fines: Decimal | None = Field(default=None, ge=0)
    has_debt: bool | None = None
    source_id: str = SOURCE_ID
    source_code: str = DATASET_CODE
    official_source: str = OFFICIAL_SOURCE_PAGE
    source_document_id: str | None = None
    matching_method: str | None = None


class S02PublicCoverage(ContractModel):
    capability_code: str = "tax_debt"
    resolution_state: ResolutionState
    observation: Observation
    freshness: Freshness
    negative_closure_proven: bool
    risk_resolved_checks: int = Field(ge=0)
    risk_applicable_checks: int = Field(ge=0)
    risk_coverage_percentage: Decimal = Field(ge=0, le=100)
    mandatory_resolved_checks: int = Field(ge=0)
    mandatory_applicable_checks: int = Field(ge=0)


class S02PublicRiskFactor(ContractModel):
    factor_ref: str = Field(min_length=1, max_length=200)
    factor_code: str = Field(min_length=1, max_length=120)
    severity: RiskSeverity
    recommendation_code: str = Field(min_length=1, max_length=120)
    parameters: dict = Field(default_factory=dict)


class S02PublicRisk(ContractModel):
    assessment_id: str = Field(min_length=1, max_length=36)
    overall_result: OverallRiskResult
    factors: tuple[S02PublicRiskFactor, ...]
    risk_model_version: str = Field(min_length=1, max_length=40)
    ruleset_version: str = Field(min_length=1, max_length=40)


class S02PublicSummary(ContractModel):
    summary_id: str = Field(min_length=1, max_length=36)
    conclusion: OverallRiskResult
    wording_key: str = Field(min_length=1, max_length=160)
    key_reason_refs: tuple[str, ...]
    confirmed_fact_codes: tuple[str, ...]
    recommendation_codes: tuple[str, ...]
    summary_model_version: str = Field(min_length=1, max_length=40)


class S02CardData(ContractModel):
    company_id: int = Field(gt=0)
    summary: S02PublicSummary
    risk: S02PublicRisk
    evidence: S02PublicEvidence
    coverage: S02PublicCoverage
    freshness: S02TaxDebtFreshness
    limitations: tuple[str, ...]
