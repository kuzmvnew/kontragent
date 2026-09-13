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
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CompanyTaxOffence(Base):
    """
    Один официальный документ ФНС
    о налоговом правонарушении.

    В текущем наборе taxoffence ФНС
    доступны:

    - ИНН организации
    - дата документа
    - дата состояния данных
    - идентификатор документа
    - сумма штрафа

    Вид налогового правонарушения
    в XML не публикуется.
    """

    __tablename__ = "company_tax_offences"

    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "source_document_id",
            name=(
                "uq_company_tax_offence_"
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

    document_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    source_document_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )

    fine_amount: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
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