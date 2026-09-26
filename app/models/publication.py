"""Durable HOME -> public VPS publication state."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
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


PUBLICATION_REQUEST_STATUSES = (
    "PENDING",
    "COALESCED",
    "BUILDING",
    "READY",
    "UPLOADING",
    "IMPORTING",
    "VERIFYING",
    "PUBLISHED",
    "RETRY_SCHEDULED",
    "FAILED",
    "SUPERSEDED",
)


class PublicProjectionPublication(Base):
    """Last successfully published semantic projection for one cohort company."""

    __tablename__ = "public_projection_publications"

    company_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("companies.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_published_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_published_release_id: Mapped[str] = mapped_column(
        String(120), nullable=False, index=True
    )
    last_enrichment_run_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("company_enrichment_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PublicPublicationRequest(Base):
    """Restart-safe release request and its publication state machine."""

    __tablename__ = "public_publication_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            + ",".join(f"'{value}'" for value in PUBLICATION_REQUEST_STATUSES)
            + ")",
            name="ck_public_publication_requests_status",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_public_publication_requests_attempts"
        ),
        CheckConstraint(
            "changed_company_count >= 0",
            name="ck_public_publication_requests_changed_count",
        ),
        Index(
            "ix_public_publication_requests_status_due",
            "status",
            "next_attempt_at",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    trigger_type: Mapped[str] = mapped_column(String(40), nullable=False)
    trigger_company_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    trigger_enrichment_run_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("company_enrichment_runs.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    projection_generation: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    projection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    candidate_release_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True, index=True
    )
    previous_release_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    published_release_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True, index=True
    )
    changed_company_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    changed_company_ids: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    changed_company_inns: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    change_summary: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_main_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    coalesced_into_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("public_publication_requests.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    build_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rollback_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
