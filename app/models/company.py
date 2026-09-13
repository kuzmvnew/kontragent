from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )

    inn: Mapped[str] = mapped_column(
        String(12),
        unique=True,
        nullable=False,
        index=True,
    )

    kpp: Mapped[str | None] = mapped_column(
        String(9),
        nullable=True,
        index=True,
    )

    ogrn: Mapped[str | None] = mapped_column(
        String(15),
        unique=True,
        nullable=True,
        index=True,
    )

    okpo: Mapped[str | None] = mapped_column(
        String(12),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        index=True,
    )

    short_name: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    full_name: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    address: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    activity: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    okved: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        index=True,
    )

    website: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    status: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        index=True,
    )

    source: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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

    managers: Mapped[list["CompanyManager"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )

    contacts: Mapped[list["CompanyContact"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )

    financials: Mapped[list["CompanyFinancial"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )

    identifiers: Mapped[list["CompanyIdentifier"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )

    branches: Mapped[list["CompanyBranch"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )


class CompanyManager(Base):
    __tablename__ = "company_managers"

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

    last_name: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    first_name: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    middle_name: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    full_name: Mapped[str | None] = mapped_column(
        String(600),
        nullable=True,
        index=True,
    )

    position: Mapped[str | None] = mapped_column(
        String(300),
        nullable=True,
    )

    is_current: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    source: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    company: Mapped["Company"] = relationship(
        back_populates="managers",
    )


class CompanyContact(Base):
    __tablename__ = "company_contacts"

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "contact_type",
            "value",
            name="uq_company_contact",
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

    contact_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
    )

    value: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    source: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    company: Mapped["Company"] = relationship(
        back_populates="contacts",
    )


class CompanyFinancial(Base):
    __tablename__ = "company_financials"

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

    year: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )

    revenue: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    company_value: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    employee_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    source: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    company: Mapped["Company"] = relationship(
        back_populates="financials",
    )


class CompanyIdentifier(Base):
    __tablename__ = "company_identifiers"

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "identifier_type",
            "value",
            name="uq_company_identifier",
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

    identifier_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )

    value: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    source: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    company: Mapped["Company"] = relationship(
        back_populates="identifiers",
    )


class CompanyBranch(Base):
    __tablename__ = "company_branches"

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "branch_code",
            "address",
            name="uq_company_branch",
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

    branch_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    name: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    address: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    kpp: Mapped[str | None] = mapped_column(
        String(9),
        nullable=True,
    )

    source: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    company: Mapped["Company"] = relationship(
        back_populates="branches",
    )