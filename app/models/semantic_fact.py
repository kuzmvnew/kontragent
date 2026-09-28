"""Persisted semantic fact identity and selected evidence."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CompanySemanticFact(Base):
    __tablename__ = "company_semantic_facts"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "section_key",
            "field_key",
            "period_identity",
            "item_identity",
            name="uq_company_semantic_fact_coordinate",
        ),
        CheckConstraint(
            "state IN ('FOUND','NOT_FOUND','NOT_APPLICABLE','NOT_CHECKED',"
            "'SOURCE_UNAVAILABLE','TIMEOUT','PARSING_ERROR','STALE_DATA','UNKNOWN','PARTIAL',"
            "'CONFLICTING_EVIDENCE')",
            name="ck_company_semantic_fact_state",
        ),
        CheckConstraint(
            "rights IN ('PUBLIC','AUTHENTICATED_ONLY','INTERNAL_ONLY')",
            name="ck_company_semantic_fact_rights",
        ),
    )

    fact_ref: Mapped[str] = mapped_column(String(41), primary_key=True)
    item_ref: Mapped[str] = mapped_column(String(41), nullable=False, index=True)
    company_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    field_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    period_identity: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=text("''")
    )
    item_identity: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    selected_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    alternative_evidence: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    evidence_history: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    state: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    rights: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"), index=True
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
