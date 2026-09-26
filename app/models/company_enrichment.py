"""Durable per-company enrichment workflow state."""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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


class CompanyEnrichmentRun(Base):
    """One restartable company workflow with a frozen source denominator."""

    __tablename__ = "company_enrichment_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','waiting_sources','retry_scheduled',"
            "'running','succeeded','failed','cancelled')",
            name="ck_company_enrichment_runs_status",
        ),
        CheckConstraint(
            "stage IN ('planning','source_enrichment','risk','summary',"
            "'complete','failed')",
            name="ck_company_enrichment_runs_stage",
        ),
        CheckConstraint(
            "source_count >= 0 AND completed_source_count >= 0 "
            "AND failed_source_count >= 0",
            name="ck_company_enrichment_runs_counts",
        ),
        CheckConstraint(
            "(completed_source_count + failed_source_count) <= source_count",
            name="ck_company_enrichment_runs_count_bounds",
        ),
        CheckConstraint(
            "restart_count >= 0 AND max_restarts >= 0 "
            "AND restart_count <= max_restarts",
            name="ck_company_enrichment_runs_restarts",
        ),
        Index(
            "ix_company_enrichment_runs_company_created",
            "company_id",
            "created_at",
        ),
        Index(
            "ix_company_enrichment_runs_status_updated",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    workflow_version: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="company-enrichment-v1",
        server_default=text("'company-enrichment-v1'"),
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default=text("'pending'")
    )
    stage: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="planning",
        server_default=text("'planning'"),
    )
    applicable_sources: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    source_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    completed_source_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    failed_source_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    restart_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_restarts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default=text("2")
    )
    risk_assessment_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    summary_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    public_ready: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompanySourceCoverage(Base):
    """Frozen source expectation and the Worker job satisfying it."""

    __tablename__ = "company_source_coverage"
    __table_args__ = (
        UniqueConstraint(
            "enrichment_run_id",
            "source_id",
            name="uq_company_source_coverage_run_source",
        ),
        CheckConstraint(
            "mode IN ('local_bulk_replay','local_snapshot_lookup','point_check')",
            name="ck_company_source_coverage_mode",
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','FOUND','NOT_FOUND','NOT_APPLICABLE',"
            "'SOURCE_UNAVAILABLE','TIMEOUT','PARSING_ERROR','STALE_DATA',"
            "'ACCESS_REQUIRED')",
            name="ck_company_source_coverage_status",
        ),
        CheckConstraint(
            "execution_status IN ('pending','queued','running','retry_scheduled',"
            "'succeeded','failed','cancelled')",
            name="ck_company_source_coverage_execution_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_company_source_coverage_attempts",
        ),
        Index(
            "ix_company_source_coverage_company_source",
            "company_id",
            "source_id",
        ),
        Index(
            "ix_company_source_coverage_status_updated",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    enrichment_run_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("company_enrichment_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dataset_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("data_sets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    worker_source_id: Mapped[str] = mapped_column(
        String(120), nullable=False, index=True
    )
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    execution_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default=text("'pending'")
    )
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    handler_version: Mapped[str] = mapped_column(String(80), nullable=False)
    worker_job_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("worker_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    worker_run_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("worker_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    publication_generation: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    source_data_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    replay_pointer: Mapped[str | None] = mapped_column(Text, nullable=True)
    replay_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    master_replay_signal_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    fact_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
