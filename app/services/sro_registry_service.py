from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import DataSet, DataSource


SOURCES = {
    "nostroy": ("НОСТРОЙ", "https://reestr.nostroy.ru/"),
    "nopriz": ("НОПРИЗ", "https://reestr.nopriz.ru/"),
}


def build_sro_dataset_specs(source_ids):
    return [
        {"source_id": source_ids["nostroy"], "code": "nostroy_sro_members_on_demand", "name": "НОСТРОЙ: члены строительных СРО", "domain": "sro_membership", "update_mode": "api", "data_format": "json", "refresh_schedule": "on_demand", "priority": 10, "enabled": True, "source_url": "https://reestr.nostroy.ru/sro/all/member/list", "description": "Exact-INN dated cache; current and historical membership."},
        {"source_id": source_ids["nopriz"], "code": "nopriz_sro_members_on_demand", "name": "НОПРИЗ: члены СРО изыскателей и проектировщиков", "domain": "sro_membership", "update_mode": "api", "data_format": "json", "refresh_schedule": "on_demand", "priority": 10, "enabled": True, "source_url": "https://reestr.nopriz.ru/sro/all/member/list", "description": "Exact-INN dated cache; current and historical membership."},
        {"source_id": source_ids["nopriz"], "code": "nopriz_nrs_private_on_demand", "name": "НРС НОПРИЗ: специалисты", "domain": "professional_registry_private", "update_mode": "api", "data_format": "json", "refresh_schedule": "on_demand", "priority": 10, "enabled": True, "source_url": "https://nrs.nopriz.ru/", "description": "Exact official registration-number lookup; PRIVATE_INTERNAL only."},
        {"source_id": source_ids["nostroy"], "code": "nostroy_nrs_protected", "name": "НРС НОСТРОЙ: специалисты", "domain": "professional_registry_private", "update_mode": "api", "data_format": "html_images", "refresh_schedule": "manual", "priority": 10, "enabled": False, "source_url": "https://nrs.nostroy.ru/", "description": "Public view protects identifiers as images; automatic decoding disabled pending access/legal review."},
    ]


def ensure_sro_datasets():
    session = get_session()
    try:
        source_ids = {}
        for code, (name, url) in SOURCES.items():
            values = {"code": code, "name": name, "source_type": "official", "priority": 10, "enabled": True, "website_url": url, "description": "Официальный национальный реестр; undocumented machine channels используются только low-load on-demand."}
            session.execute(insert(DataSource).values(**values).on_conflict_do_update(index_elements=[DataSource.code], set_={k: v for k, v in values.items() if k != "code"}))
            session.flush()
            source_ids[code] = session.scalar(select(DataSource.id).where(DataSource.code == code))
        for values in build_sro_dataset_specs(source_ids):
            session.execute(insert(DataSet).values(**values).on_conflict_do_update(index_elements=[DataSet.code], set_={k: v for k, v in values.items() if k != "code"}))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
