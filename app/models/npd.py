from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class NpdStatusCheck(Base):
    """
    Результат точечной проверки статуса НПД
    через официальный публичный API ФНС.

    Таблица не является bulk-зеркалом реестра.
    Одна строка хранит последний результат нашей
    проверки конкретного ИНН на конкретную дату.
    """

    __tablename__ = "npd_status_checks"

    __table_args__ = (
        UniqueConstraint(
            "inn",
            "request_date",
            name="uq_npd_status_inn_request_date",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
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

    is_npd: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )

    message: Mapped[str | None] = mapped_column(
        Text,
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
