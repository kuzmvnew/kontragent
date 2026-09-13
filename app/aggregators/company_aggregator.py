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


# =========================================================
# FIELDS
# =========================================================


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


LIST_FIELDS = [
    "phones",
    "emails",
    "websites",
    "branches",
]


# =========================================================
# PROVIDERS
# =========================================================


PROVIDERS = {
    "dadata": DadataCompanyProvider,
}


# Позже здесь появятся:
#
# "fns": FnsCompanyProvider,
# "girbo": GirboProvider,
# "fedresurs": FedresursProvider,
# ...


# =========================================================
# HELPERS
# =========================================================


def has_value(value):
    if value is None:
        return False

    if isinstance(value, str):
        return bool(
            value.strip()
        )

    if isinstance(
        value,
        (list, dict),
    ):
        return bool(value)

    return True


# =========================================================
# SOURCE REGISTRY
# =========================================================


def get_source_registry():
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

        registry = {}

        for source in sources:

            registry[source.code] = {
                "id": source.id,
                "code": source.code,
                "name": source.name,
                "source_type": (
                    source.source_type
                ),
                "priority": (
                    source.priority
                ),
                "enabled": (
                    source.enabled
                ),
            }

        return registry

    finally:
        session.close()


# =========================================================
# NORMALIZATION
# =========================================================


def normalize_base_company(
    company,
):
    if company is None:
        return None

    result = {}

    for field in SCALAR_FIELDS:
        result[field] = (
            company.get(field)
        )

    for field in LIST_FIELDS:
        result[field] = (
            company.get(field)
            or []
        )

    return result


def normalize_provider_payload(
    payload,
):
    if payload is None:
        return None

    result = {}

    for field in SCALAR_FIELDS:
        result[field] = (
            payload.get(field)
        )

    for field in LIST_FIELDS:

        value = payload.get(field)

        if isinstance(
            value,
            list,
        ):
            result[field] = value

        else:
            result[field] = []

    return result


# =========================================================
# SOURCE SNAPSHOTS
# =========================================================


def save_source_payload(
    company_id,
    source_id,
    payload,
    status="success",
    error_message=None,
):
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
            error_message=(
                error_message
            ),
        )

        statement = (
            statement.on_conflict_do_update(
                constraint=(
                    "uq_company_source_data"
                ),
                set_={
                    "normalized_payload": (
                        payload
                    ),
                    "fetched_at": now,
                    "status": status,
                    "error_message": (
                        error_message
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


def load_cached_source_candidates(
    company_id,
    source_registry,
):
    """
    Загружает уже сохранённые snapshots
    внешних источников из PostgreSQL.

    Никаких API-запросов здесь нет.
    """

    session = get_session()

    try:
        statement = (
            select(
                CompanySourceData
            )
            .where(
                CompanySourceData.company_id
                == company_id,
                CompanySourceData.status
                == "success",
            )
        )

        snapshots = (
            session.execute(
                statement
            )
            .scalars()
            .all()
        )

        sources_by_id = {
            source["id"]: source
            for source
            in source_registry.values()
        }

        results = []

        for snapshot in snapshots:

            source = sources_by_id.get(
                snapshot.source_id
            )

            if source is None:
                continue

            payload = (
                snapshot.normalized_payload
                or {}
            )

            if not payload:
                continue

            normalized = (
                normalize_provider_payload(
                    payload
                )
            )

            results.append(
                {
                    "source": (
                        source["code"]
                    ),
                    "source_id": (
                        source["id"]
                    ),
                    "priority": (
                        source["priority"]
                    ),
                    "payload": normalized,
                    "cached": True,
                }
            )

        return results

    finally:
        session.close()


# =========================================================
# EXTERNAL PROVIDERS
# =========================================================


def fetch_external_sources(
    inn,
    source_registry,
):
    results = []

    for (
        source_code,
        source,
    ) in source_registry.items():

        if not source["enabled"]:
            continue

        provider_class = (
            PROVIDERS.get(
                source_code
            )
        )

        # Источник есть в Registry,
        # но provider ещё не написан.
        if provider_class is None:
            continue

        try:
            provider = (
                provider_class()
            )

            payload = (
                provider.get_company(
                    inn
                )
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
                    "source": (
                        source_code
                    ),
                    "source_id": (
                        source["id"]
                    ),
                    "priority": (
                        source["priority"]
                    ),
                    "payload": (
                        normalized
                    ),
                    "error": None,
                }
            )

        except Exception as error:

            results.append(
                {
                    "source": (
                        source_code
                    ),
                    "source_id": (
                        source["id"]
                    ),
                    "priority": (
                        source["priority"]
                    ),
                    "payload": None,
                    "error": str(error),
                }
            )

    return results


# =========================================================
# MERGE
# =========================================================


def merge_candidates(
    candidates,
):
    candidates = sorted(
        candidates,
        key=lambda item: (
            item["priority"]
        ),
    )

    merged = {}

    field_sources = {}

    sources_used = []

    for field in LIST_FIELDS:
        merged[field] = []

    for candidate in candidates:

        source_code = (
            candidate["source"]
        )

        payload = (
            candidate["payload"]
        )

        if (
            source_code
            not in sources_used
        ):
            sources_used.append(
                source_code
            )

        # ---------------------------------
        # Одиночные значения
        # ---------------------------------

        for field in SCALAR_FIELDS:

            value = payload.get(
                field
            )

            current_value = (
                merged.get(field)
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

        # ---------------------------------
        # Списочные значения
        # ---------------------------------

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
                    merged[
                        field
                    ].append(
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

    for field in SCALAR_FIELDS:

        if field not in merged:
            merged[field] = None

    merged[
        "field_sources"
    ] = field_sources

    merged[
        "sources_used"
    ] = sources_used

    merged[
        "source"
    ] = "aggregator"

    return merged


# =========================================================
# MAIN AGGREGATOR
# =========================================================


def aggregate_company(
    inn: str,
    refresh_external: bool = False,
):
    """
    refresh_external=False

        Использует:
        - основную PostgreSQL
        - сохранённые source snapshots

        Никаких внешних запросов.


    refresh_external=True

        Дополнительно обращается
        к включённым providers,
        сохраняет свежие snapshots
        и объединяет их.
    """

    inn = str(inn).strip()

    registry = (
        get_source_registry()
    )

    # Используем словарь,
    # чтобы один источник
    # не появился дважды.
    candidates_by_source = {}

    company_id = None

    # =====================================================
    # 1. MAIN DATABASE
    # =====================================================

    base_company = (
        get_company_from_database(
            inn
        )
    )

    if base_company is not None:

        company_id = (
            base_company["id"]
        )

        base_source_code = (
            base_company.get(
                "source"
            )
            or "excel_import"
        )

        source_info = (
            registry.get(
                base_source_code
            )
        )

        priority = (
            source_info["priority"]
            if source_info
            else 100
        )

        candidates_by_source[
            base_source_code
        ] = {
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

    # =====================================================
    # 2. CACHED SOURCE SNAPSHOTS
    # =====================================================

    if company_id is not None:

        cached_candidates = (
            load_cached_source_candidates(
                company_id,
                registry,
            )
        )

        for candidate in (
            cached_candidates
        ):
            candidates_by_source[
                candidate["source"]
            ] = candidate

    # =====================================================
    # 3. OPTIONAL EXTERNAL REFRESH
    # =====================================================

    if refresh_external:

        external_results = (
            fetch_external_sources(
                inn,
                registry,
            )
        )

        for result in (
            external_results
        ):

            payload = (
                result.get(
                    "payload"
                )
            )

            error = (
                result.get(
                    "error"
                )
            )

            # Компания отсутствовала
            # в нашей PostgreSQL.
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
                        new_company[
                            "id"
                        ]
                    )

            # Сохраняем свежий snapshot.
            if company_id is not None:

                if payload is not None:

                    save_source_payload(
                        company_id=(
                            company_id
                        ),
                        source_id=result[
                            "source_id"
                        ],
                        payload=payload,
                        status="success",
                    )

                elif error:

                    save_source_payload(
                        company_id=(
                            company_id
                        ),
                        source_id=result[
                            "source_id"
                        ],
                        payload={},
                        status="error",
                        error_message=error,
                    )

            # Свежий provider-result
            # заменяет старый cached snapshot
            # этого же источника.
            if payload is not None:

                candidates_by_source[
                    result["source"]
                ] = {
                    "source": (
                        result["source"]
                    ),
                    "priority": (
                        result[
                            "priority"
                        ]
                    ),
                    "payload": payload,
                }

    # =====================================================
    # 4. NOTHING FOUND
    # =====================================================

    if not candidates_by_source:
        return None

    # =====================================================
    # 5. MERGE
    # =====================================================

    result = merge_candidates(
        list(
            candidates_by_source.values()
        )
    )

    if company_id is not None:
        result["id"] = (
            company_id
        )

    return result


# =========================================================
# WEB READ MODE
# =========================================================


def get_company_for_web(
    inn: str,
):
    """
    Используется сайтом.

    1. Сначала только локальный кэш.
    2. Если компании вообще нет —
       разрешаем один внешний запрос.

    Поэтому просмотр существующей
    карточки НЕ расходует API.
    """

    company = aggregate_company(
        inn=inn,
        refresh_external=False,
    )

    if company is not None:
        return company

    return aggregate_company(
        inn=inn,
        refresh_external=True,
    )