"""Immutable PostgreSQL persistence for normalized v3 assessments and summaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from app.contracts.risk import RiskProfile
from app.contracts.risk_v3 import RiskAssessmentV3
from app.contracts.source_architecture import NormalizedCheckResult
from app.contracts.summary_v3 import SummaryV3
from app.database.postgres import get_session
from app.models.company import Company
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from app.services.risk_engine_v3_service import build_risk_v3
from app.services.summary_engine_v3_service import build_summary_v3


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def persist_v3_assessment(
    inn: str, resolved: Mapping[str, NormalizedCheckResult], *, profile: RiskProfile,
    now: datetime | None = None,
) -> tuple[RiskAssessmentV3, SummaryV3, str, bool]:
    now = now or datetime.now(timezone.utc)
    normalized_payload = [item.model_dump(mode="json") for item in resolved.values()]
    input_hash = hashlib.sha256(_canonical(normalized_payload).encode()).hexdigest()
    with get_session() as session:
        company_id = session.scalar(select(Company.id).where(Company.inn == inn))
        if company_id is None:
            raise ValueError("Company not found")
        previous = session.scalar(
            select(CompanyRiskAssessmentV3)
            .where(
                CompanyRiskAssessmentV3.company_id == company_id,
                CompanyRiskAssessmentV3.input_hash == input_hash,
                CompanyRiskAssessmentV3.risk_engine_version == "risk-engine-3.1.0",
            )
            .order_by(CompanyRiskAssessmentV3.id.desc()).limit(1)
        )
        if previous:
            summary_row = session.scalar(select(CompanySummaryV3).where(CompanySummaryV3.risk_assessment_id == previous.assessment_id))
            if summary_row and summary_row.summary_engine_version == "summary-engine-3.1.1":
                return RiskAssessmentV3.model_validate(previous.result_payload), SummaryV3.model_validate(summary_row.structured_payload), previous.assessment_id, True
    risk = build_risk_v3(resolved, profile=profile)
    summary = build_summary_v3(risk, resolved)
    assessment_id, summary_id = str(uuid4()), str(uuid4())
    with get_session() as session:
        session.add(CompanyRiskAssessmentV3(
            assessment_id=assessment_id, company_id=company_id, input_hash=input_hash,
            risk_engine_version=risk.version, coverage_engine_version=risk.coverage.version,
            calculated_at=now, risk_score=risk.risk_score, risk_label=risk.label,
            normalized_results=normalized_payload, coverage=risk.coverage.model_dump(mode="json"),
            result_payload=risk.model_dump(mode="json"),
        ))
        session.flush()
        session.add(CompanySummaryV3(
            summary_id=summary_id, company_id=company_id, risk_assessment_id=assessment_id,
            summary_engine_version=summary.version, generated_at=now,
            structured_payload=summary.model_dump(mode="json"),
        ))
        session.commit()
    return risk, summary, assessment_id, False


def get_latest_v3(inn: str) -> tuple[RiskAssessmentV3, SummaryV3] | None:
    with get_session() as session:
        company_id = session.scalar(select(Company.id).where(Company.inn == inn))
        if company_id is None: return None
        row = session.scalar(select(CompanyRiskAssessmentV3).where(CompanyRiskAssessmentV3.company_id == company_id).order_by(CompanyRiskAssessmentV3.id.desc()).limit(1))
        if row is None: return None
        summary = session.scalar(select(CompanySummaryV3).where(CompanySummaryV3.risk_assessment_id == row.assessment_id))
        return RiskAssessmentV3.model_validate(row.result_payload), SummaryV3.model_validate(summary.structured_payload)
