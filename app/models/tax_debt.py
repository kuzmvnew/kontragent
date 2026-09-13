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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class CompanyTaxDebtSnapshot(Base):
    """
    Исторический снимок налоговой задолженности.

    Одна строка =
    одна компания + один dataset + одна дата состояния.

    Например:
    ИНН 7722858778
    дата состояния 01.08.2026
    общий долг 855 846.65 ₽
    """

    __tablename__ = "company_tax_debt_snapshots"

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "dataset_id",
            "data_date",
            name=(
                "uq_company_tax_debt_"
                "company_dataset_date"
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

    document_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    source_document_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    total_arrears: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    total_penalties: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    total_fines: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    total_debt: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
        index=True,
    )

    item_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
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

    items: Mapped[list["CompanyTaxDebtItem"]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )


class CompanyTaxDebtItem(Base):
    """
    Детализация одного снимка задолженности.

    Например:

    НДС
        недоимка = 660 849.22

    Суммы пеней
        пени = 182 438.43
    """

    __tablename__ = "company_tax_debt_items"

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "tax_name",
            name=(
                "uq_company_tax_debt_"
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
            "company_tax_debt_snapshots.id",
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

    arrears: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    penalties: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    fines: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    total: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
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

    snapshot: Mapped["CompanyTaxDebtSnapshot"] = relationship(
        back_populates="items",
    )