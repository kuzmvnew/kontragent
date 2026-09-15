from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class DisqualifiedPersonSnapshot(Base):
    """
    Запись Реестра дисквалифицированных лиц ФНС
    в конкретном опубликованном snapshot.

    Запись хранится независимо от наличия компании
    в master registry.

    organization_inn:
    ИНН организации, где лицо работало
    во время совершения правонарушения.
    Это не означает автоматически, что лицо
    является текущим руководителем организации.
    """

    __tablename__ = "disqualified_person_snapshots"

    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "data_date",
            "register_number",
            name=(
                "uq_disqualified_person_"
                "dataset_date_register"
            ),
        ),
    )

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

    data_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )

    register_number: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )

    full_name: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        index=True,
    )

    birth_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    birth_place: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    organization_name: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    organization_inn: Mapped[str | None] = mapped_column(
        String(12),
        nullable=True,
        index=True,
    )

    position: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    offence_article: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    protocol_authority: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    judge_name: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    judge_position: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    disqualification_term: Mapped[str | None] = mapped_column(
        String(100),
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
