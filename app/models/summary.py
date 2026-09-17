from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CompanySummary(Base):
    """Immutable persisted projection of one Risk Engine assessment."""

    __tablename__ = "company_summaries"
    __table_args__ = (
        UniqueConstraint(
            "risk_assessment_id", "mode", "summary_engine_version",
            "deal_context_hash", "projection_policy_version",
            name="uq_company_summaries_cache_key",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    summary_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    risk_assessment_id: Mapped[str] = mapped_column(
        ForeignKey("company_risk_assessments.assessment_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    summary_engine_version: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    risk_engine_version: Mapped[str] = mapped_column(String(40), nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(80), nullable=False)
    deal_context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    structured_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    text_blocks: Mapped[dict] = mapped_column(JSONB, nullable=False)
    explainability_refs: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
