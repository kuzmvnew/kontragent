from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import DataSet, DataSource


SOURCES = {
    "moscow_courts_official": {
        "name": "Суды города Москвы",
        "source_type": "official_public_service",
        "website_url": "https://mos-gorsud.ru/",
        "dataset": ("moscow_general_court_cases", "Суды Москвы: дела общей юрисдикции", "general_courts", "html", "https://mos-gorsud.ru/search"),
    },
    "checko": {
        "name": "Checko",
        "source_type": "commercial_aggregator_free_api",
        "website_url": "https://checko.ru/",
        "dataset": ("checko_arbitration_cases", "Checko: арбитражные дела", "arbitration_courts", "json", "https://api.checko.ru/v2/legal-cases"),
    },
}


def ensure_stage15_dataset(source_code: str) -> int:
    definition = SOURCES[source_code]
    dataset_code, dataset_name, domain, data_format, source_url = definition["dataset"]
    session = get_session()
    try:
        source_values = {
            "code": source_code,
            "name": definition["name"],
            "source_type": definition["source_type"],
            "priority": 30,
            "enabled": True,
            "website_url": definition["website_url"],
            "description": "Точечная user-triggered проверка; cache-first; без массового обхода.",
        }
        session.execute(insert(DataSource).values(**source_values).on_conflict_do_update(index_elements=[DataSource.code], set_={k: v for k, v in source_values.items() if k != "code"}))
        session.flush()
        source_id = session.scalar(select(DataSource.id).where(DataSource.code == source_code))
        dataset_values = {
            "source_id": source_id,
            "code": dataset_code,
            "name": dataset_name,
            "domain": domain,
            "update_mode": "api",
            "data_format": data_format,
            "refresh_schedule": "on_demand",
            "priority": 30,
            "enabled": True,
            "source_url": source_url,
            "description": "Один внешний запрос только после явного действия пользователя; карточка читает датированный cache.",
        }
        session.execute(insert(DataSet).values(**dataset_values).on_conflict_do_update(index_elements=[DataSet.code], set_={k: v for k, v in dataset_values.items() if k != "code"}))
        session.commit()
        return session.scalar(select(DataSet.id).where(DataSet.code == dataset_code))
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
