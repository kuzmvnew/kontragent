from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Identity, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class NostroyMemberCheck(Base):
    """Dated on-demand cache; public payload excludes contacts and person fields."""

    __tablename__ = "nostroy_member_checks"
    __table_args__ = (UniqueConstraint("inn", "request_date", name="uq_nostroy_member_check_inn_date"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    inn: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    request_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    result_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    is_found: Mapped[bool | None] = mapped_column(Boolean, index=True)
    record_count: Mapped[int | None] = mapped_column(Integer)
    public_records: Mapped[list | None] = mapped_column(JSONB)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(120), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class NoprizMemberCheck(Base):
    __tablename__ = "nopriz_member_checks"
    __table_args__ = (UniqueConstraint("inn", "request_date", name="uq_nopriz_member_check_inn_date"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    inn: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    request_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    result_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    is_found: Mapped[bool | None] = mapped_column(Boolean, index=True)
    record_count: Mapped[int | None] = mapped_column(Integer)
    public_records: Mapped[list | None] = mapped_column(JSONB)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(120), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class SroPersonRegistryRecord(Base):
    """Private/internal person evidence. No public product path may import it."""

    __tablename__ = "sro_person_registry_records"
    __table_args__ = (UniqueConstraint("source_code", "source_record_id", name="uq_sro_person_source_record"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_record_id: Mapped[str] = mapped_column(String(100), nullable=False)
    official_registration_number: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    person_name: Mapped[str] = mapped_column(Text, nullable=False)
    company_inn: Mapped[str | None] = mapped_column(String(10), index=True)
    relationship_status: Mapped[str] = mapped_column(String(40), nullable=False, default="manual_review_required")
    professional_status: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status_date: Mapped[date | None] = mapped_column(Date)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    match_confidence: Mapped[str] = mapped_column(String(40), nullable=False, default="official_record_id")
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    public_visibility: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    access_classification: Mapped[str] = mapped_column(String(40), nullable=False, default="PRIVATE_INTERNAL")
    retention_policy: Mapped[str] = mapped_column(String(120), nullable=False, default="review_annually_or_on_source_change")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
