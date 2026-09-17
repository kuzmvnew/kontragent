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
