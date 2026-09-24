from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


TAX_DEBT_FACT_CODE = "tax.debt.amount_as_of_date"


class FnsTaxDebtRawArtifact(Base):
    """Content-addressed immutable FNS debt ZIP used by one or more replays."""

    __tablename__ = "fns_tax_debt_raw_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "sha256", name="uq_fns_tax_debt_raw_artifact_checksum"
        ),
        CheckConstraint("size_bytes > 0", name="ck_fns_tax_debt_raw_artifact_size"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    first_worker_run_id: Mapped[UUID] = mapped_column(
        Uuid,
        nullable=False,
        index=True,
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    artifact_reference: Mapped[str] = mapped_column(Text, nullable=False)
    original_file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FnsTaxDebtPublicationGeneration(Base):
    """Immutable S02 publication metadata used for exact generation rollback."""

    __tablename__ = "fns_tax_debt_publication_generations"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "generation",
            name="uq_fns_tax_debt_publication_generation",
        ),
        UniqueConstraint(
            "dataset_id",
            "artifact_id",
            "publication_scope",
            name="uq_fns_tax_debt_publication_artifact",
        ),
        CheckConstraint("generation >= 0", name="ck_fns_tax_debt_generation_nonnegative"),
        CheckConstraint(
            "publication_scope IN ('baseline', 'pilot')",
            name="ck_fns_tax_debt_publication_scope",
        ),
        CheckConstraint(
            "status IN ('baseline', 'active', 'rollback', 'superseded')",
            name="ck_fns_tax_debt_generation_status",
        ),
        Index(
            "uq_fns_tax_debt_one_active_generation",
            "dataset_id",
            unique=True,
            postgresql_where=text("((status)::text = 'active'::text)"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("fns_tax_debt_raw_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    worker_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("worker_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    publication_scope: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    staging_pointer: Mapped[str] = mapped_column(Text, nullable=False)
    raw_pointer: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    official_actual_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_data_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    record_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    coverage: Mapped[dict] = mapped_column(JSONB, nullable=False)
    counters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    validation_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False)
    dataset_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class FnsTaxDebtPilotState(Base):
    """Single operational state row for S02 discovery, freshness and generations."""

    __tablename__ = "fns_tax_debt_pilot_state"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_fns_tax_debt_pilot_generation"),
        CheckConstraint(
            "normalized_generation >= 0 AND fact_generation >= 0 "
            "AND query_generation >= 0 AND baseline_generation >= 0",
            name="ck_fns_tax_debt_pilot_fact_query_generation",
        ),
        CheckConstraint(
            "freshness IN ('unknown', 'current', 'stale')",
            name="ck_fns_tax_debt_pilot_freshness",
        ),
    )

    source_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    dataset_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=True, unique=True
    )
    pilot_environment: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    cohort_inns: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    last_discovery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    discovered_artifact_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_xsd_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discovered_source_as_of: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    official_actual_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active_raw_pointer: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_source_as_of: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active_retrieved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    baseline_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    normalized_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    fact_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    query_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    rollback_fact_generation: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    rollback_normalized_generation: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    active_data_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    baseline_data_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    counters: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    freshness: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown", server_default=text("'unknown'")
    )
    errors: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FnsTaxDebtNormalizedRecord(Base):
    """Validated source record plus explicit exact-INN matching outcome."""

    __tablename__ = "fns_tax_debt_normalized_records"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "record_hash",
            name="uq_fns_tax_debt_normalized_artifact_record",
        ),
        CheckConstraint(
            "match_state IN ('matched', 'unmatched', 'conflict')",
            name="ck_fns_tax_debt_normalized_match_state",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("fns_tax_debt_raw_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_member: Mapped[str] = mapped_column(String(500), nullable=False)
    source_ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inn: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    company_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    total_arrears: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    total_penalties: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    total_fines: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    total_debt: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    items: Mapped[list] = mapped_column(JSONB, nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    normalization_version: Mapped[str] = mapped_column(String(40), nullable=False)
    validation_state: Mapped[str] = mapped_column(
        String(30), nullable=False, default="valid", server_default=text("'valid'")
    )
    match_state: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    match_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    limitation_states: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FnsTaxDebtQuarantineRecord(Base):
    """Invalid or contradictory source document excluded from normalization."""

    __tablename__ = "fns_tax_debt_quarantine_records"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "source_member",
            "source_ordinal",
            "raw_hash",
            name="uq_fns_tax_debt_quarantine_record",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("fns_tax_debt_raw_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_member: Mapped[str] = mapped_column(String(500), nullable=False)
    source_ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason_codes: Mapped[list] = mapped_column(JSONB, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompanyTaxDebtSnapshot(Base):
    """
    Исторический снимок налоговой задолженности.

    Одна строка =
    одна компания + один dataset + одна дата состояния.

    Например:
    ИНН 7722858778
    дата состояния 01.08.2026
    общий долг 855 846.65 ₽
    """

    __tablename__ = "company_tax_debt_snapshots"

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "dataset_id",
            "data_date",
            "publication_generation",
            name=(
                "uq_company_tax_debt_"
                "company_dataset_date_generation"
            ),
        ),
        CheckConstraint(
            f"fact_code::text = '{TAX_DEBT_FACT_CODE}'::text",
            name="ck_company_tax_debt_fact_code",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )

    company_id: Mapped[int] = mapped_column(
        ForeignKey(
            "companies.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    dataset_id: Mapped[int] = mapped_column(
        ForeignKey(
            "data_sets.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    normalized_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("fns_tax_debt_normalized_records.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )

    publication_generation: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
        index=True,
    )

    fact_code: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        default=TAX_DEBT_FACT_CODE,
        server_default=text(f"'{TAX_DEBT_FACT_CODE}'"),
        index=True,
    )

    data_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )

    document_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    source_document_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    total_arrears: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    total_penalties: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    total_fines: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    total_debt: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
        index=True,
    )

    item_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)

    provenance: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    limitation_states: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    retrieved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
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

    items: Mapped[list["CompanyTaxDebtItem"]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )


class CompanyTaxDebtItem(Base):
    """
    Детализация одного снимка задолженности.

    Например:

    НДС
        недоимка = 660 849.22

    Суммы пеней
        пени = 182 438.43
    """

    __tablename__ = "company_tax_debt_items"

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "tax_name",
            name=(
                "uq_company_tax_debt_"
                "snapshot_tax_name"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )

    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey(
            "company_tax_debt_snapshots.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    tax_name: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
        index=True,
    )

    arrears: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    penalties: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    fines: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
    )

    total: Mapped[Decimal] = mapped_column(
        Numeric(
            precision=20,
            scale=2,
        ),
        nullable=False,
        default=Decimal("0.00"),
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

    snapshot: Mapped["CompanyTaxDebtSnapshot"] = relationship(
        back_populates="items",
    )
