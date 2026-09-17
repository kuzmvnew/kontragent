from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Identity, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CorporateDisclosureCheck(Base):
    """Dated exact-INN cache of a public issuer disclosure page."""

    __tablename__ = "corporate_disclosure_checks"
    __table_args__ = (
        UniqueConstraint("dataset_id", "inn", "request_date", name="uq_corporate_disclosure_check"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True)
    inn: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    request_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    result_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    is_found: Mapped[bool | None] = mapped_column(Boolean, index=True)
    profile: Mapped[dict | None] = mapped_column(JSONB)
    documents: Mapped[list | None] = mapped_column(JSONB)
    document_count: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(120), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
