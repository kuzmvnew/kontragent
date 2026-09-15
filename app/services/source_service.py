from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import (
    DataSet,
    DataSource,
)


# =========================================================
# DEFAULT SOURCES
# =========================================================


DEFAULT_SOURCES = [
    {
        "code": "fns",
        "name": "ФНС России",
        "source_type": "official",
        "priority": 10,
        "enabled": False,
        "website_url": "https://www.nalog.gov.ru/",
        "description": (
            "Официальные данные "
            "Федеральной налоговой службы"
        ),
    },
    {
        "code": "girbo",
        "name": "ГИР БО",
        "source_type": "official",
        "priority": 10,
        "enabled": False,
        "website_url": "https://bo.nalog.gov.ru/",
        "description": (
            "Государственный информационный "
            "ресурс бухгалтерской отчетности"
        ),
    },
    {
        "code": "fssp",
        "name": "ФССП России",
        "source_type": "official",
        "priority": 10,
        "enabled": False,
        "website_url": "https://fssp.gov.ru/",
        "description": (
            "Данные исполнительных производств"
        ),
    },
    {
        "code": "fedresurs",
        "name": "Федресурс",
        "source_type": "official",
        "priority": 10,
        "enabled": False,
        "website_url": "https://fedresurs.ru/",
        "description": (
            "Сообщения о существенных фактах "
            "и банкротстве"
        ),
    },
    {
        "code": "eis",
        "name": "ЕИС Закупки",
        "source_type": "official",
        "priority": 10,
        "enabled": False,
        "website_url": "https://zakupki.gov.ru/",
        "description": (
            "Сведения о государственных закупках"
        ),
    },
    {
        "code": "dadata",
        "name": "DaData",
        "source_type": "api",
        "priority": 50,
        "enabled": True,
        "website_url": "https://dadata.ru/",
        "description": (
            "Временный API-источник "
            "для разработки и fallback"
        ),
    },
    {
        "code": "excel_import",
        "name": "Первичная Excel-выгрузка",
        "source_type": "import",
        "priority": 80,
        "enabled": True,
        "website_url": None,
        "description": (
            "Первичная база компаний "
            "из Excel"
        ),
    },
]


# =========================================================
# DEFAULT DATASETS
# =========================================================


DEFAULT_DATASETS = [
    {
        "source_code": "excel_import",
        "code": "excel_companies",
        "name": "Первичная база компаний Excel",
        "domain": "registry",
        "update_mode": "import",
        "data_format": "xlsx",
        "refresh_schedule": "manual",
        "priority": 80,
        "enabled": True,
        "source_url": None,
        "description": (
            "Исходная Excel-база компаний"
        ),
    },
    {
        "source_code": "dadata",
        "code": "dadata_company_lookup",
        "name": "DaData: организация по ИНН",
        "domain": "registry",
        "update_mode": "api",
        "data_format": "json",
        "refresh_schedule": "on_demand",
        "priority": 50,
        "enabled": True,
        "source_url": None,
        "description": (
            "Временный API lookup "
            "для разработки"
        ),
    },
    {
        "source_code": "fns",
        "code": "fns_egrul",
        "name": "ФНС: ЕГРЮЛ",
        "domain": "registry",
        "update_mode": "delta",
        "data_format": "xml",
        "refresh_schedule": "daily",
        "priority": 10,
        "enabled": False,
        "source_url": None,
        "description": (
            "Реестр юридических лиц"
        ),
    },
    {
        "source_code": "fns",
        "code": "fns_egrip",
        "name": "ФНС: ЕГРИП",
        "domain": "registry",
        "update_mode": "delta",
        "data_format": "xml",
        "refresh_schedule": "daily",
        "priority": 10,
        "enabled": False,
        "source_url": None,
        "description": (
            "Реестр индивидуальных предпринимателей"
        ),
    },
    {
        "source_code": "fns",
        "code": "fns_msp",
        "name": "ФНС: Реестр МСП",
        "domain": "msp",
        "update_mode": "bulk",
        "data_format": "xml",
        "refresh_schedule": "monthly",
        "priority": 10,
        "enabled": False,
        "source_url": None,
        "description": (
            "Единый реестр субъектов "
            "малого и среднего предпринимательства"
        ),
    },
    {
        "source_code": "fns",
        "code": "fns_snr",
        "name": "ФНС: Специальные налоговые режимы ЮЛ",
        "domain": "tax_regime",
        "update_mode": "bulk",
        "data_format": "xml",
        "refresh_schedule": "monthly",
        "priority": 10,
        "enabled": False,
        "source_url": (
            "https://www.nalog.gov.ru/"
            "opendata/7707329152-snr/"
        ),
        "description": (
            "Специальные налоговые режимы "
            "юридических лиц"
        ),
    },
    {
        "source_code": "fns",
        "code": "fns_snrip",
        "name": "ФНС: Специальные налоговые режимы ИП",
        "domain": "tax_regime",
        "update_mode": "bulk",
        "data_format": "xml",
        "refresh_schedule": "monthly",
        "priority": 10,
        "enabled": False,
        "source_url": (
            "https://www.nalog.gov.ru/"
            "opendata/7707329152-snrip/"
        ),
        "description": (
            "Специальные налоговые режимы "
            "индивидуальных предпринимателей"
        ),
    },
    {
        "source_code": "fns",
        "code": "fns_disqualified",
        "name": "ФНС: Реестр дисквалифицированных лиц",
        "domain": "disqualification",
        "update_mode": "bulk",
        "data_format": "csv",
        "refresh_schedule": "daily",
        "priority": 10,
        "enabled": False,
        "source_url": (
            "https://www.nalog.gov.ru/"
            "opendata/"
            "7707329152-registerdisqualified/"
        ),
        "description": (
            "Реестр дисквалифицированных лиц ФНС. "
            "Для автоматической связи с компанией "
            "используется только ИНН организации."
        ),
    },
    {
        "source_code": "fns",
        "code": "fns_headcount",
        "name": "ФНС: Среднесписочная численность",
        "domain": "headcount",
        "update_mode": "bulk",
        "data_format": "xml",
        "refresh_schedule": "annual",
        "priority": 10,
        "enabled": False,
        "source_url": None,
        "description": (
            "Среднесписочная численность работников"
        ),
    },
    {
        "source_code": "fns",
        "code": "fns_tax_paid",
        "name": "ФНС: Уплаченные налоги",
        "domain": "taxes",
        "update_mode": "bulk",
        "data_format": "xml",
        "refresh_schedule": "annual",
        "priority": 10,
        "enabled": False,
        "source_url": None,
        "description": (
            "Сведения об уплаченных налогах"
        ),
    },
    {
        "source_code": "fns",
        "code": "fns_tax_debt",
        "name": "ФНС: Налоговая задолженность",
        "domain": "tax_debt",
        "update_mode": "bulk",
        "data_format": "xml",
        "refresh_schedule": "periodic",
        "priority": 10,
        "enabled": False,
        "source_url": None,
        "description": (
            "Сведения о задолженности "
            "по налогам и сборам"
        ),
    },
    {
        "source_code": "fns",
        "code": "fns_revenue_expenses",
        "name": "ФНС: Доходы и расходы",
        "domain": "revenue_expenses",
        "update_mode": "bulk",
        "data_format": "xml",
        "refresh_schedule": "annual",
        "priority": 10,
        "enabled": False,
        "source_url": None,
        "description": (
            "Сведения о доходах и расходах"
        ),
    },
    {
        "source_code": "girbo",
        "code": "girbo_reports",
        "name": "ГИР БО: Бухгалтерская отчетность",
        "domain": "financials",
        "update_mode": "api",
        "data_format": "json",
        "refresh_schedule": "periodic",
        "priority": 10,
        "enabled": False,
        "source_url": None,
        "description": (
            "Официальная бухгалтерская отчетность"
        ),
    },
    {
        "source_code": "fssp",
        "code": "fssp_enforcement",
        "name": "ФССП: Исполнительные производства",
        "domain": "enforcement",
        "update_mode": "api",
        "data_format": "json",
        "refresh_schedule": "periodic",
        "priority": 10,
        "enabled": False,
        "source_url": None,
        "description": (
            "Исполнительные производства"
        ),
    },
    {
        "source_code": "fedresurs",
        "code": "fedresurs_messages",
        "name": "Федресурс: Сообщения",
        "domain": "events",
        "update_mode": "api",
        "data_format": "json",
        "refresh_schedule": "daily",
        "priority": 10,
        "enabled": False,
        "source_url": None,
        "description": (
            "Сообщения о существенных фактах"
        ),
    },
    {
        "source_code": "eis",
        "code": "eis_procurements",
        "name": "ЕИС: Государственные закупки",
        "domain": "procurement",
        "update_mode": "bulk",
        "data_format": "xml",
        "refresh_schedule": "daily",
        "priority": 10,
        "enabled": False,
        "source_url": None,
        "description": (
            "Государственные и муниципальные закупки"
        ),
    },
]


# =========================================================
# SYNC REGISTRY
# =========================================================


def sync_default_registry():
    session = get_session()

    try:
        # ---------------------------------------------
        # SOURCES
        # ---------------------------------------------

        for source_data in DEFAULT_SOURCES:

            statement = insert(
                DataSource
            ).values(
                **source_data
            )

            statement = (
                statement.on_conflict_do_update(
                    index_elements=[
                        DataSource.code
                    ],
                    set_={
                        "name": (
                            source_data["name"]
                        ),
                        "source_type": (
                            source_data[
                                "source_type"
                            ]
                        ),
                        "priority": (
                            source_data[
                                "priority"
                            ]
                        ),
                        "enabled": (
                            source_data[
                                "enabled"
                            ]
                        ),
                        "website_url": (
                            source_data[
                                "website_url"
                            ]
                        ),
                        "description": (
                            source_data[
                                "description"
                            ]
                        ),
                    },
                )
            )

            session.execute(
                statement
            )

        session.flush()

        # ---------------------------------------------
        # SOURCE IDS
        # ---------------------------------------------

        source_rows = (
            session.execute(
                select(
                    DataSource
                )
            )
            .scalars()
            .all()
        )

        source_ids = {
            source.code: source.id
            for source in source_rows
        }

        # ---------------------------------------------
        # DATASETS
        # ---------------------------------------------

        for dataset_data in DEFAULT_DATASETS:

            source_code = (
                dataset_data[
                    "source_code"
                ]
            )

            source_id = (
                source_ids[
                    source_code
                ]
            )

            values = {
                key: value
                for key, value
                in dataset_data.items()
                if key != "source_code"
            }

            values[
                "source_id"
            ] = source_id

            statement = insert(
                DataSet
            ).values(
                **values
            )

            statement = (
                statement.on_conflict_do_update(
                    index_elements=[
                        DataSet.code
                    ],
                    set_={
                        "source_id": (
                            source_id
                        ),
                        "name": (
                            values["name"]
                        ),
                        "domain": (
                            values["domain"]
                        ),
                        "update_mode": (
                            values[
                                "update_mode"
                            ]
                        ),
                        "data_format": (
                            values[
                                "data_format"
                            ]
                        ),
                        "refresh_schedule": (
                            values[
                                "refresh_schedule"
                            ]
                        ),
                        "priority": (
                            values["priority"]
                        ),
                        "enabled": (
                            values["enabled"]
                        ),
                        "source_url": (
                            values[
                                "source_url"
                            ]
                        ),
                        "description": (
                            values[
                                "description"
                            ]
                        ),
                    },
                )
            )

            session.execute(
                statement
            )

        session.commit()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


# Старое название оставляем,
# чтобы ничего случайно не сломать.
def sync_default_sources():
    sync_default_registry()


# =========================================================
# READ SOURCES
# =========================================================


def get_source_by_code(
    code: str,
):
    session = get_session()

    try:
        statement = (
            select(DataSource)
            .where(
                DataSource.code
                == code
            )
        )

        return (
            session.execute(
                statement
            )
            .scalar_one_or_none()
        )

    finally:
        session.close()


def get_dataset_by_code(
    code: str,
):
    session = get_session()

    try:
        statement = (
            select(DataSet)
            .where(
                DataSet.code
                == code
            )
        )

        return (
            session.execute(
                statement
            )
            .scalar_one_or_none()
        )

    finally:
        session.close()


def list_sources():
    session = get_session()

    try:
        statement = (
            select(DataSource)
            .order_by(
                DataSource.priority,
                DataSource.name,
            )
        )

        sources = (
            session.execute(
                statement
            )
            .scalars()
            .all()
        )

        return [
            {
                "id": source.id,
                "code": source.code,
                "name": source.name,
                "type": (
                    source.source_type
                ),
                "priority": (
                    source.priority
                ),
                "enabled": (
                    source.enabled
                ),
            }
            for source in sources
        ]

    finally:
        session.close()


def list_datasets():
    session = get_session()

    try:
        statement = (
            select(
                DataSet,
                DataSource,
            )
            .join(
                DataSource,
                DataSet.source_id
                == DataSource.id,
            )
            .order_by(
                DataSource.name,
                DataSet.domain,
                DataSet.code,
            )
        )

        rows = (
            session.execute(
                statement
            )
            .all()
        )

        return [
            {
                "id": dataset.id,
                "source_code": (
                    source.code
                ),
                "source_name": (
                    source.name
                ),
                "code": dataset.code,
                "name": dataset.name,
                "domain": dataset.domain,
                "update_mode": (
                    dataset.update_mode
                ),
                "data_format": (
                    dataset.data_format
                ),
                "schedule": (
                    dataset.refresh_schedule
                ),
                "priority": (
                    dataset.priority
                ),
                "enabled": (
                    dataset.enabled
                ),
                "last_success_at": (
                    dataset.last_success_at
                ),
                "last_data_date": (
                    dataset.last_data_date
                ),
            }
            for dataset, source in rows
        ]

    finally:
        session.close()