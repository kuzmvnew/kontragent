from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
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


# =========================================================
# COMPANY
# =========================================================


class Company(Base):
    """
    Master entity контрагента.

    Одна строка = одно юридическое лицо
    или один индивидуальный предприниматель.

    Главный идентификатор дедупликации:
    ИНН.
    """

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )

    # -----------------------------------------------------
    # IDENTIFIERS
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # ENTITY TYPE
    # -----------------------------------------------------

    # legal
    # individual_entrepreneur
    #
    # Пока nullable=True, потому что
    # у нас уже есть 9 799 старых записей.
    # После backfill сможем сделать строже.
    entity_type: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        index=True,
    )

    # -----------------------------------------------------
    # NAMES
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # REGISTRATION
    # -----------------------------------------------------

    status: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        index=True,
    )

    registration_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )

    termination_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )

    # -----------------------------------------------------
    # ADDRESS
    # -----------------------------------------------------

    address: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Код субъекта РФ.
    #
    # Храним строкой, потому что
    # возможны ведущие нули.
    region_code: Mapped[str | None] = mapped_column(
        String(3),
        nullable=True,
        index=True,
    )

    # -----------------------------------------------------
    # ACTIVITY
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # MASTER REGISTRY
    # -----------------------------------------------------

    # Dataset, который сейчас является
    # master-источником регистрационных данных.
    #
    # Например:
    #
    # fns_msp
    # fns_egrul
    # fns_egrip
    master_dataset_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "data_sets.id",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )

    # Дата состояния master dataset.
    master_data_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )

    # -----------------------------------------------------
    # LEGACY / COMPATIBILITY SOURCE
    # -----------------------------------------------------

    # Поле пока сохраняем,
    # чтобы не сломать Aggregator v1
    # и существующий Excel importer.
    source: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # -----------------------------------------------------
    # TIMESTAMPS
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # RELATIONSHIPS
    # -----------------------------------------------------

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


# =========================================================
# MANAGER
# =========================================================


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


# =========================================================
# CONTACT
# =========================================================


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


# =========================================================
# FINANCIAL
# =========================================================


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


# =========================================================
# IDENTIFIER
# =========================================================


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


# =========================================================
# BRANCH
# =========================================================


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