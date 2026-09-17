from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CompanyRiskAssessment(Base):
    """Immutable, reproducible Risk Engine calculation."""

    __tablename__ = "company_risk_assessments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    assessment_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    risk_engine_version: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    ruleset_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    ruleset_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    deal_context_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    change_origin: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    input_snapshot_refs: Mapped[list] = mapped_column(JSONB, nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    signals: Mapped[list] = mapped_column(JSONB, nullable=False)
    section_assessments: Mapped[list] = mapped_column(JSONB, nullable=False)
    coverage: Mapped[dict] = mapped_column(JSONB, nullable=False)
    completeness: Mapped[dict] = mapped_column(JSONB, nullable=False)
    overall_status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    limitations: Mapped[list] = mapped_column(JSONB, nullable=False)
    result_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

