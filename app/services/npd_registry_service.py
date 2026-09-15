from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import DataSet, DataSource


DATASET_CODE = "fns_npd"


def ensure_npd_dataset():
    """
    Регистрирует точечную live-проверку НПД
    как отдельный dataset внутри источника ФНС.
    """

    session = get_session()

    try:
        source = (
            session.execute(
                select(DataSource)
                .where(
                    DataSource.code == "fns"
                )
                .limit(1)
            )
            .scalar_one_or_none()
        )

        if source is None:
            raise RuntimeError(
                "Источник fns не зарегистрирован"
            )

        values = {
            "source_id": source.id,
            "code": DATASET_CODE,
            "name": (
                "ФНС: Статус плательщика НПД"
            ),
            "domain": "npd_status",
            "update_mode": "api",
            "data_format": "json",
            "refresh_schedule": "on_demand",
            "priority": 10,
            "enabled": True,
            "source_url": (
                "https://npd.nalog.ru/check-status/"
            ),
            "description": (
                "Публичная точечная проверка статуса "
                "плательщика НПД по ИНН и дате. "
                "Не используется для bulk crawl."
            ),
        }

        statement = insert(DataSet).values(
            **values
        )

        statement = statement.on_conflict_do_update(
            index_elements=[DataSet.code],
            set_={
                key: value
                for key, value in values.items()
                if key != "code"
            },
        )

        session.execute(statement)
        session.commit()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()
