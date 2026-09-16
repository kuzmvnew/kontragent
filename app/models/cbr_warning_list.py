from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
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


class CbrWarningListEntry(Base):
    """
    Текущая запись официального предупредительного списка Банка России.

    Таблица хранит только последний успешно опубликованный snapshot,
    загруженный из официального JSON для автоматизированных систем.
    Сопоставление с Master Registry выполняется только по точному ИНН.
    """

    __tablename__ = "cbr_warning_list_entries"

    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "cbr_id",
            name="uq_cbr_warning_list_dataset_cbr_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )

    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("data_sets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    data_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )

    cbr_id: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        index=True,
    )

    inn: Mapped[str | None] = mapped_column(
        String(12),
        nullable=True,
        index=True,
    )

    name: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    entry_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )

    update_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )

    address: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    sites: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    signs: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    regions: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    additional_info: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    liquidation_status: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    comment: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    org_type: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    raw_payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
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
