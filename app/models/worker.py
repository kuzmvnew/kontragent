"""Durable V1 worker-foundation persistence models.

These tables are deliberately isolated from the existing source-specific
ingestion tables.  Nothing imports or schedules this subsystem from the
production application yet.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class WorkerJob(Base):
    """Durable unit of work; an idempotency key prevents duplicate jobs."""

    __tablename__ = "worker_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_scheduled', "
            "'succeeded', 'failed', 'cancelled')",
            name="ck_worker_jobs_status",
        ),
        CheckConstraint("max_attempts > 0", name="ck_worker_jobs_max_attempts"),
        CheckConstraint("timeout_seconds > 0", name="ck_worker_jobs_timeout"),
        Index("ix_worker_jobs_claim", "status", "next_attempt_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    handler_version: Mapped[str] = mapped_column(String(80), nullable=False)
    schedule_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="queued", server_default=text("'queued'")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300, server_default=text("300")
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class WorkerRun(Base):
    """One immutable-attempt identity with mutable execution state."""

    __tablename__ = "worker_runs"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no", name="uq_worker_runs_attempt"),
        CheckConstraint("attempt_no > 0", name="ck_worker_runs_attempt"),
        CheckConstraint("fencing_token > 0", name="ck_worker_runs_fencing_token"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'timed_out', "
            "'interrupted')",
            name="ck_worker_runs_status",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_worker_runs_duration",
        ),
        Index("ix_worker_runs_stale", "status", "heartbeat_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("worker_jobs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="running", server_default=text("'running'")
    )
    worker_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    handler_version: Mapped[str] = mapped_column(String(80), nullable=False)
    current_stage: Mapped[str] = mapped_column(
        String(50), nullable=False, default="claimed", server_default=text("'claimed'")
    )
    errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    checksum_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    retryable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class WorkerLease(Base):
    """Database-authoritative source lease with a monotonically increasing fence."""

    __tablename__ = "worker_leases"
    __table_args__ = (
        CheckConstraint("fencing_token > 0", name="ck_worker_leases_fencing_token"),
        Index("ix_worker_leases_expiry", "expires_at"),
    )

    source_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    owner_worker_id: Mapped[str] = mapped_column(
        String(200), nullable=False, index=True
    )
    fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default=text("1")
    )
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class WorkerHandlerRegistration(Base):
    """Audit record for an explicitly approved, non-live handler version."""

    __tablename__ = "worker_handler_registry"
    __table_args__ = (
        CheckConstraint("live_mode = false", name="ck_worker_handlers_not_live"),
    )

    source_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    handler_version: Mapped[str] = mapped_column(String(80), primary_key=True)
    approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    live_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkerRawManifest(Base):
    """Immutable metadata reference; artifact bytes live behind a future adapter."""

    __tablename__ = "worker_raw_manifests"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "artifact_reference", name="uq_worker_raw_manifest_reference"
        ),
        CheckConstraint("immutable = true", name="ck_worker_raw_manifest_immutable"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("worker_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    artifact_reference: Mapped[str] = mapped_column(Text, nullable=False)
    checksum_algorithm: Mapped[str] = mapped_column(
        String(30), nullable=False, default="sha256", server_default=text("'sha256'")
    )
    checksum: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    immutable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkerPublicationState(Base):
    """Atomic source pointer plus the immediately recoverable previous pointer."""

    __tablename__ = "worker_publication_state"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_worker_publication_generation"),
        CheckConstraint(
            "last_fencing_token >= 0", name="ck_worker_publication_fencing_token"
        ),
    )

    source_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    active_pointer: Mapped[str | None] = mapped_column(Text, nullable=True)
    rollback_pointer: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    last_fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    published_by_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("worker_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    validation_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
