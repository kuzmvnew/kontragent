"""Immutable persistence and deterministic reuse for internal Risk/Summary v3."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.risk_v3 import (
    NormalizedEvidenceCandidate,
    RiskAssessmentV3,
    SubjectScope,
    UnsupportedSubjectOutcome,
)
from app.contracts.summary_v3 import SummaryV3
from app.models.company import Company
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from app.services.risk_engine_v3_service import calculate_risk_v3
from app.services.risk_v3_evidence_service import load_persisted_evidence_candidates
from app.services.summary_engine_v3_service import (
    PROJECTION_POLICY_VERSION,
    SUMMARY_MODEL_VERSION,
    build_summary_v3,
)


def _risk_from_row(row: CompanyRiskAssessmentV3) -> RiskAssessmentV3:
    return RiskAssessmentV3.model_validate(row.result_payload)


def _summary_from_row(row: CompanySummaryV3) -> SummaryV3:
    return SummaryV3.model_validate(row.structured_payload)


def persist_risk_assessment_v3(
    session: Session,
    assessment: RiskAssessmentV3,
) -> tuple[RiskAssessmentV3, bool]:
    """Persist once, or return the immutable compatible assessment."""

    existing = session.scalar(
        select(CompanyRiskAssessmentV3).where(
            CompanyRiskAssessmentV3.company_id == assessment.company_id,
            CompanyRiskAssessmentV3.input_hash == assessment.input_hash,
        )
    )
    if existing is not None:
        persisted = _risk_from_row(existing)
        version_fields = (
            "risk_model_version",
            "ruleset_version",
            "coverage_policy_version",
            "applicability_policy_version",
            "source_resolution_policy_version",
            "freshness_policy_version",
        )
        if any(
            getattr(persisted, field) != getattr(assessment, field)
            for field in version_fields
        ):
            raise ValueError("Risk v3 input identity collided across policy versions")
        return persisted, True

    if session.get(Company, assessment.company_id) is None:
        raise ValueError("Company not found")
    payload = assessment.model_dump(mode="json")
    session.add(
        CompanyRiskAssessmentV3(
            assessment_id=assessment.assessment_id,
            company_id=assessment.company_id,
            subject_scope=assessment.subject_scope.value,
            risk_model_version=assessment.risk_model_version,
            ruleset_version=assessment.ruleset_version,
            coverage_policy_version=assessment.coverage_policy_version,
            applicability_policy_version=assessment.applicability_policy_version,
            source_resolution_policy_version=(
                assessment.source_resolution_policy_version
            ),
            freshness_policy_version=assessment.freshness_policy_version,
            input_hash=assessment.input_hash,
            calculated_at=assessment.calculated_at,
            evidence_snapshot=payload["evidence_snapshot"],
            resolved_checks=payload["resolved_checks"],
            factors=payload["factors"],
            coverage_snapshot=payload["coverage_snapshot"],
            mandatory_gate=payload["mandatory_gate"],
            limitations=payload["limitations"],
            result_payload=payload,
        )
    )
    session.flush()
    return assessment, False


def calculate_and_persist_risk_v3(
    session: Session,
    candidates: Sequence[NormalizedEvidenceCandidate],
    *,
    company_id: int,
    subject_scope: SubjectScope,
    calculated_at: datetime | None = None,
) -> tuple[RiskAssessmentV3 | UnsupportedSubjectOutcome, bool]:
    result = calculate_risk_v3(
        candidates,
        company_id=company_id,
        subject_scope=subject_scope,
        calculated_at=calculated_at,
    )
    if isinstance(result, UnsupportedSubjectOutcome):
        return result, False
    return persist_risk_assessment_v3(session, result)


def calculate_company_risk_v3_from_persisted(
    session: Session,
    company_id: int,
    *,
    calculated_at: datetime | None = None,
) -> tuple[RiskAssessmentV3 | UnsupportedSubjectOutcome, bool]:
    """DB-only internal entry point; the captured rows are then frozen inputs."""

    captured_at = calculated_at or datetime.now(timezone.utc)
    snapshot = load_persisted_evidence_candidates(
        session, company_id, captured_at=captured_at
    )
    return calculate_and_persist_risk_v3(
        session,
        snapshot.candidates,
        company_id=snapshot.company_id,
        subject_scope=snapshot.subject_scope,
        calculated_at=captured_at,
    )


def get_risk_assessment_v3(
    session: Session, assessment_id: str
) -> RiskAssessmentV3 | None:
    row = session.scalar(
        select(CompanyRiskAssessmentV3).where(
            CompanyRiskAssessmentV3.assessment_id == assessment_id
        )
    )
    return _risk_from_row(row) if row else None


def get_or_create_summary_v3(
    session: Session,
    risk_assessment_id: str,
    *,
    summary_model_version: str = SUMMARY_MODEL_VERSION,
    projection_policy_version: str = PROJECTION_POLICY_VERSION,
    generated_at: datetime | None = None,
) -> tuple[SummaryV3, bool]:
    """Read one persisted Risk v3 row and project it without domain rereads."""

    existing = session.scalar(
        select(CompanySummaryV3).where(
            CompanySummaryV3.risk_assessment_id == risk_assessment_id,
            CompanySummaryV3.summary_model_version == summary_model_version,
            CompanySummaryV3.projection_policy_version == projection_policy_version,
        )
    )
    if existing is not None:
        return _summary_from_row(existing), True

    risk_row = session.scalar(
        select(CompanyRiskAssessmentV3).where(
            CompanyRiskAssessmentV3.assessment_id == risk_assessment_id
        )
    )
    if risk_row is None:
        raise ValueError("Persisted RiskAssessmentV3 not found")
    assessment = _risk_from_row(risk_row)
    summary = build_summary_v3(
        assessment,
        generated_at=generated_at or datetime.now(timezone.utc),
        summary_model_version=summary_model_version,
        projection_policy_version=projection_policy_version,
    )
    payload = summary.model_dump(mode="json")
    session.add(
        CompanySummaryV3(
            summary_id=summary.summary_id,
            company_id=summary.company_id,
            risk_assessment_id=summary.risk_assessment_id,
            summary_model_version=summary.summary_model_version,
            projection_policy_version=summary.projection_policy_version,
            generated_at=summary.generated_at,
            structured_payload=payload,
            explainability_refs=payload["traceability"],
        )
    )
    session.flush()
    return summary, False


def get_summary_v3(session: Session, summary_id: str) -> SummaryV3 | None:
    row = session.scalar(
        select(CompanySummaryV3).where(CompanySummaryV3.summary_id == summary_id)
    )
    return _summary_from_row(row) if row else None
