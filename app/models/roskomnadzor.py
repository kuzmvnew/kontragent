from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Identity, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class RoskomnadzorCompanyFact(Base):
    """Sanitised facts that may be joined to a legal entity by exact INN."""

    __tablename__ = "roskomnadzor_company_facts"
    __table_args__ = (UniqueConstraint("dataset_id", "record_key", name="uq_rkn_company_fact"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("data_sets.id", ondelete="CASCADE"), index=True)
    data_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    record_key: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    inn: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    ogrn: Mapped[str | None] = mapped_column(String(13), index=True)
    external_number: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    public_details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RoskomnadzorPrivatePersonRecord(Base):
    """Closed staging for people/IPs. No product service is allowed to read it."""

    __tablename__ = "roskomnadzor_private_person_records"
    __table_args__ = (UniqueConstraint("dataset_id", "record_key", name="uq_rkn_private_person_record"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("data_sets.id", ondelete="CASCADE"), index=True)
    data_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    record_key: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    identifier_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    identifier_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    private_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RoskomnadzorPdOperatorCheck(Base):
    __tablename__ = "roskomnadzor_pd_operator_checks"
    __table_args__ = (UniqueConstraint("dataset_id", "inn", "request_date", name="uq_rkn_pd_operator_check"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("data_sets.id", ondelete="CASCADE"), index=True)
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
