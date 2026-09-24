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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


# =========================================================
# DATA SOURCE
# =========================================================


class DataSource(Base):
    """
    Организация или система,
    из которой мы получаем данные.

    Примеры:

    fns
    girbo
    dadata
    excel_import

    ВАЖНО:

    DataSource — это не конкретный файл
    или набор данных.

    Например ФНС является одним source,
    но внутри неё может быть много datasets.
    """

    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )

    code: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    source_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )

    # Старый общий priority пока сохраняем,
    # чтобы не ломать Aggregator v1.
    #
    # Позже основным станет priority
    # конкретного dataset/domain.
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        index=True,
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
    )

    website_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    description: Mapped[str | None] = mapped_column(
        Text,
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


# =========================================================
# DATA SET
# =========================================================


class DataSet(Base):
    """
    Конкретный набор данных внутри source.

    Например:

    source:
        fns

    datasets:
        fns_egrul
        fns_egrip
        fns_msp
        fns_headcount
        fns_tax_paid
        fns_tax_debt
    """

    __tablename__ = "data_sets"

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )

    source_id: Mapped[int] = mapped_column(
        ForeignKey(
            "data_sources.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    code: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(
        String(250),
        nullable=False,
    )

    # Какой тип информации содержит dataset.
    #
    # registry
    # financials
    # headcount
    # taxes
    # tax_debt
    # procurement
    # enforcement
    # bankruptcy
    # ...
    domain: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )

    # Как получаем данные:
    #
    # bulk
    # delta
    # api
    # import
    update_mode: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
    )

    # Формат исходных данных:
    #
    # xml
    # json
    # csv
    # xlsx
    data_format: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    # Логическая периодичность.
    #
    # daily
    # monthly
    # annual
    # on_demand
    # manual
    refresh_schedule: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    # Приоритет именно этого набора
    # внутри своего domain.
    #
    # Чем меньше число,
    # тем выше приоритет.
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        index=True,
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
    )

    source_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Когда последний ingestion
    # закончился успешно.
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # За какую дату были исходные данные.
    last_data_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    # Operational metadata. ``last_data_date`` remains for compatibility;
    # ``source_as_of`` is the precise publisher timestamp used by readiness.
    dataset_kind: Mapped[str] = mapped_column(String(50), nullable=False, default="bulk_snapshot", index=True)
    freshness_policy: Mapped[str] = mapped_column(String(30), nullable=False, default="irregular", index=True)
    freshness_threshold_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Official publisher validity boundary.  This is deliberately separate
    # from our check/retrieval timestamps and from the source data date.
    official_actual_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    record_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    coverage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    operational_status: Mapped[str] = mapped_column(String(30), nullable=False, default="not_configured", index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    next_expected_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    auto_update_status: Mapped[str] = mapped_column(String(30), nullable=False, default="not_configured", index=True)

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


# =========================================================
# INGESTION RUN
# =========================================================


class IngestionRun(Base):
    """
    Журнал каждой загрузки dataset.

    Например:

    fns_headcount
    2026-09-13
    source_file = ...
    rows_read = 5 000 000
    rows_updated = 4 700 000
    status = success
    """

    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )

    dataset_id: Mapped[int] = mapped_column(
        ForeignKey(
            "data_sets.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="running",
        index=True,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    data_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    source_file_name: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    source_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # SHA-256 или другой checksum файла.
    #
    # Позже поможет не импортировать
    # один и тот же файл дважды.
    file_checksum: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    rows_read: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    rows_inserted: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    rows_updated: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    rows_skipped: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    errors_count: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Дополнительная техническая информация.
    details: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    run_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True, index=True)
    trigger: Mapped[str | None] = mapped_column(String(30), nullable=True)
    source_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    records_seen: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    records_written: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    records_rejected: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    duplicates: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    conflicts: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lock_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)


class DatasetUpdateLock(Base):
    """Cross-process lock; expired rows may be atomically reclaimed."""

    __tablename__ = "dataset_update_locks"

    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    owner: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class DatasetPublication(Base):
    """Immutable publication metadata; only one version is active per dataset."""

    __tablename__ = "dataset_publications"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version", name="uq_dataset_publication_version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    source_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    record_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


# =========================================================
# API / SOURCE SNAPSHOT
# =========================================================


class CompanySourceData(Base):
    """
    Текущий snapshot источника для компании.

    Пока эта таблица продолжает обслуживать
    наш Aggregator v1 и DaData.

    Bulk datasets позже будут писать
    информацию в специализированные
    domain-таблицы.
    """

    __tablename__ = "company_source_data"

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "source_id",
            name="uq_company_source_data",
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

    source_id: Mapped[int] = mapped_column(
        ForeignKey(
            "data_sources.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    normalized_payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )

    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="success",
        index=True,
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
