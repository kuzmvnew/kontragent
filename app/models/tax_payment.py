from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
)

from app.database.base import Base


class CompanyTaxPaymentSnapshot(Base):
    """
    Исторический снимок уплаченных налогов
    и иных платежей организации по данным ФНС.

    Одна строка =
    одна компания + один dataset + одна дата данных.

    Пример:

    ИНН: 7802670891
    дата данных: 31.12.2025
    год: 2025

    налог на добавленную стоимость
    + налог на прибыль
    + страховые взносы
    + пени
    + иные позиции
    """

    __tablename__ = "company_tax_payment_snapshots"

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "dataset_id",
            "data_date",
            name=(
                "uq_company_tax_payment_"
                "company_dataset_date"
            ),
        ),
        UniqueConstraint(
            "dataset_id",
            "source_document_id",
            name=(
                "uq_company_tax_payment_"
                "dataset_document"
            ),
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

    data_year: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
    )

    document_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    source_document_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )

    source_company_name: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    # -----------------------------------------------------
    # TOTALS
    # -----------------------------------------------------

    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=22,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
        index=True,
    )

    tax_amount: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=22,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    insurance_amount: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=22,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    penalty_amount: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=22,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    non_tax_amount: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=22,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    other_amount: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=22,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    # -----------------------------------------------------
    # ITEM COUNTS
    # -----------------------------------------------------

    source_item_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    stored_item_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # -----------------------------------------------------
    # TIMESTAMPS
    # -----------------------------------------------------

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

    items: Mapped[
        list["CompanyTaxPaymentItem"]
    ] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )


class CompanyTaxPaymentItem(Base):
    """
    Ненулевая позиция платежа внутри
    одного снимка PAYTAX.

    Нулевые строки ФНС мы сознательно
    не сохраняем.

    Исходное название платежа всегда
    сохраняется без изменений.
    """

    __tablename__ = "company_tax_payment_items"

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "tax_name",
            name=(
                "uq_company_tax_payment_"
                "snapshot_tax_name"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )

    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey(
            "company_tax_payment_snapshots.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    tax_name: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
        index=True,
    )

    payment_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
    )

    amount: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=22,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
        index=True,
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

    snapshot: Mapped[
        "CompanyTaxPaymentSnapshot"
    ] = relationship(
        back_populates="items",
    )