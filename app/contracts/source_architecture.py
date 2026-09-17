"""Canonical source-capability and normalized-check contracts.

The contracts deliberately distinguish source availability, company-level
resolution and business risk.  A configured dataset is not evidence that a
specific company check completed.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.contracts.decision import ContractModel
from app.contracts.risk import RiskProfile


class SourceClass(StrEnum):
    OFFICIAL_DIRECT = "OFFICIAL_DIRECT"
    OFFICIAL_DOWNLOADED_DATASET = "OFFICIAL_DOWNLOADED_DATASET"
    AUTHORIZED_BRIDGE = "AUTHORIZED_BRIDGE"
    DISCOVERY_ONLY = "DISCOVERY_ONLY"


class NormalizedResultStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class FreshnessStatus(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class CapabilityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    USER_TRIGGERED = "USER_TRIGGERED"
    ACCESS_PENDING = "ACCESS_PENDING"
    BLOCKED = "BLOCKED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class SourcePath(ContractModel):
    source_code: str
    source_class: SourceClass
    priority: int = Field(ge=0)


class SourceCapability(ContractModel):
    capability_id: str
    domain: str
    human_name: str
    profiles: tuple[RiskProfile, ...]
    mandatory_for_profiles: tuple[RiskProfile, ...] = ()
    risk_weight: float = Field(ge=0)
    coverage_weight: float = Field(gt=0)
    freshness_policy: dict[str, Any]
    accepted_source_paths: tuple[SourcePath, ...]
    source_precedence: tuple[SourceClass, ...]
    bridge_allowed: bool = False
    negative_bridge_allowed: bool = False
    runner: str | None = None
    fallback_runner: str | None = None
    status: CapabilityStatus
    last_verified_at: datetime | None = None

    @field_validator("last_verified_at")
    @classmethod
    def timestamp_has_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("last_verified_at must include a timezone")
        return value


class NormalizedEvidence(ContractModel):
    fact: str
    value: Any = None
    evidence_id: str
    snapshot_hash: str | None = None
    calculation: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedCheckResult(ContractModel):
    check_code: str
    result: NormalizedResultStatus
    source_class: SourceClass
    source_code: str
    original_source: str | None = None
    exact_identifier_match: bool | None = None
    checked_at: datetime | None = None
    source_as_of: datetime | None = None
    freshness: FreshnessStatus = FreshnessStatus.UNKNOWN
    coverage: float = Field(default=0, ge=0, le=1)
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: tuple[NormalizedEvidence, ...] = ()
    source_url: str | None = None
    limitation: str | None = None
    resolved_by: tuple[str, ...] = ()

    @field_validator("checked_at", "source_as_of")
    @classmethod
    def timestamps_have_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("normalized result timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_resolution_semantics(self) -> "NormalizedCheckResult":
        if self.result in {NormalizedResultStatus.FOUND, NormalizedResultStatus.NOT_FOUND}:
            if self.coverage <= 0:
                raise ValueError("completed results require non-zero coverage")
            if self.exact_identifier_match is False:
                raise ValueError("completed result cannot use a mismatched identifier")
        if self.result == NormalizedResultStatus.NOT_APPLICABLE and self.coverage != 1:
            raise ValueError("NOT_APPLICABLE uses coverage=1 and is removed from the denominator")
        if self.source_class == SourceClass.DISCOVERY_ONLY and self.result == NormalizedResultStatus.NOT_FOUND:
            raise ValueError("discovery-only evidence cannot prove absence")
        return self


class CoverageBreakdown(ContractModel):
    official_direct: int = 0
    official_datasets: int = 0
    authorized_bridge: int = 0
    partial: int = 0
    unresolved: int = 0
    not_applicable: int = 0


class CoverageAssessmentV2(ContractModel):
    version: str = "coverage-engine-2.0.0"
    profile: RiskProfile
    coverage_score: int = Field(ge=0, le=100)
    mandatory_score: int = Field(ge=0, le=100)
    mandatory_hard_checks_resolved: bool
    resolved_capabilities: tuple[str, ...]
    unresolved_capabilities: tuple[str, ...]
    partial_capabilities: tuple[str, ...]
    not_applicable_capabilities: tuple[str, ...]
    breakdown: CoverageBreakdown
    calculation: str
