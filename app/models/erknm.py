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


class ErknmInspection(Base):
    """
    Нормализованное текущее состояние записи ФГИС ЕРКНМ (248-ФЗ).

    Одна строка соответствует одному ERPID. Повторная публикация того же
    мероприятия обновляет строку через UPSERT, а ingestion history хранит
    происхождение конкретного импортированного файла.

    Связь с Master Registry выполняется только по subject_inn/subject_ogrn.
    Название субъекта не используется как автоматический ключ сопоставления.
    """

    __tablename__ = "erknm_inspections"

    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "erpid",
            name="uq_erknm_inspections_dataset_erpid",
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

    period_year: Mapped[int] = mapped_column(
        nullable=False,
        index=True,
    )

    period_month: Mapped[int] = mapped_column(
        nullable=False,
        index=True,
    )

    erpid: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    classification: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    creation_source: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    status: Mapped[str | None] = mapped_column(
        String(300),
        nullable=True,
        index=True,
    )

    status_key: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    control_level: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    supervision_name: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    start_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )

    end_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )

    prosecutor_office: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind_control: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind_knm: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    kno_organization: Mapped[str | None] = mapped_column(Text, nullable=True)

    subject_inn: Mapped[str | None] = mapped_column(
        String(12),
        nullable=True,
        index=True,
    )
    subject_ogrn: Mapped[str | None] = mapped_column(
        String(15),
        nullable=True,
        index=True,
    )
    subject_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_type: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )
    subject_guid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    msp_code: Mapped[str | None] = mapped_column(String(300), nullable=True)

    place: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_sub_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_category: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    inspection_attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    subject_attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    okveds: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    objects: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    inspectors: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    reasons: Mapped[list | None] = mapped_column(JSONB, nullable=True)

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
