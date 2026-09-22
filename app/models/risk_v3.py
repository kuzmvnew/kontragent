"""Isolated immutable persistence family for Risk/Summary v3."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CompanyRiskAssessmentV3(Base):
    __tablename__ = "company_risk_assessments_v3"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "input_hash",
            name="uq_company_risk_assessments_v3_input_identity",
        ),
        Index(
            "ix_company_risk_assessments_v3_company_calculated",
            "company_id",
            "calculated_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    assessment_id: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_scope: Mapped[str] = mapped_column(String(40), nullable=False)
    risk_model_version: Mapped[str] = mapped_column(String(40), nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(80), nullable=False)
    coverage_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    applicability_policy_version: Mapped[str] = mapped_column(
        String(80), nullable=False
    )
    source_resolution_policy_version: Mapped[str] = mapped_column(
        String(80), nullable=False
    )
    freshness_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    evidence_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    resolved_checks: Mapped[list] = mapped_column(JSONB, nullable=False)
    factors: Mapped[list] = mapped_column(JSONB, nullable=False)
    coverage_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    mandatory_gate: Mapped[dict] = mapped_column(JSONB, nullable=False)
    limitations: Mapped[list] = mapped_column(JSONB, nullable=False)
    result_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompanySummaryV3(Base):
    __tablename__ = "company_summaries_v3"
    __table_args__ = (
        UniqueConstraint(
            "risk_assessment_id",
            "summary_model_version",
            "projection_policy_version",
            name="uq_company_summaries_v3_cache_identity",
        ),
        Index(
            "ix_company_summaries_v3_company_generated",
            "company_id",
            "generated_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    summary_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    risk_assessment_id: Mapped[str] = mapped_column(
        ForeignKey(
            "company_risk_assessments_v3.assessment_id", ondelete="CASCADE"
        ),
        nullable=False,
        index=True,
    )
    summary_model_version: Mapped[str] = mapped_column(String(40), nullable=False)
    projection_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    structured_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    explainability_refs: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _immutable(_mapper, _connection, target) -> None:
    raise ValueError(f"{target.__class__.__name__} rows are immutable")


event.listen(CompanyRiskAssessmentV3, "before_update", _immutable)
event.listen(CompanySummaryV3, "before_update", _immutable)
