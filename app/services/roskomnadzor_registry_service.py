from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import DataSet, DataSource

SOURCE_CODE = "roskomnadzor"
DATASETS = {
    "communications": "rkn_communications_licenses",
    "broadcast": "rkn_broadcast_licenses",
    "media": "rkn_registered_media",
    "information_distributors": "rkn_information_distributors",
    "hosting": "rkn_hosting_providers",
    "pd_operators": "rkn_personal_data_operators",
}


def build_roskomnadzor_source_spec():
    return {"code": SOURCE_CODE, "name": "Роскомнадзор", "source_type": "official", "priority": 10, "enabled": True, "website_url": "https://rkn.gov.ru/", "description": "Официальные реестры Роскомнадзора; персональные данные изолированы от публичного продукта."}


def build_roskomnadzor_dataset_specs(source_id):
    common = {"source_id": source_id, "priority": 10, "enabled": True}
    return [
        {**common, "code": DATASETS["communications"], "name": "Роскомнадзор: лицензии связи", "domain": "communications_licenses", "update_mode": "bulk", "data_format": "xml", "refresh_schedule": "manual", "source_url": "https://rkn.gov.ru/opendata/7705846236-LicComm/", "description": "Полный официальный XML snapshot; exact ИНН/ОГРН."},
        {**common, "code": DATASETS["broadcast"], "name": "Роскомнадзор: лицензии вещания", "domain": "broadcast_licenses", "update_mode": "bulk", "data_format": "xml", "refresh_schedule": "manual", "source_url": "https://rkn.gov.ru/opendata/7705846236-LicBroadcast/", "description": "Полный официальный XML snapshot; exact ИНН/ОГРН."},
        {**common, "code": DATASETS["media"], "name": "Роскомнадзор: зарегистрированные СМИ", "domain": "media_registry", "update_mode": "bulk", "data_format": "xml", "refresh_schedule": "manual", "source_url": "https://rkn.gov.ru/opendata/7705846236-ResolutionSMI/", "description": "Exact ИНН означает учредителя СМИ, но не доказывает текущий контроль."},
        {**common, "code": DATASETS["information_distributors"], "name": "Роскомнадзор: организаторы распространения информации", "domain": "information_distributors", "update_mode": "bulk", "data_format": "xml", "refresh_schedule": "manual", "source_url": "https://rkn.gov.ru/opendata/7705846236-InformationDistributor/", "description": "Официальный XML; публичный слой очищен от контактов."},
        {**common, "code": DATASETS["hosting"], "name": "Роскомнадзор: провайдеры хостинга", "domain": "hosting_registry", "update_mode": "bulk", "data_format": "xlsx", "refresh_schedule": "manual", "source_url": "https://rkn.gov.ru/activity/connection/register/p1578/", "description": "Официальный XLSX; публичный слой очищен от контактных лиц."},
        {**common, "code": DATASETS["pd_operators"], "name": "Роскомнадзор: операторы персональных данных", "domain": "personal_data_registry", "update_mode": "api", "data_format": "html", "refresh_schedule": "on_demand", "source_url": "https://pd.rkn.gov.ru/operators-registry/operators-list/", "description": "Low-load exact-INN lookup с датированным cache; без массового обхода."},
    ]


def ensure_roskomnadzor_datasets():
    session = get_session()
    try:
        source = build_roskomnadzor_source_spec()
        session.execute(insert(DataSource).values(**source).on_conflict_do_update(index_elements=[DataSource.code], set_={k: v for k, v in source.items() if k != "code"}))
        session.flush()
        source_id = session.scalar(select(DataSource.id).where(DataSource.code == SOURCE_CODE))
        for spec in build_roskomnadzor_dataset_specs(source_id):
            session.execute(insert(DataSet).values(**spec).on_conflict_do_update(index_elements=[DataSet.code], set_={k: v for k, v in spec.items() if k != "code"}))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
