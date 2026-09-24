from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


MINTRANS_TED_FACT_CODE = "transport_forwarding.registry_listing"


class MintransTedRawArtifact(Base):
    """Content-addressed metadata for an immutable fixture XLSX."""

    __tablename__ = "mintrans_ted_raw_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "sha256",
            name="uq_mintrans_ted_raw_artifact_checksum",
        ),
        CheckConstraint("size_bytes > 0", name="ck_mintrans_ted_raw_artifact_size"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    original_file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MintransTedEntry(Base):
    """Current normalized Mintrans freight-forwarder registry row."""

    __tablename__ = "mintrans_ted_entries"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "row_hash", name="uq_mintrans_ted_entry_row_hash"
        ),
        CheckConstraint(
            "match_state IN ('matched', 'unmatched', 'conflict_identity')",
            name="ck_mintrans_ted_entry_match_state",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("mintrans_ted_raw_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_row_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inn: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    ogrn: Mapped[str | None] = mapped_column(String(15), nullable=True, index=True)
    registry_number: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    included_at: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    validation_state: Mapped[str] = mapped_column(String(30), nullable=False)
    match_state: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    match_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MintransTedQuarantineRow(Base):
    """Invalid source row kept separate from normalized and projected facts."""

    __tablename__ = "mintrans_ted_quarantine_rows"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "source_row_number",
            "raw_hash",
            name="uq_mintrans_ted_quarantine_row",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("mintrans_ted_raw_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_row_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason_codes: Mapped[list] = mapped_column(JSONB, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TransportForwardingRegistryListing(Base):
    """Canonical company fact projected only from an exact identity match."""

    __tablename__ = "transport_forwarding_registry_listings"
    __table_args__ = (
        UniqueConstraint(
            "source_entry_id",
            name="uq_transport_forwarding_registry_listing_entry",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_entry_id: Mapped[int] = mapped_column(
        ForeignKey("mintrans_ted_entries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fact_code: Mapped[str] = mapped_column(
        String(120), nullable=False, default=MINTRANS_TED_FACT_CODE, index=True
    )
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
