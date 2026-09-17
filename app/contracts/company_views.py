"""Stable projections for public, authenticated and internal company reads."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PublicRiskView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    score: int | None = Field(default=None, ge=0, le=100)
    label: str | None = None
    coverage_score: int | None = Field(default=None, ge=0, le=100)
    workflow_completion_percent: int | None = Field(default=None, ge=0, le=100)


class PublicSummaryView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conclusion: str | None = None
    main_reasons: tuple[str, ...] = ()
    positive_checks: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()


class PublicCompanyView(BaseModel):
    """Anonymous contract: business facts only, without raw/internal payloads."""

    model_config = ConfigDict(extra="ignore")

    inn: str
    ogrn: str | None = None
    kpp: str | None = None
    name: str
    short_name: str | None = None
    full_name: str | None = None
    entity_type: str | None = None
    status: str | None = None
    registration_date: date | None = None
    termination_date: date | None = None
    address: str | None = None
    region_code: str | None = None
    activity: str | None = None
    okved: str | None = None
    risk: PublicRiskView | None = None
    summary: PublicSummaryView | None = None
    sources: tuple[str, ...] = ()

    @classmethod
    def from_read_model(cls, company: dict[str, Any]) -> "PublicCompanyView":
        risk_payload = company.get("risk_v3") or company.get("risk_assessment") or {}
        coverage = risk_payload.get("coverage") or {}
        score = risk_payload.get("risk_score")
        if score is None:
            score = company.get("risk_score")
        risk = None
        if score is not None or coverage:
            coverage_score = coverage.get("coverage_score")
            if coverage_score is None:
                coverage_score = risk_payload.get("coverage_score")
            workflow_completion = risk_payload.get("workflow_completion_percent")
            if workflow_completion is None:
                workflow_completion = coverage.get("workflow_completion_percent")
            risk = PublicRiskView(
                score=score,
                label=(
                    risk_payload.get("label")
                    or risk_payload.get("overall")
                    or risk_payload.get("risk_label")
                    or company.get("risk_label")
                ),
                coverage_score=coverage_score,
                workflow_completion_percent=workflow_completion,
            )

        raw_summary = company.get("summary_v3") or company.get("summary") or {}
        summary = None
        if raw_summary:
            summary = PublicSummaryView(
                conclusion=raw_summary.get("conclusion") or raw_summary.get("executive_summary"),
                main_reasons=tuple(raw_summary.get("main_reasons") or ()),
                positive_checks=tuple(raw_summary.get("positive_checks") or ()),
                limitations=tuple(raw_summary.get("limitations") or ()),
                recommendations=tuple(raw_summary.get("recommendations") or ()),
            )

        return cls.model_validate({
            **company,
            "risk": risk,
            "summary": summary,
            "sources": tuple(company.get("sources_used") or ()),
        })


class AuthenticatedCompanyView(PublicCompanyView):
    """Reserved authenticated projection; no debug or security material."""

    director_name: str | None = None
    director_position: str | None = None
    revenue: int | float | None = None
    employee_count: int | None = None


class InternalCompanyView(AuthenticatedCompanyView):
    """Protected technical projection for diagnostics and provenance."""

    internal_id: int | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
