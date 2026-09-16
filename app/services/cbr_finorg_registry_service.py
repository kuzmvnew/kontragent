from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import DataSet, DataSource


SOURCE_CODE = "cbr"
DATASET_CODE = "cbr_finorg"


def build_cbr_finorg_source_spec() -> dict:
    return {
        "code": SOURCE_CODE,
        "name": "Банк России",
        "source_type": "official",
        "priority": 10,
        "enabled": True,
        "website_url": "https://www.cbr.ru/",
        "description": "Официальные данные Банка России",
    }


def build_cbr_finorg_dataset_spec(source_id: int) -> dict:
    return {
        "source_id": source_id,
        "code": DATASET_CODE,
        "name": (
            "Банк России: участники финансового рынка и лицензии"
        ),
        "domain": "financial_market_participants",
        "update_mode": "api",
        "data_format": "soap_xml",
        "refresh_schedule": "on_demand",
        "priority": 10,
        "enabled": True,
        "source_url": (
            "https://www.cbr.ru/FO_ZoomWS/FinOrg.asmx"
        ),
        "description": (
            "Официальный веб-сервис Банка России для получения "
            "актуальных сведений об участниках финансового рынка, "
            "их статусах и лицензиях."
        ),
    }


def ensure_cbr_finorg_dataset() -> None:
    session = get_session()

    try:
        source_values = build_cbr_finorg_source_spec()

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

        dataset_values = build_cbr_finorg_dataset_spec(
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
