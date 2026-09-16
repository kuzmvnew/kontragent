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


class CbrFinorgCheck(Base):
    """
    Кэш точечной проверки ИНН через официальный веб-сервис
    Банка России «Участники финансового рынка».

    Сопоставление выполняется только по точному ИНН.
    Название, адрес, сайты и другие поля не используются
    как доказательство связи записи с компанией или ИП.
    """

    __tablename__ = "cbr_finorg_checks"

    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "inn",
            "request_date",
            name="uq_cbr_finorg_dataset_inn_request_date",
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

    inn: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        index=True,
    )

    request_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )

    # success / error
    result_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
    )

    is_participant: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        index=True,
    )

    cbr_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
    )

    ogrn: Mapped[str | None] = mapped_column(
        String(15),
        nullable=True,
        index=True,
    )

    short_name: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    name: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    status: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        index=True,
    )

    fo_types: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    licenses: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    payment_systems: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    mfo_history: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    websites: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    address: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    phones: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    email: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    region: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    regnum: Mapped[str | None] = mapped_column(
        String(80),
        nullable=True,
    )

    bic: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    is_sro_member: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )

    has_branches: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )

    registration_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    http_status: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    error_code: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        index=True,
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    raw_payload: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
