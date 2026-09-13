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


class CompanyHeadcount(Base):
    """
    История среднесписочной численности
    работников компании.

    Каждая строка относится к:
    - одной компании;
    - одному году;
    - одному dataset.
    """

    __tablename__ = "company_headcounts"

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "year",
            "dataset_id",
            name=(
                "uq_company_headcounts_"
                "company_year_dataset"
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

    year: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
    )

    employee_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    source_document_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    source_document_date: Mapped[date | None] = mapped_column(
        Date,
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