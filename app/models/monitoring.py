"""Source-neutral Monitoring P0 persistence.

Snapshots and events are canonical company-level objects.  Subscriptions and
feed entries are workspace-owned delivery objects.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CompanyMonitoringSnapshot(Base):
    """Immutable provider-independent semantic state for one company."""

    __tablename__ = "company_monitoring_snapshots"
    __table_args__ = (
        Index(
            "ix_company_monitoring_snapshots_company_captured",
            "company_id",
            "captured_at",
        ),
        Index(
            "ix_company_monitoring_snapshots_company_revision",
            "company_id",
            "company_view_revision",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_view_revision: Mapped[str | None] = mapped_column(String(80), nullable=True)
    risk_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    summary_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    facts: Mapped[list] = mapped_column(JSONB, nullable=False)
    last_known_business_facts: Mapped[list] = mapped_column(JSONB, nullable=False)
    fact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MonitoringSubscription(Base):
    """Workspace-owned lifecycle and scan cursor for a saved company."""

    __tablename__ = "monitoring_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "company_id",
            name="uq_monitoring_subscription_workspace_company",
        ),
        UniqueConstraint(
            "id",
            "workspace_id",
            name="uq_monitoring_subscription_workspace_scope",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','PAUSED')",
            name="ck_monitoring_subscription_status",
        ),
        Index(
            "ix_monitoring_subscriptions_workspace_status",
            "workspace_id",
            "status",
        ),
        Index(
            "ix_monitoring_subscriptions_company_status",
            "company_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("customer_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    baseline_snapshot_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("company_monitoring_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class MonitoringEvent(Base):
    """Canonical company-level semantic change, independent of tenants."""

    __tablename__ = "monitoring_events"
    __table_args__ = (
        CheckConstraint(
            "origin IN ('SOURCE_CHANGE','RULESET_CHANGE','COVERAGE_CHANGE','DEAL_CONTEXT_CHANGE')",
            name="ck_monitoring_event_origin",
        ),
        CheckConstraint(
            "change_kind IN ('FACT_ADDED','FACT_CHANGED','FACT_REMOVED','STATE_CHANGED')",
            name="ck_monitoring_event_change_kind",
        ),
        CheckConstraint(
            "severity IN ('INFO','LOW','MEDIUM','HIGH')",
            name="ck_monitoring_event_severity",
        ),
        Index(
            "ix_monitoring_events_company_detected",
            "company_id",
            "detected_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event_ref: Mapped[str] = mapped_column(String(42), nullable=False, unique=True)
    company_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fact_ref: Mapped[str | None] = mapped_column(String(41), nullable=True)
    origin: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    change_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    section_key: Mapped[str] = mapped_column(String(80), nullable=False)
    field_key: Mapped[str] = mapped_column(String(120), nullable=False)
    period_identity: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=text("''")
    )
    item_identity: Mapped[str] = mapped_column(
        String(300), nullable=False, default="", server_default=text("''")
    )
    source_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    old_value: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    old_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    new_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    severity_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    source_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    evidence_refs: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkspaceFeedEntry(Base):
    """Tenant-scoped delivery pointer for one canonical monitoring event."""

    __tablename__ = "workspace_feed_entries"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "event_id",
            name="uq_workspace_feed_subscription_event",
        ),
        ForeignKeyConstraint(
            ("subscription_id", "workspace_id"),
            ("monitoring_subscriptions.id", "monitoring_subscriptions.workspace_id"),
            name="fk_workspace_feed_subscription_scope",
            ondelete="CASCADE",
        ),
        Index(
            "ix_workspace_feed_workspace_created",
            "workspace_id",
            "created_at",
        ),
        Index(
            "ix_workspace_feed_workspace_unread",
            "workspace_id",
            "created_at",
            postgresql_where=text("(read_at IS NULL)"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscription_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        index=True,
    )
    event_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("monitoring_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _immutable(_mapper, _connection, target) -> None:
    raise ValueError(f"{target.__class__.__name__} rows are immutable")


event.listen(CompanyMonitoringSnapshot, "before_update", _immutable)
event.listen(MonitoringEvent, "before_update", _immutable)
