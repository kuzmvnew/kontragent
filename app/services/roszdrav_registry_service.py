from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import DataSet, DataSource


SOURCE_CODE = "roszdravnadzor"
LICENSE_DATASETS = {
    "pharma": "roszdrav_pharma_licenses",
    "narcotics": "roszdrav_narcotics_licenses",
    "medical_device_maintenance": "roszdrav_medical_device_maintenance_licenses",
}
UNIFIED_LICENSE_DATASET = "roszdrav_unified_license_search"
MEDICAL_DEVICE_DATASET = "roszdrav_medical_devices"
CLINICAL_ORG_DATASET = "roszdrav_clinical_research_orgs"


def build_roszdrav_source_spec() -> dict:
    return {
        "code": SOURCE_CODE,
        "name": "Росздравнадзор",
        "source_type": "official",
        "priority": 10,
        "enabled": True,
        "website_url": "https://roszdravnadzor.gov.ru/",
        "description": "Официальные реестры Федеральной службы по надзору в сфере здравоохранения",
    }


def build_roszdrav_dataset_specs(source_id: int) -> list[dict]:
    common = {"source_id": source_id, "priority": 10, "enabled": True}
    return [
        {**common, "code": LICENSE_DATASETS["pharma"], "name": "Росздравнадзор: лицензии на фармацевтическую деятельность", "domain": "healthcare_licenses", "update_mode": "bulk", "data_format": "zip_xml", "refresh_schedule": "weekly", "source_url": "https://roszdravnadzor.gov.ru/opendata/7710537160-ls_licenses", "description": "Официальный полный XML snapshot лицензий по фармацевтической деятельности."},
        {**common, "code": LICENSE_DATASETS["narcotics"], "name": "Росздравнадзор: лицензии на оборот наркотических средств", "domain": "healthcare_licenses", "update_mode": "bulk", "data_format": "zip_xml", "refresh_schedule": "weekly", "source_url": "https://roszdravnadzor.gov.ru/opendata/7710537160-nark_licenses", "description": "Официальный полный XML snapshot лицензий по обороту наркотических средств и психотропных веществ."},
        {**common, "code": LICENSE_DATASETS["medical_device_maintenance"], "name": "Росздравнадзор: лицензии на обслуживание медицинских изделий", "domain": "healthcare_licenses", "update_mode": "bulk", "data_format": "zip_xml", "refresh_schedule": "weekly", "source_url": "https://roszdravnadzor.gov.ru/opendata/7710537160-md_licenses", "description": "Официальный полный XML snapshot лицензий на техническое обслуживание медицинских изделий."},
        {**common, "code": UNIFIED_LICENSE_DATASET, "name": "Росздравнадзор: поиск в Едином реестре лицензий", "domain": "healthcare_licenses", "update_mode": "api", "data_format": "json", "refresh_schedule": "on_demand", "source_url": "https://roszdravnadzor.gov.ru/services/licenses", "description": "Точечная low-load проверка ИНН через официальный публичный поиск с датированным cache; transport не документирован как публичный API."},
        {**common, "code": MEDICAL_DEVICE_DATASET, "name": "Росздравнадзор: государственный реестр медицинских изделий", "domain": "medical_devices", "update_mode": "api", "data_format": "json", "refresh_schedule": "on_demand", "source_url": "https://elk.roszdravnadzor.gov.ru/widget/", "description": "Lookup по точному номеру регистрационного удостоверения. Автопривязка к Company запрещена: публичный ответ не содержит ИНН/ОГРН производителя."},
        {**common, "code": CLINICAL_ORG_DATASET, "name": "Росздравнадзор: организации для клинических исследований медицинских изделий", "domain": "healthcare_permissions", "update_mode": "bulk", "data_format": "csv", "refresh_schedule": "weekly", "source_url": "https://roszdravnadzor.gov.ru/opendata/7710537160-organizations", "description": "Официальный полный CSV-перечень организаций с exact ИНН."},
    ]


def ensure_roszdrav_datasets() -> None:
    session = get_session()
    try:
        source_values = build_roszdrav_source_spec()
        statement = insert(DataSource).values(**source_values).on_conflict_do_update(
            index_elements=[DataSource.code],
            set_={key: value for key, value in source_values.items() if key != "code"},
        )
        session.execute(statement)
        session.flush()
        source_id = session.execute(
            select(DataSource.id).where(DataSource.code == SOURCE_CODE)
        ).scalar_one()
        for values in build_roszdrav_dataset_specs(source_id):
            statement = insert(DataSet).values(**values).on_conflict_do_update(
                index_elements=[DataSet.code],
                set_={key: value for key, value in values.items() if key != "code"},
            )
            session.execute(statement)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
