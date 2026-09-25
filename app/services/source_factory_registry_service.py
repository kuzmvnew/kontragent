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
    "firmoteka": {
        "source_code": "firmoteka",
        "source_name": "Firmoteka",
        "source_type": "authorized_bridge",
        "name": "Firmoteka: разрешённый публичный каталог",
        "domain": "company_enrichment",
        "update_mode": "catalog_crawl",
        "data_format": "html",
        "refresh_schedule": "daily_check",
        "priority": 20,
        "source_url": "https://firmoteka.ru/",
        "dataset_kind": "bulk_snapshot",
        "freshness_policy": "daily",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
        "description": (
            "Authorized secondary bridge. Public sitemap/catalog traversal; "
            "not an official registry and no national completeness claim."
        ),
    },
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
    "fns_npd": {
        "source_code": "fns",
        "source_name": "ФНС России",
        "source_priority": 1,
        "name": "ФНС: статус плательщика НПД",
        "domain": "npd_status",
        "update_mode": "api",
        "data_format": "json",
        "refresh_schedule": "daily_check",
        "priority": 10,
        "source_url": "https://statusnpd.nalog.ru/api/v1/tracker/taxpayer_status",
        "dataset_kind": "on_demand_api",
        "freshness_policy": "daily",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
    },
    "nostroy_sro_members_on_demand": {
        "source_code": "nostroy",
        "source_name": "НОСТРОЙ",
        "name": "НОСТРОЙ: члены строительных СРО",
        "domain": "sro_membership",
        "update_mode": "api",
        "data_format": "json",
        "refresh_schedule": "daily_check",
        "priority": 10,
        "source_url": "https://reestr.nostroy.ru/api/sro/all/member/list",
        "dataset_kind": "on_demand_api",
        "freshness_policy": "daily",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
    },
    "nopriz_sro_members_on_demand": {
        "source_code": "nopriz",
        "source_name": "НОПРИЗ",
        "name": "НОПРИЗ: члены СРО изыскателей и проектировщиков",
        "domain": "sro_membership",
        "update_mode": "api",
        "data_format": "json",
        "refresh_schedule": "daily_check",
        "priority": 10,
        "source_url": "https://reestr.nopriz.ru/api/sro/all/member/list",
        "dataset_kind": "on_demand_api",
        "freshness_policy": "daily",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
    },
    "prime_corporate_disclosure": {
        "source_code": "prime_disclosure",
        "source_name": "ПРАЙМ Раскрытие",
        "source_type": "public_disclosure",
        "name": "ПРАЙМ: корпоративное раскрытие",
        "domain": "corporate_disclosure",
        "update_mode": "api",
        "data_format": "html",
        "refresh_schedule": "daily_check",
        "priority": 20,
        "source_url": "https://disclosure.1prime.ru/Portal/Default.aspx?emId={inn}",
        "dataset_kind": "on_demand_api",
        "freshness_policy": "daily",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
        "description": "Public issuer-disclosure disseminator; exact-INN evidence, not Master authority.",
    },
    "eis_rnp": {
        "source_code": "eis",
        "source_name": "ЕИС Закупки",
        "name": "ЕИС: реестр недобросовестных поставщиков",
        "domain": "procurement_rnp",
        "update_mode": "soap_archive",
        "data_format": "xml_zip",
        "refresh_schedule": "daily_check",
        "priority": 10,
        "source_url": "https://int44.zakupki.gov.ru/eis-integration/services/getDocsIP",
        "dataset_kind": "access_credential_pending",
        "freshness_policy": "access_pending",
        "operational_status": OperationalStatus.ACCESS_PENDING,
        "auto_update_status": AutoUpdateStatus.ACCESS_PENDING,
        "description": "Official EIS machine channel; requires EIS_IP_TOKEN and accepted baseline schema.",
    },
    "rkn_personal_data_operators": {
        "source_code": "roskomnadzor",
        "source_name": "Роскомнадзор",
        "name": "Роскомнадзор: операторы персональных данных",
        "domain": "personal_data_registry",
        "update_mode": "api",
        "data_format": "html",
        "refresh_schedule": "daily_check",
        "priority": 10,
        "source_url": "https://pd.rkn.gov.ru/operators-registry/operators-list/",
        "dataset_kind": "on_demand_api",
        "freshness_policy": "daily",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
        "description": "Low-load exact-INN public legal-entity lookup; no mass crawling or personal data publication.",
    },
    "rkn_communications_licenses": {
        "source_code": "roskomnadzor", "source_name": "Роскомнадзор",
        "name": "Роскомнадзор: лицензии связи", "domain": "communications_licenses",
        "update_mode": "bulk", "data_format": "xml", "refresh_schedule": "daily_check",
        "priority": 10, "source_url": "https://rkn.gov.ru/opendata/7705846236-LicComm/",
        "dataset_kind": "bulk_snapshot", "freshness_policy": "daily",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
        "description": "Official daily XML snapshot with separately frozen XSD.",
    },
    "rkn_broadcast_licenses": {
        "source_code": "roskomnadzor", "source_name": "Роскомнадзор",
        "name": "Роскомнадзор: лицензии вещания", "domain": "broadcast_licenses",
        "update_mode": "bulk", "data_format": "xml", "refresh_schedule": "daily_check",
        "priority": 10, "source_url": "https://rkn.gov.ru/opendata/7705846236-LicBroadcast/",
        "dataset_kind": "bulk_snapshot", "freshness_policy": "daily",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
        "description": "Official daily XML snapshot with separately frozen XSD.",
    },
    "rkn_registered_media": {
        "source_code": "roskomnadzor", "source_name": "Роскомнадзор",
        "name": "Роскомнадзор: зарегистрированные СМИ", "domain": "media_registry",
        "update_mode": "bulk", "data_format": "xml", "refresh_schedule": "daily_check",
        "priority": 10, "source_url": "https://rkn.gov.ru/opendata/7705846236-ResolutionSMI/",
        "dataset_kind": "bulk_snapshot", "freshness_policy": "daily",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
        "description": "Official daily XML snapshot with separately frozen XSD.",
    },
    "rkn_information_distributors": {
        "source_code": "roskomnadzor", "source_name": "Роскомнадзор",
        "name": "Роскомнадзор: организаторы распространения информации", "domain": "information_distributors",
        "update_mode": "bulk", "data_format": "xml", "refresh_schedule": "daily_check",
        "priority": 10, "source_url": "https://rkn.gov.ru/opendata/7705846236-InformationDistributor/",
        "dataset_kind": "bulk_snapshot", "freshness_policy": "daily",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
        "description": "Official XML snapshot with separately frozen XSD.",
    },
    "rkn_hosting_providers": {
        "source_code": "roskomnadzor", "source_name": "Роскомнадзор",
        "name": "Роскомнадзор: провайдеры хостинга", "domain": "hosting_registry",
        "update_mode": "bulk", "data_format": "xlsx", "refresh_schedule": "daily_check",
        "priority": 10, "source_url": "https://rkn.gov.ru/activity/connection/register/p1578/",
        "dataset_kind": "bulk_snapshot", "freshness_policy": "daily",
        "operational_status": OperationalStatus.NOT_CONFIGURED,
        "auto_update_status": AutoUpdateStatus.NOT_CONFIGURED,
        "description": "Official XLSX registry snapshot; personal rows remain unpublished.",
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
            "source_type": spec.get("source_type", "official"),
            "priority": spec.get("source_priority", spec["priority"]),
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
            "description": spec.get(
                "description", "Official source adapter executed by Worker Foundation."
            ),
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
