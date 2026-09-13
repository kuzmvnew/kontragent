from datetime import date, datetime, timezone

from sqlalchemy import select

from app.database.postgres import get_session
from app.models.company import Company
from app.models.source import (
    DataSet,
    DataSource,
)


# =========================================================
# HELPERS
# =========================================================


def utc_now():
    return datetime.now(
        timezone.utc
    )


def has_value(value):
    if value is None:
        return False

    if isinstance(value, str):
        return bool(
            value.strip()
        )

    return True


def detect_entity_type(
    inn: str,
):
    inn = str(
        inn
    ).strip()

    if (
        len(inn) == 10
        and inn.isdigit()
    ):
        return "legal"

    if (
        len(inn) == 12
        and inn.isdigit()
    ):
        return (
            "individual_entrepreneur"
        )

    raise ValueError(
        "Некорректный ИНН: "
        f"{inn}"
    )


def get_dataset_info(
    session,
    dataset_code,
):
    row = (
        session.execute(
            select(
                DataSet,
                DataSource,
            )
            .join(
                DataSource,
                DataSet.source_id
                == DataSource.id,
            )
            .where(
                DataSet.code
                == dataset_code
            )
        )
        .first()
    )

    if row is None:
        raise ValueError(
            "Dataset не найден: "
            f"{dataset_code}"
        )

    dataset, source = row

    return {
        "dataset": dataset,
        "source": source,
    }


def get_current_master_priority(
    session,
    company,
):
    """
    Возвращает priority текущего
    master dataset компании.

    Если master dataset отсутствует,
    считаем его низкоприоритетным.
    """

    if (
        company.master_dataset_id
        is None
    ):
        return 999

    dataset = session.get(
        DataSet,
        company.master_dataset_id,
    )

    if dataset is None:
        return 999

    return dataset.priority


# =========================================================
# MASTER UPSERT
# =========================================================


def upsert_master_company(
    *,
    dataset_code: str,
    inn: str,
    name: str,
    entity_type: str | None = None,
    kpp: str | None = None,
    ogrn: str | None = None,
    okpo: str | None = None,
    short_name: str | None = None,
    full_name: str | None = None,
    status: str | None = None,
    registration_date: date | None = None,
    termination_date: date | None = None,
    address: str | None = None,
    region_code: str | None = None,
    okved: str | None = None,
    activity: str | None = None,
    master_data_date: date | None = None,
):
    """
    Создаёт или обновляет master entity.

    Основной ключ:
        INN

    Правила:

    1. Если компании нет:
       создаём.

    2. Если incoming dataset имеет
       более высокий или равный приоритет:
       он может обновлять master-поля.

    3. Если incoming dataset хуже:
       он только заполняет пустые поля.

    Таким образом Excel не сможет
    потом перезаписать официальные
    данные ЕГРЮЛ.
    """

    inn = str(
        inn
    ).strip()

    if entity_type is None:
        entity_type = (
            detect_entity_type(
                inn
            )
        )

    if not has_value(name):
        raise ValueError(
            "Для master company "
            "обязательно название"
        )

    session = get_session()

    try:
        info = get_dataset_info(
            session,
            dataset_code,
        )

        dataset = info[
            "dataset"
        ]

        source = info[
            "source"
        ]

        if (
            dataset.domain
            not in {
                "registry",
                "msp",
            }
        ):
            raise ValueError(
                "Dataset не является "
                "registry dataset: "
                f"{dataset_code}"
            )

        company = (
            session.execute(
                select(Company)
                .where(
                    Company.inn
                    == inn
                )
            )
            .scalar_one_or_none()
        )

        # =================================================
        # CREATE
        # =================================================

        if company is None:

            company = Company(
                inn=inn,
                entity_type=entity_type,
                name=name,
                kpp=kpp,
                ogrn=ogrn,
                okpo=okpo,
                short_name=short_name,
                full_name=full_name,
                status=status,
                registration_date=(
                    registration_date
                ),
                termination_date=(
                    termination_date
                ),
                address=address,
                region_code=(
                    region_code
                ),
                okved=okved,
                activity=activity,
                master_dataset_id=(
                    dataset.id
                ),
                master_data_date=(
                    master_data_date
                ),
                source=source.code,
                source_updated_at=(
                    utc_now()
                ),
            )

            session.add(
                company
            )

            session.flush()

            result = {
                "action": "inserted",
                "company_id": (
                    company.id
                ),
                "inn": company.inn,
                "master_dataset": (
                    dataset.code
                ),
            }

            session.commit()

            return result

        # =================================================
        # UPDATE
        # =================================================

        current_priority = (
            get_current_master_priority(
                session,
                company,
            )
        )

        incoming_priority = (
            dataset.priority
        )

        incoming_is_preferred = (
            incoming_priority
            <= current_priority
        )

        values = {
            "entity_type": entity_type,
            "name": name,
            "kpp": kpp,
            "ogrn": ogrn,
            "okpo": okpo,
            "short_name": short_name,
            "full_name": full_name,
            "status": status,
            "registration_date": (
                registration_date
            ),
            "termination_date": (
                termination_date
            ),
            "address": address,
            "region_code": (
                region_code
            ),
            "okved": okved,
            "activity": activity,
        }

        fields_changed = []

        for (
            field,
            incoming_value,
        ) in values.items():

            if not has_value(
                incoming_value
            ):
                continue

            current_value = getattr(
                company,
                field,
            )

            should_write = (
                incoming_is_preferred
                or not has_value(
                    current_value
                )
            )

            if (
                should_write
                and current_value
                != incoming_value
            ):
                setattr(
                    company,
                    field,
                    incoming_value,
                )

                fields_changed.append(
                    field
                )

        if incoming_is_preferred:
            company.master_dataset_id = (
                dataset.id
            )

            if (
                master_data_date
                is not None
            ):
                company.master_data_date = (
                    master_data_date
                )

            company.source = (
                source.code
            )

            company.source_updated_at = (
                utc_now()
            )

        session.commit()

        return {
            "action": "updated",
            "company_id": company.id,
            "inn": company.inn,
            "master_dataset": (
                dataset.code
                if incoming_is_preferred
                else None
            ),
            "incoming_is_preferred": (
                incoming_is_preferred
            ),
            "fields_changed": (
                fields_changed
            ),
        }

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()