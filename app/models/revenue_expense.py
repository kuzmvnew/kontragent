from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
)

from app.database.base import Base


class CompanyRevenueExpenseSnapshot(Base):
    """
    Исторический снимок доходов и расходов
    юридического лица по данным ФНС REVEXP.

    Одна строка соответствует:

    - одной компании;
    - одному набору данных;
    - одной дате состояния сведений.

    profit_loss рассчитывается как:

    revenue - expenses
    """

    __tablename__ = (
        "company_revenue_expense_snapshots"
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "dataset_id",
            "data_date",
            name=(
                "uq_company_revexp_"
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

    data_year: Mapped[int] = mapped_column(
        nullable=False,
        index=True,
    )

    document_date: Mapped[
        date | None
    ] = mapped_column(
        Date,
        nullable=True,
    )

    source_document_id: Mapped[
        str
    ] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )

    source_company_name: Mapped[
        str | None
    ] = mapped_column(
        String(1000),
        nullable=True,
    )

    revenue: Mapped[
        Decimal
    ] = mapped_column(
        Numeric(
            precision=22,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    expenses: Mapped[
        Decimal
    ] = mapped_column(
        Numeric(
            precision=22,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    profit_loss: Mapped[
        Decimal
    ] = mapped_column(
        Numeric(
            precision=22,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    created_at: Mapped[
        datetime
    ] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[
        datetime
    ] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )