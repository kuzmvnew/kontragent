from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class RoszdravLicenseEntry(Base):
    __tablename__ = "roszdrav_license_entries"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "record_key", name="uq_roszdrav_license_record"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), index=True
    )
    data_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    record_key: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    inn: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    ogrn: Mapped[str | None] = mapped_column(String(15), index=True)
    license_number: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    licensee_name: Mapped[str | None] = mapped_column(Text)
    authority_name: Mapped[str | None] = mapped_column(Text)
    activity_type: Mapped[str | None] = mapped_column(Text)
    legal_form: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    work_places: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    decision_date: Mapped[date | None] = mapped_column(Date)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    termination_info: Mapped[str | None] = mapped_column(Text)
    termination_date: Mapped[date | None] = mapped_column(Date)
    suspension_info: Mapped[str | None] = mapped_column(Text)
    cancellation_info: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RoszdravUnifiedLicenseCheck(Base):
    __tablename__ = "roszdrav_unified_license_checks"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "inn", "request_date",
            name="uq_roszdrav_unified_license_check",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), index=True
    )
    inn: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    request_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    result_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    is_found: Mapped[bool | None] = mapped_column(Boolean, index=True)
    records: Mapped[list | None] = mapped_column(JSONB)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(120), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )


class RoszdravMedicalDeviceCheck(Base):
    __tablename__ = "roszdrav_medical_device_checks"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "registration_number", "request_date",
            name="uq_roszdrav_medical_device_check",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), index=True
    )
    registration_number: Mapped[str] = mapped_column(
        String(160), nullable=False, index=True
    )
    request_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    result_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    is_found: Mapped[bool | None] = mapped_column(Boolean, index=True)
    total_results: Mapped[int | None] = mapped_column(Integer)
    records: Mapped[list | None] = mapped_column(JSONB)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(120), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )


class RoszdravClinicalOrganizationEntry(Base):
    __tablename__ = "roszdrav_clinical_organization_entries"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "record_key", name="uq_roszdrav_clinical_record"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), index=True
    )
    data_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    record_key: Mapped[str] = mapped_column(String(64), nullable=False)
    inn: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    included_at: Mapped[date | None] = mapped_column(Date)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
