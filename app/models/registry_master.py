from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class RegistrySourceCheckpoint(Base):
    """Durable full/delta position for an official master registry."""

    __tablename__ = "registry_source_checkpoints"

    source_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    format_version: Mapped[str] = mapped_column(String(20), nullable=False)
    baseline_accepted: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    last_full_date: Mapped[date | None] = mapped_column(Date)
    last_delta_date: Mapped[date | None] = mapped_column(Date)
    last_release_identity: Mapped[str | None] = mapped_column(String(200))
    cursor: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CompanyRegistryChange(Base):
    """Auditable semantic change emitted by EGRUL/EGRIP publication."""

    __tablename__ = "company_registry_changes"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created','identity_changed','status_changed','address_changed','leader_changed','okved_changed','terminated')",
            name="ck_company_registry_change_event_type",
        ),
        UniqueConstraint(
            "source_id", "source_record_key", "event_type",
            name="uq_company_registry_change_source_record_event",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("worker_runs.id", ondelete="SET NULL"), index=True
    )
    inn: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    changed_fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    source_data_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source_record_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MasterReplaySignal(Base):
    """Durable request to reproject an accepted source for a Master change."""

    __tablename__ = "master_replay_signals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','scheduled','complete','failed')",
            name="ck_master_replay_signal_status",
        ),
        UniqueConstraint(
            "company_id", "target_source_id", "registry_change_id",
            name="uq_master_replay_signal_company_source_change",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_source_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    registry_change_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("company_registry_changes.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default=text("'pending'"), index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
