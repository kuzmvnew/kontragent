"""Contracts for the on-demand company check orchestrator."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from app.contracts.decision import ContractModel
from app.contracts.risk import RiskAssessmentResult
from app.contracts.summary import SummaryResult
from app.contracts.risk_v3 import RiskAssessmentV3
from app.contracts.source_architecture import CoverageAssessmentV2, NormalizedCheckResult
from app.contracts.summary_v3 import SummaryV3


class CompanyCheckMode(StrEnum):
    QUICK = "QUICK"
    FULL = "FULL"
    REFRESH_DUE = "REFRESH_DUE"


class CompanyCheckStatus(StrEnum):
    SUCCESS_FOUND = "SUCCESS_FOUND"
    SUCCESS_NOT_FOUND = "SUCCESS_NOT_FOUND"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
    ACCESS_PENDING = "ACCESS_PENDING"
    SOURCE_BLOCKED = "SOURCE_BLOCKED"
    ERROR = "ERROR"


class CompanyCheckOutcome(ContractModel):
    check_code: str = Field(min_length=1, max_length=120)
    status: CompanyCheckStatus
    source_code: str = Field(min_length=1, max_length=100)
    checked_at: datetime | None = None
    source_as_of: datetime | None = None
    source_url: str | None = None
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("checked_at", "source_as_of")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Orchestrator timestamps must include a timezone")
        return value


class CompanyCheckResult(ContractModel):
    run_id: str
    company_inn: str
    mode: CompanyCheckMode
    started_at: datetime
    completed_at: datetime
    outcomes: tuple[CompanyCheckOutcome, ...]
    risk: RiskAssessmentResult
    summary: SummaryResult
    human_action_queue: tuple[CompanyCheckOutcome, ...] = ()
    normalized_results: tuple[NormalizedCheckResult, ...] = ()
    coverage_v2: CoverageAssessmentV2 | None = None
    risk_v3: RiskAssessmentV3 | None = None
    summary_v3: SummaryV3 | None = None
