"""Private source-operations persistence.

The admin console stores only operational metadata.  Credentials, environment
values and source payloads must never be copied into these tables.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, String, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class SourceChangeSummary(Base):
    """Canonical, nullable change counters for one successful worker run."""

    __tablename__ = "source_change_summaries"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("worker_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    matched_companies: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    new_facts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    changed_facts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    removed_or_expired_facts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    unchanged_facts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    replayed_facts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quarantined_records: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_records: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_data_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    previous_source_data_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class AdminActionAudit(Base):
    """Owner actions and their safe before/after state."""

    __tablename__ = "admin_action_audit"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    job_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    previous_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    new_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    result: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(
        String(80), nullable=False, default="local_owner", server_default=text("'local_owner'")
    )
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
