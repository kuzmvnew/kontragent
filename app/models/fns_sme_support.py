from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class FnsSmeSupportEntry(Base):
    """One support fact from the published FNS SME-support snapshot."""

    __tablename__ = "fns_sme_support_entries"
    __table_args__ = (
        UniqueConstraint(
            "ingestion_run_id",
            "source_record_key",
            name="uq_fns_sme_support_run_record_key",
        ),
        Index(
            "ix_fns_sme_support_run_inn_decision",
            "ingestion_run_id",
            "recipient_inn",
            "decision_date",
        ),
        Index(
            "ix_fns_sme_support_dataset_run",
            "dataset_id",
            "ingestion_run_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False
    )
    ingestion_run_id: Mapped[int] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"), nullable=False
    )
    data_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_record_key: Mapped[str] = mapped_column(String(220), nullable=False)
    source_document_id: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_inn: Mapped[str | None] = mapped_column(String(10), nullable=True)
    recipient_inn: Mapped[str] = mapped_column(String(12), nullable=False)
    recipient_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient_ogrn: Mapped[str | None] = mapped_column(String(15), nullable=True)
    information_date: Mapped[date] = mapped_column(Date, nullable=False)
    support_until: Mapped[date] = mapped_column(Date, nullable=False)
    decision_date: Mapped[date] = mapped_column(Date, nullable=False)
    termination_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    violation_code: Mapped[str | None] = mapped_column(String(1), nullable=True)
    support_form_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    support_form_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    support_type_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    support_type_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    amounts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    violations: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    regulatory_document_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
