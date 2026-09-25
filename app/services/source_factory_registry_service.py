"""Control rows for the master-foundation source wave.

This module registers source metadata only.  It deliberately preserves runtime
enablement and readiness fields, so a deploy never activates a new source.
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.models.source import DataSet, DataSource


DATASETS = {
    "fns_egrul": {
        "source_code": "fns",
        "source_name": "ФНС России",
        "name": "ЕГРЮЛ: полная выгрузка и ежедневные изменения",
        "domain": "registry",
        "update_mode": "delta",
        "data_format": "xml_zip",
        "refresh_schedule": "daily",
        "priority": 1,
        "source_url": "https://www.nalog.gov.ru/rn77/service/egrip2/egrip_vzayim/",
        "dataset_kind": "access_credential_pending",
        "freshness_policy": "access_pending",
        "operational_status": OperationalStatus.ACCESS_PENDING,
        "auto_update_status": AutoUpdateStatus.ACCESS_PENDING,
    },
    "fns_egrip": {
        "source_code": "fns",
        "source_name": "ФНС России",
        "name": "ЕГРИП: полная выгрузка и ежедневные изменения",
        "domain": "registry",
        "update_mode": "delta",
        "data_format": "xml_zip",
        "refresh_schedule": "daily",
        "priority": 1,
        "source_url": "https://www.nalog.gov.ru/rn77/service/egrip2/egrip_vzayim/",
        "dataset_kind": "access_credential_pending",
        "freshness_policy": "access_pending",
        "operational_status": OperationalStatus.ACCESS_PENDING,
        "auto_update_status": AutoUpdateStatus.ACCESS_PENDING,
    },
    "girbo_accounting": {
        "source_code": "girbo",
        "source_name": "ГИР БО ФНС России",
        "name": "ГИР БО: бухгалтерская отчётность и корректировки",
        "domain": "financials",
        "update_mode": "api",
        "data_format": "json",
        "refresh_schedule": "daily",
        "priority": 5,
        "source_url": "https://api-bo.nalog.gov.ru/",
        "dataset_kind": "access_credential_pending",
        "freshness_policy": "access_pending",
        "operational_status": OperationalStatus.ACCESS_PENDING,
        "auto_update_status": AutoUpdateStatus.ACCESS_PENDING,
    },
    "mintrans_ted_registry": {
        "source_code": "mintrans",
        "source_name": "Минтранс России",
        "name": "Минтранс: реестр уведомлений ТЭД",
        "domain": "transport_forwarding",
        "update_mode": "bulk",
        "data_format": "xlsx",
        "refresh_schedule": "daily_check",
        "priority": 30,
        "source_url": "https://www.mintrans.gov.ru/search?type=docs",
        "dataset_kind": "bulk_snapshot",
        "freshness_policy": "irregular",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
    },
}


def ensure_source_factory_datasets(session: Session) -> tuple[DataSet, ...]:
    """Idempotently register all wave datasets without changing activation."""

    result: list[DataSet] = []
    for code, spec in DATASETS.items():
        source_values = {
            "code": spec["source_code"],
            "name": spec["source_name"],
            "source_type": "official",
            "priority": spec["priority"],
            "enabled": True,
            "website_url": spec["source_url"],
        }
        session.execute(
            insert(DataSource)
            .values(**source_values)
            .on_conflict_do_update(
                index_elements=[DataSource.code],
                set_={key: value for key, value in source_values.items() if key != "code"},
            )
        )
        source_id = session.scalar(
            select(DataSource.id).where(DataSource.code == spec["source_code"])
        )
        existing = session.scalar(select(DataSet).where(DataSet.code == code))
        metadata = {
            "source_id": source_id,
            "name": spec["name"],
            "domain": spec["domain"],
            "update_mode": spec["update_mode"],
            "data_format": spec["data_format"],
            "refresh_schedule": spec["refresh_schedule"],
            "priority": spec["priority"],
            "source_url": spec["source_url"],
            "description": "Official source adapter executed by Worker Foundation.",
        }
        if existing is None:
            existing = DataSet(
                code=code,
                enabled=False,
                dataset_kind=spec["dataset_kind"],
                freshness_policy=spec["freshness_policy"],
                operational_status=spec["operational_status"],
                auto_update_status=spec["auto_update_status"],
                **metadata,
            )
            session.add(existing)
        else:
            for key, value in metadata.items():
                setattr(existing, key, value)
        session.flush()
        result.append(existing)
    return tuple(result)
