from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Identity, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


COMPANY_FACT_TYPES = (
    "website",
    "phone",
    "email",
    "registered_address",
    "factual_address",
    "postal_address",
    "mass_address",
    "mass_director",
    "mass_founder",
    "public_bank_details",
    "related_company",
)


class CompanyPublicFact(Base):
    """Source-backed company fact; absence of a row is never a negative fact."""

    __tablename__ = "company_public_facts"
    __table_args__ = (
        CheckConstraint(
            "fact_type IN (" + ", ".join(f"'{value}'" for value in COMPANY_FACT_TYPES) + ")",
            name="ck_company_public_fact_type",
        ),
        UniqueConstraint(
            "company_id", "fact_type", "value_hash", "source_code", "source_identifier",
            name="uq_company_public_fact_evidence",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("data_sets.id", ondelete="SET NULL"), index=True)
    fact_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source_identifier: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    publication_date: Mapped[date | None] = mapped_column(Date, index=True)
    effective_from: Mapped[date | None] = mapped_column(Date, index=True)
    effective_to: Mapped[date | None] = mapped_column(Date, index=True)
    currentness: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    confidence: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
