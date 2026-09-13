from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CompanyMspProfile(Base):
    """
    Текущее состояние компании
    в Едином реестре субъектов МСП.

    История изменений позже будет
    храниться отдельно через events/history.
    """

    __tablename__ = "company_msp_profiles"

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "dataset_id",
            name="uq_company_msp_profile_company_dataset",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )

    company_id: Mapped[int] = mapped_column(
        ForeignKey(
            "companies.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    dataset_id: Mapped[int] = mapped_column(
        ForeignKey(
            "data_sets.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    data_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )

    inclusion_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    # Исходный код ФНС:
    # 1 / 2 и т.д.
    subject_type_code: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        index=True,
    )

    category_code: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        index=True,
    )

    is_new_code: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
    )

    social_enterprise_code: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
    )

    employee_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    source_document_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )