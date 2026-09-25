from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import DataSet, DataSource


SOURCE_CODE = "fns"
DATASET_CODE = "fns_sme_support"
METADATA_URL = "https://www.nalog.gov.ru/opendata/7707329152-rsmppp/"


def build_fns_sme_support_source_spec() -> dict:
    return {
        "code": SOURCE_CODE,
        "name": "ФНС России",
        "source_type": "official",
        "priority": 10,
        # Registration must never activate the durable schedule. Activation is
        # an explicit, source-scoped operator action.
        "enabled": False,
        "website_url": "https://www.nalog.gov.ru/",
        "description": "Официальные данные Федеральной налоговой службы",
    }


def build_fns_sme_support_dataset_spec(source_id: int) -> dict:
    return {
        "source_id": source_id,
        "code": DATASET_CODE,
        "name": "ФНС: МСП — получатели поддержки",
        "domain": "sme_support",
        "update_mode": "bulk",
        "data_format": "xml",
        "refresh_schedule": "monthly",
        "priority": 10,
        "enabled": True,
        "source_url": METADATA_URL,
        "description": (
            "Официальный открытый XML-набор ФНС Единого реестра субъектов МСП "
            "и применяющих НПД физических лиц — получателей поддержки. "
            "Карточка компании связывается только по точному ИНН."
        ),
    }


def ensure_fns_sme_support_dataset() -> None:
    session = get_session()
    try:
        source_values = build_fns_sme_support_source_spec()
        statement = insert(DataSource).values(**source_values)
        statement = statement.on_conflict_do_update(
            index_elements=[DataSource.code],
            set_={k: v for k, v in source_values.items() if k != "code"},
        )
        session.execute(statement)
        session.flush()
        source_id = session.execute(
            select(DataSource.id).where(DataSource.code == SOURCE_CODE).limit(1)
        ).scalar_one()

        dataset_values = build_fns_sme_support_dataset_spec(source_id)
        statement = insert(DataSet).values(**dataset_values)
        statement = statement.on_conflict_do_update(
            index_elements=[DataSet.code],
            set_={k: v for k, v in dataset_values.items() if k != "code"},
        )
        session.execute(statement)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
