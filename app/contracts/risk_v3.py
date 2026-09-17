"""Explainable 0-100 product risk-index contracts."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.contracts.decision import ContractModel
from app.contracts.risk import RiskProfile
from app.contracts.source_architecture import CoverageAssessmentV2, SourceClass


class RiskPoint(ContractModel):
    capability_id: str
    section: str
    source_code: str
    source_class: SourceClass
    fact: str
    rule: str
    raw_points: float = Field(ge=0)
    evidence_strength: float = Field(ge=0, le=1)
    points: float = Field(ge=0)
    calculation: str
    source_as_of: str | None = None
    evidence_ids: tuple[str, ...] = ()
    details: dict[str, Any] = Field(default_factory=dict)


class RiskAssessmentV3(ContractModel):
    version: str = "risk-engine-3.0.2"
    profile: RiskProfile
    risk_score: int = Field(ge=0, le=100)
    reliability_index: int = Field(ge=0, le=100)
    label: str
    overall: str
    preliminary: bool
    coverage: CoverageAssessmentV2
    points: tuple[RiskPoint, ...]
    section_scores: dict[str, float]
    explanation: str
