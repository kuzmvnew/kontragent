from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import DataSource


DEFAULT_SOURCES = [
    {
        "code": "fns",
        "name": "ФНС России",
        "source_type": "official",
        "priority": 10,
        "enabled": False,
        "website_url": "https://www.nalog.gov.ru/",
        "description": (
            "Официальные данные Федеральной "
            "налоговой службы России"
        ),
    },
    {
        "code": "girbo",
        "name": "ГИР БО",
        "source_type": "official",
        "priority": 20,
        "enabled": False,
        "website_url": "https://bo.nalog.gov.ru/",
        "description": (
            "Государственный информационный "
            "ресурс бухгалтерской отчетности"
        ),
    },
    {
        "code": "dadata",
        "name": "DaData",
        "source_type": "api",
        "priority": 50,
        "enabled": True,
        "website_url": "https://dadata.ru/",
        "description": (
            "API поиска организаций "
            "по ИНН и ОГРН"
        ),
    },
    {
        "code": "excel_import",
        "name": "Первичная Excel-выгрузка",
        "source_type": "import",
        "priority": 80,
        "enabled": True,
        "website_url": None,
        "description": (
            "Первичная база компаний, "
            "загруженная из Excel"
        ),
    },
]


def sync_default_sources():
    session = get_session()

    try:
        for source_data in DEFAULT_SOURCES:

            statement = insert(
                DataSource
            ).values(
                **source_data
            )

            statement = (
                statement.on_conflict_do_update(
                    index_elements=[
                        DataSource.code
                    ],
                    set_={
                        "name": source_data["name"],
                        "source_type": (
                            source_data[
                                "source_type"
                            ]
                        ),
                        "priority": (
                            source_data[
                                "priority"
                            ]
                        ),
                        "enabled": (
                            source_data[
                                "enabled"
                            ]
                        ),
                        "website_url": (
                            source_data[
                                "website_url"
                            ]
                        ),
                        "description": (
                            source_data[
                                "description"
                            ]
                        ),
                    },
                )
            )

            session.execute(
                statement
            )

        session.commit()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def get_source_by_code(code: str):
    session = get_session()

    try:
        statement = (
            select(DataSource)
            .where(
                DataSource.code == code
            )
        )

        return session.execute(
            statement
        ).scalar_one_or_none()

    finally:
        session.close()


def list_sources():
    session = get_session()

    try:
        statement = (
            select(DataSource)
            .order_by(
                DataSource.priority,
                DataSource.name,
            )
        )

        sources = (
            session.execute(
                statement
            )
            .scalars()
            .all()
        )

        return [
            {
                "id": source.id,
                "code": source.code,
                "name": source.name,
                "type": source.source_type,
                "priority": source.priority,
                "enabled": source.enabled,
            }
            for source in sources
        ]

    finally:
        session.close()