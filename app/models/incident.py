"""Durable incident-autopilot state for HOME DATA WORKER."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


INCIDENT_CATEGORIES = (
    "TEMPORARY_NETWORK", "HTTP_5XX", "TIMEOUT", "RATE_LIMIT", "DNS_FAILURE",
    "SOURCE_UNAVAILABLE", "SOURCE_STALE", "SOURCE_INVALID_ARTIFACT",
    "SOURCE_SCHEMA_VIOLATION", "SOURCE_ACCESS_REQUIRED", "CHECKSUM_MISMATCH",
    "RAW_STORAGE_ERROR", "DISK_PRESSURE", "PARSER_ERROR", "NORMALIZATION_ERROR",
    "MATCHING_ERROR", "PUBLICATION_ERROR", "DATABASE_UNAVAILABLE", "DATABASE_LOCK",
    "LEASE_STUCK", "WORKER_CRASH", "WORKER_NOT_RUNNING", "CONFIGURATION_ERROR",
    "CODE_REGRESSION", "UNKNOWN",
)
OWNER_DOMAINS = ("SOURCE_OWNED", "OUR_INFRASTRUCTURE", "OUR_CODE", "ACCESS_REQUIRED", "UNKNOWN")
INCIDENT_STATUSES = (
    "OPEN", "RETRY_SCHEDULED", "AUTO_HEAL_RUNNING", "WAITING_SOURCE",
    "AWAITING_ENGINEERING_REVIEW", "REVIEW_REQUIRED", "AGENT_UNAVAILABLE",
    "AUTO_REPAIR_EXHAUSTED", "RESOLVED", "CANCELLED",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class SourceIncident(Base):
    __tablename__ = "source_incidents"
    __table_args__ = (
        CheckConstraint(f"category IN ({_quoted(INCIDENT_CATEGORIES)})", name="ck_source_incidents_category"),
        CheckConstraint(f"owner_domain IN ({_quoted(OWNER_DOMAINS)})", name="ck_source_incidents_owner_domain"),
        CheckConstraint(f"status IN ({_quoted(INCIDENT_STATUSES)})", name="ck_source_incidents_status"),
        CheckConstraint("remediation_level >= 0 AND remediation_level <= 3", name="ck_source_incidents_level"),
        CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_source_incidents_attempts"),
        Index(
            "uq_source_incidents_active_dedup",
            "dedup_key",
            unique=True,
            postgresql_where=text("((status)::text <> ALL ((ARRAY['RESOLVED'::character varying, 'CANCELLED'::character varying])::text[]))"),
        ),
        Index(
            "uq_source_incidents_active_engineering_source",
            "source_id",
            unique=True,
            postgresql_where=text(
                "(((status)::text = ANY ((ARRAY['AWAITING_ENGINEERING_REVIEW'::character varying, "
                "'REVIEW_REQUIRED'::character varying, 'AGENT_UNAVAILABLE'::character varying])::text[])) "
                "AND (repair_branch IS NOT NULL))"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    incident_code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    source_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    dataset_code: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("worker_runs.id", ondelete="SET NULL"), nullable=True)
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("worker_jobs.id", ondelete="SET NULL"), nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="MEDIUM")
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    owner_domain: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="OPEN", index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    auto_heal_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    auto_code_repair_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    remediation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default=text("2"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default=text("3"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_action: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_action_result: Mapped[str | None] = mapped_column(String(40), nullable=True)
    repair_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    repair_pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repair_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repair_ci_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


class SourceIncidentAction(Base):
    __tablename__ = "source_incident_actions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("source_incidents.id", ondelete="CASCADE"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[str] = mapped_column(String(40), nullable=False, default="STARTED")
    safe_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_safe: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


class SourceAutomationPolicy(Base):
    __tablename__ = "source_automation_policies"
    __table_args__ = (
        CheckConstraint("remediation_level >= 0 AND remediation_level <= 3", name="ck_source_automation_level"),
        CheckConstraint("max_attempts >= 1 AND max_attempts <= 10", name="ck_source_automation_attempts"),
        CheckConstraint("cooldown_seconds = ANY (ARRAY[60, 300, 900, 3600, 21600, 86400])", name="ck_source_automation_cooldown"),
    )

    source_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    dataset_code: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    auto_heal_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    auto_code_repair_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    remediation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default=text("2"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default=text("3"))
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600, server_default=text("3600"))
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
