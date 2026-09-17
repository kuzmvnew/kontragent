from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CompanyRiskAssessmentV3(Base):
    __tablename__ = "company_risk_assessments_v3"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    assessment_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    risk_engine_version: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    coverage_engine_version: Mapped[str] = mapped_column(String(40), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    risk_label: Mapped[str] = mapped_column(String(80), nullable=False)
    normalized_results: Mapped[list] = mapped_column(JSONB, nullable=False)
    coverage: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CompanySummaryV3(Base):
    __tablename__ = "company_summaries_v3"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    summary_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    risk_assessment_id: Mapped[str] = mapped_column(ForeignKey("company_risk_assessments_v3.assessment_id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    summary_engine_version: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    structured_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
