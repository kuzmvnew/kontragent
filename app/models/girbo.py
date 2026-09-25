from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Identity, Integer, Numeric, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class GirboAccountingReport(Base):
    """Versioned official accounting report; corrections remain queryable."""

    __tablename__ = "girbo_accounting_reports"
    __table_args__ = (
        UniqueConstraint("dataset_id", "report_id", "revision", name="uq_girbo_report_revision"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reporting_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    publication_date: Mapped[date | None] = mapped_column(Date)
    correction_date: Mapped[date | None] = mapped_column(Date)
    source_data_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"), index=True
    )
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    expenses: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    profit_loss: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    assets: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    liabilities: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    equity: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    statement_values: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    raw_checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
