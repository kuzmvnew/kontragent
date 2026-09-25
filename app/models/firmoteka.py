"""Persistent, restart-safe state for the authorized Firmoteka bridge."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class FirmotekaCrawlRun(Base):
    __tablename__ = "firmoteka_crawl_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','paused','completed','failed')",
            name="ck_firmoteka_crawl_run_status",
        ),
        CheckConstraint(
            "phase IN ('discovery','catalog','companies','daily_refresh','complete')",
            name="ck_firmoteka_crawl_run_phase",
        ),
        CheckConstraint("request_delay_seconds >= 4", name="ck_firmoteka_delay"),
        CheckConstraint("concurrency = 1", name="ck_firmoteka_concurrency"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_kind: Mapped[str] = mapped_column(String(30), nullable=False, default="initial")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running", index=True)
    phase: Mapped[str] = mapped_column(String(30), nullable=False, default="discovery", index=True)
    cursor: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    request_delay_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=4, server_default=text("4"))
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    success_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    failure_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    retry_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    catalog_discovered: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    company_discovered: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    fetched_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    parsed_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    valid_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    new_master_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    updated_master_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    legal_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    ip_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    quarantine_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    captcha_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    raw_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    http_status_counts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class FirmotekaCatalogPage(Base):
    __tablename__ = "firmoteka_catalog_pages"
    __table_args__ = (
        UniqueConstraint("crawl_run_id", "url_hash", name="uq_firmoteka_catalog_run_url"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed')",
            name="ck_firmoteka_catalog_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    crawl_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("firmoteka_crawl_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    discovered_companies: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FirmotekaCrawlItem(Base):
    __tablename__ = "firmoteka_crawl_items"
    __table_args__ = (
        UniqueConstraint("crawl_run_id", "inn", name="uq_firmoteka_crawl_run_inn"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','quarantined','failed')",
            name="ck_firmoteka_item_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    crawl_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("firmoteka_crawl_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    inn: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    discovered_from: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    raw_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FirmotekaRawArtifact(Base):
    __tablename__ = "firmoteka_raw_artifacts"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_headers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FirmotekaCompanySnapshot(Base):
    __tablename__ = "firmoteka_company_snapshots"
    __table_args__ = (
        UniqueConstraint("inn", "content_hash", name="uq_firmoteka_snapshot_content"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    dataset_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    inn: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    ogrn: Mapped[str | None] = mapped_column(String(15), index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    source_as_of: Mapped[date | None] = mapped_column(Date, index=True)
    raw_sha256: Mapped[str] = mapped_column(String(64), ForeignKey("firmoteka_raw_artifacts.sha256", ondelete="RESTRICT"), nullable=False, index=True)
    normalized_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    normalized_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    projection: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FirmotekaQuarantineRecord(Base):
    __tablename__ = "firmoteka_quarantine_records"
    __table_args__ = (
        UniqueConstraint("crawl_run_id", "source_url", "reason", name="uq_firmoteka_quarantine_evidence"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    crawl_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("firmoteka_crawl_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_inn: Mapped[str | None] = mapped_column(String(12), index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    raw_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    safe_details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
