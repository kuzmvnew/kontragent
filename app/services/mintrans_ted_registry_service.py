"""Disabled-by-default registry metadata for the Mintrans TED fixture source."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import DataSet, DataSource


SOURCE_CODE = "mintrans"
DATASET_CODE = "mintrans_ted_registry"
LIVE_INGESTION = False


def build_mintrans_ted_source_spec() -> dict:
    return {
        "code": SOURCE_CODE,
        "name": "Минтранс России",
        "source_type": "official",
        "priority": 30,
        "enabled": False,
        "website_url": None,
        "description": (
            "Метаданные официального владельца источника. В DEV-007 разрешены "
            "только локальные fixture-артефакты; live ingestion отключён."
        ),
    }


def build_mintrans_ted_dataset_spec(source_id: int) -> dict:
    return {
        "source_id": source_id,
        "code": DATASET_CODE,
        "name": "Минтранс: реестр экспедиторов (fixture)",
        "domain": "transport_forwarding",
        "update_mode": "import",
        "data_format": "xlsx",
        "refresh_schedule": "manual",
        "priority": 30,
        "enabled": False,
        "source_url": None,
        "description": (
            "Непроизводственный fixture pipeline. Сетевое получение, scheduler "
            "и публичная активация запрещены; live_ingestion=false."
        ),
        "dataset_kind": "bulk_snapshot",
        "freshness_policy": "manual",
        "operational_status": "not_configured",
        "auto_update_status": "not_configured",
    }


def ensure_mintrans_ted_fixture_dataset() -> None:
    """Register storage metadata without enabling ingestion or scheduling."""

    session = get_session()
    try:
        source_values = build_mintrans_ted_source_spec()
        session.execute(
            insert(DataSource)
            .values(**source_values)
            .on_conflict_do_update(
                index_elements=[DataSource.code],
                set_={
                    key: value for key, value in source_values.items() if key != "code"
                },
            )
        )
        session.flush()
        source_id = session.scalar(
            select(DataSource.id).where(DataSource.code == SOURCE_CODE)
        )
        dataset_values = build_mintrans_ted_dataset_spec(source_id)
        session.execute(
            insert(DataSet)
            .values(**dataset_values)
            .on_conflict_do_update(
                index_elements=[DataSet.code],
                set_={
                    key: value
                    for key, value in dataset_values.items()
                    if key != "code"
                },
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
