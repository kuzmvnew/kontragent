from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import DataSet, DataSource


SOURCE_CODE = "genproc"
DATASET_CODE = "erknm_inspections"


def build_erknm_source_spec() -> dict:
    return {
        "code": SOURCE_CODE,
        "name": "Генеральная прокуратура РФ",
        "source_type": "official",
        "priority": 10,
        "enabled": True,
        "website_url": "https://proverki.gov.ru/",
        "description": (
            "Официальные открытые данные ФГИС ЕРКНМ "
            "Генеральной прокуратуры Российской Федерации"
        ),
    }


def build_erknm_dataset_spec(source_id: int) -> dict:
    return {
        "source_id": source_id,
        "code": DATASET_CODE,
        "name": "ФГИС ЕРКНМ: контрольные мероприятия по 248-ФЗ",
        "domain": "inspections",
        "update_mode": "bulk",
        "data_format": "xml",
        "refresh_schedule": "daily",
        "priority": 10,
        "enabled": True,
        "source_url": (
            "https://proverki.gov.ru/portal/public-open-data"
        ),
        "description": (
            "Официальные месячные XML-наборы ФГИС ЕРКНМ по 248-ФЗ. "
            "Актуальная версия определяется через metadata XML; "
            "bulk-файлы и XSD скачиваются только с официального "
            "пути proverki.gov.ru/blob/erknm-opendata."
        ),
    }


def ensure_erknm_dataset() -> None:
    """Регистрирует Генпрокуратуру и набор ФГИС ЕРКНМ в Source Registry."""

    session = get_session()

    try:
        source_values = build_erknm_source_spec()

        source_statement = insert(DataSource).values(
            **source_values
        )
        source_statement = source_statement.on_conflict_do_update(
            index_elements=[DataSource.code],
            set_={
                key: value
                for key, value in source_values.items()
                if key != "code"
            },
        )
        session.execute(source_statement)
        session.flush()

        source_id = (
            session.execute(
                select(DataSource.id)
                .where(DataSource.code == SOURCE_CODE)
                .limit(1)
            )
            .scalar_one()
        )

        dataset_values = build_erknm_dataset_spec(
            source_id=source_id
        )

        dataset_statement = insert(DataSet).values(
            **dataset_values
        )
        dataset_statement = dataset_statement.on_conflict_do_update(
            index_elements=[DataSet.code],
            set_={
                key: value
                for key, value in dataset_values.items()
                if key != "code"
            },
        )
        session.execute(dataset_statement)
        session.commit()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()
