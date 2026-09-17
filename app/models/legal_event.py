from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


LEGAL_EVENT_TYPES = (
    "bankruptcy_intent",
    "bankruptcy_application_filed",
    "bankruptcy_application_accepted",
    "bankruptcy_observation",
    "bankruptcy_financial_rehabilitation",
    "bankruptcy_external_administration",
    "bankruptcy_restructuring",
    "bankruptcy_asset_realisation",
    "bankruptcy_estate",
    "bankruptcy_procedure_terminated",
    "bankruptcy_procedure_completed",
    "liquidation_decision",
    "liquidation_in_process",
    "planned_exclusion",
    "actual_exclusion",
)


class CompanyLegalEvent(Base):
    """Evidence-backed bankruptcy, liquidation, and exclusion event."""

    __tablename__ = "company_legal_events"
    __table_args__ = (
        UniqueConstraint(
            "source_code",
            "source_identifier",
            "event_type",
            "company_id",
            name="uq_company_legal_event_source",
        ),
        CheckConstraint(
            "event_type IN ("
            + ", ".join(f"'{value}'" for value in LEGAL_EVENT_TYPES)
            + ")",
            name="ck_company_legal_event_type",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dataset_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_sets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        index=True,
    )
    event_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )
    publication_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        index=True,
    )
    source_code: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        index=True,
    )
    source_identifier: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    source_url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    raw_event_type: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    evidence: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
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
