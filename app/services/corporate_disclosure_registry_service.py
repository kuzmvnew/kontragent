from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import DataSet, DataSource


SOURCE_CODE = "prime_disclosure"
DATASET_CODE = "prime_corporate_disclosure"


def ensure_corporate_disclosure_dataset() -> None:
    session = get_session()
    try:
        source_values = {
            "code": SOURCE_CODE,
            "name": "ПРАЙМ Раскрытие",
            "source_type": "public_disclosure",
            "priority": 20,
            "enabled": True,
            "website_url": "https://disclosure.1prime.ru/",
            "description": "Публичные страницы раскрытия эмитентов; exact ИНН, cache-first, без массового обхода.",
        }
        session.execute(insert(DataSource).values(**source_values).on_conflict_do_update(index_elements=[DataSource.code], set_={k: v for k, v in source_values.items() if k != "code"}))
        session.flush()
        source_id = session.scalar(select(DataSource.id).where(DataSource.code == SOURCE_CODE))
        dataset_values = {
            "source_id": source_id,
            "code": DATASET_CODE,
            "name": "ПРАЙМ: корпоративное раскрытие",
            "domain": "corporate_disclosure",
            "update_mode": "api",
            "data_format": "html",
            "refresh_schedule": "on_demand",
            "priority": 20,
            "enabled": True,
            "source_url": "https://disclosure.1prime.ru/Portal/Default.aspx?emId={inn}",
            "description": "Targeted public issuer page lookup by exact INN; document metadata and links only.",
        }
        session.execute(insert(DataSet).values(**dataset_values).on_conflict_do_update(index_elements=[DataSet.code], set_={k: v for k, v in dataset_values.items() if k != "code"}))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
