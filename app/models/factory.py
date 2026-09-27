"""Durable source/ruleset generations for restart-safe incremental backfill."""

from datetime import datetime
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


class FactoryGeneration(Base):
    """One source activation or Risk/Semantic ruleset generation."""

    __tablename__ = "factory_generations"
    __table_args__ = (
        CheckConstraint(
            "generation_type IN ('source','risk_ruleset','semantic_ruleset')",
            name="ck_factory_generations_type",
        ),
        CheckConstraint(
            "status IN ('pending','running','complete','failed')",
            name="ck_factory_generations_status",
        ),
        CheckConstraint(
            "selected_count >= 0 AND scheduled_count >= 0 "
            "AND completed_count >= 0 AND failed_count >= 0",
            name="ck_factory_generations_counts",
        ),
        Index("ix_factory_generations_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    generation_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    generation_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    source_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    dataset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=True, index=True
    )
    publication_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ruleset_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default=text("'pending'")
    )
    selection_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    cursor_company_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    selected_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    scheduled_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    completed_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    failed_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    metadata_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FactoryGenerationCompany(Base):
    """Idempotent company membership and completion state for a generation."""

    __tablename__ = "factory_generation_companies"
    __table_args__ = (
        UniqueConstraint(
            "generation_id", "company_id", name="uq_factory_generation_company"
        ),
        CheckConstraint(
            "status IN ('pending','scheduled','complete','failed','not_applicable')",
            name="ck_factory_generation_companies_status",
        ),
        Index("ix_factory_generation_companies_status", "generation_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    generation_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("factory_generations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrichment_run_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("company_enrichment_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    risk_assessment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    summary_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default=text("'pending'")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
