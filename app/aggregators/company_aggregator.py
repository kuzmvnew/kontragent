from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.source import (
    CompanySourceData,
    DataSource,
)
from app.providers.dadata_provider import (
    DadataCompanyProvider,
)
from app.services.company_service import (
    get_company_from_database,
    save_provider_company,
)


# Поля, где нам нужно выбрать одно лучшее значение.
SCALAR_FIELDS = [
    "inn",
    "kpp",
    "ogrn",
    "okpo",
    "name",
    "short_name",
    "full_name",
    "address",
    "activity",
    "okved",
    "website",
    "status",
    "director_name",
    "director_position",
    "registration_date",
    "revenue",
    "company_value",
    "employee_count",
]


# Поля, которые можно объединять
# сразу из нескольких источников.
LIST_FIELDS = [
    "phones",
    "emails",
    "websites",
    "branches",
]


# Здесь будут подключаться все наши providers.
#
# Пока реально работает только DaData.
# Позже добавим:
#
# "fns": FnsCompanyProvider,
# "girbo": GirboProvider,
#
PROVIDERS = {
    "dadata": DadataCompanyProvider,
}


def has_value(value):
    """
    Проверяет, содержит ли поле полезное значение.
    """

    if value is None:
        return False

    if isinstance(value, str):
        return bool(value.strip())

    if isinstance(value, (list, dict)):
        return bool(value)

    return True


def get_source_registry():
    """
    Загружает Source Registry из PostgreSQL.

    Возвращает словарь вида:

    {
        "dadata": {
            "id": 3,
            "priority": 50,
            "enabled": True,
            ...
        }
    }
    """

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
            session.execute(statement)
            .scalars()
            .all()
        )

        registry = {}

        for source in sources:

            registry[source.code] = {
                "id": source.id,
                "code": source.code,
                "name": source.name,
                "source_type": source.source_type,
                "priority": source.priority,
                "enabled": source.enabled,
            }

        return registry

    finally:
        session.close()


def normalize_base_company(company):
    """
    Приводит нашу локальную карточку
    к единому формату Aggregator.
    """

    if company is None:
        return None

    result = {}

    for field in SCALAR_FIELDS:
        result[field] = company.get(field)

    for field in LIST_FIELDS:
        result[field] = (
            company.get(field)
            or []
        )

    return result


def normalize_provider_payload(payload):
    """
    Приводит ответ внешнего provider
    к единому формату Aggregator.
    """

    if payload is None:
        return None

    result = {}

    for field in SCALAR_FIELDS:
        result[field] = payload.get(field)

    for field in LIST_FIELDS:

        value = payload.get(field)

        if isinstance(value, list):
            result[field] = value

        else:
            result[field] = []

    return result


def save_source_payload(
    company_id,
    source_id,
    payload,
    status="success",
    error_message=None,
):
    """
    Сохраняет последний нормализованный ответ
    конкретного источника.

    Например:

    company_id = 1
    source = DaData
    payload = {...}
    """

    session = get_session()

    try:
        now = datetime.now(
            timezone.utc
        )

        statement = insert(
            CompanySourceData
        ).values(
            company_id=company_id,
            source_id=source_id,
            normalized_payload=payload,
            fetched_at=now,
            status=status,
            error_message=error_message,
        )

        statement = (
            statement.on_conflict_do_update(
                constraint=(
                    "uq_company_source_data"
                ),
                set_={
                    "normalized_payload": payload,
                    "fetched_at": now,
                    "status": status,
                    "error_message": (
                        error_message
                    ),
                },
            )
        )

        session.execute(statement)

        session.commit()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def fetch_external_sources(
    inn,
    source_registry,
):
    """
    Вызывает все включённые providers.

    Сейчас это фактически только DaData.
    """

    results = []

    for source_code, source in (
        source_registry.items()
    ):

        # Источник отключён.
        if not source["enabled"]:
            continue

        # Для источника ещё нет provider.
        provider_class = PROVIDERS.get(
            source_code
        )

        if provider_class is None:
            continue

        try:
            provider = provider_class()

            payload = provider.get_company(
                inn
            )

            if payload is None:
                continue

            normalized = (
                normalize_provider_payload(
                    payload
                )
            )

            results.append(
                {
                    "source": source_code,
                    "source_id": source["id"],
                    "priority": (
                        source["priority"]
                    ),
                    "payload": normalized,
                    "error": None,
                }
            )

        except Exception as error:

            results.append(
                {
                    "source": source_code,
                    "source_id": source["id"],
                    "priority": (
                        source["priority"]
                    ),
                    "payload": None,
                    "error": str(error),
                }
            )

    return results


def merge_candidates(candidates):
    """
    Объединяет данные разных источников.

    Меньшее значение priority =
    более приоритетный источник.

    Для обычных полей выбираем первое
    непустое значение.

    Для списков объединяем значения.
    """

    candidates = sorted(
        candidates,
        key=lambda item: item["priority"],
    )

    merged = {}

    field_sources = {}

    sources_used = []

    # Сначала создаём пустые списки.
    for field in LIST_FIELDS:
        merged[field] = []

    for candidate in candidates:

        source_code = candidate[
            "source"
        ]

        payload = candidate[
            "payload"
        ]

        if source_code not in sources_used:
            sources_used.append(
                source_code
            )

        # -------------------------
        # Одиночные поля
        # -------------------------

        for field in SCALAR_FIELDS:

            value = payload.get(field)

            current_value = merged.get(
                field
            )

            if (
                not has_value(
                    current_value
                )
                and has_value(value)
            ):

                merged[field] = value

                field_sources[
                    field
                ] = source_code

        # -------------------------
        # Списочные поля
        # -------------------------

        for field in LIST_FIELDS:

            values = (
                payload.get(field)
                or []
            )

            for value in values:

                if (
                    value
                    not in merged[field]
                ):

                    merged[field].append(
                        value
                    )

            if (
                values
                and field
                not in field_sources
            ):

                field_sources[
                    field
                ] = source_code

    # Добавляем отсутствующие scalar-поля.
    for field in SCALAR_FIELDS:

        if field not in merged:
            merged[field] = None

    merged["field_sources"] = (
        field_sources
    )

    merged["sources_used"] = (
        sources_used
    )

    merged["source"] = "aggregator"

    return merged


def aggregate_company(
    inn: str,
    refresh_external: bool = False,
):
    """
    Главная функция Aggregator.

    refresh_external=False
        использует только нашу БД.

    refresh_external=True
        дополнительно вызывает
        включённые внешние providers.
    """

    inn = str(inn).strip()

    registry = get_source_registry()

    candidates = []

    company_id = None

    # ==================================
    # 1. Наша PostgreSQL
    # ==================================

    base_company = (
        get_company_from_database(
            inn
        )
    )

    if base_company is not None:

        company_id = base_company[
            "id"
        ]

        base_source_code = (
            base_company.get("source")
            or "excel_import"
        )

        source_info = registry.get(
            base_source_code
        )

        # Если почему-то источник
        # отсутствует в Registry,
        # ставим самый низкий приоритет.
        priority = (
            source_info["priority"]
            if source_info
            else 100
        )

        candidates.append(
            {
                "source": (
                    base_source_code
                ),
                "priority": priority,
                "payload": (
                    normalize_base_company(
                        base_company
                    )
                ),
            }
        )

    # ==================================
    # 2. Внешние providers
    # ==================================

    if refresh_external:

        external_results = (
            fetch_external_sources(
                inn,
                registry,
            )
        )

        for result in external_results:

            payload = result.get(
                "payload"
            )

            error = result.get(
                "error"
            )

            # --------------------------
            # Компании вообще не было
            # в нашей БД.
            # --------------------------

            if (
                payload is not None
                and company_id is None
            ):

                save_provider_company(
                    payload
                )

                new_company = (
                    get_company_from_database(
                        inn
                    )
                )

                if new_company:
                    company_id = (
                        new_company["id"]
                    )

            # --------------------------
            # Сохраняем snapshot
            # внешнего источника.
            # --------------------------

            if company_id is not None:

                if payload is not None:

                    save_source_payload(
                        company_id=company_id,
                        source_id=result[
                            "source_id"
                        ],
                        payload=payload,
                        status="success",
                    )

                elif error:

                    save_source_payload(
                        company_id=company_id,
                        source_id=result[
                            "source_id"
                        ],
                        payload={},
                        status="error",
                        error_message=error,
                    )

            # --------------------------
            # Добавляем provider
            # в кандидаты для merge.
            # --------------------------

            if payload is not None:

                candidates.append(
                    {
                        "source": result[
                            "source"
                        ],
                        "priority": result[
                            "priority"
                        ],
                        "payload": payload,
                    }
                )

    # Ничего не нашли.
    if not candidates:
        return None

    # ==================================
    # 3. Merge
    # ==================================

    result = merge_candidates(
        candidates
    )

    if company_id is not None:
        result["id"] = company_id

    return result