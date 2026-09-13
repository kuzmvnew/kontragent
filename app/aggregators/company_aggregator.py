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
from app.services.headcount_service import (
    get_latest_headcount_for_company,
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
    "employee_count_year",
]


LIST_FIELDS = [
    "phones",
    "emails",
    "websites",
    "branches",
]


# =========================================================
# API PROVIDERS
# =========================================================


PROVIDERS = {
    "dadata": DadataCompanyProvider,
}


# =========================================================
# HELPERS
# =========================================================


def has_value(value):
    """
    Проверяет наличие реального значения.

    ВАЖНО:
    число 0 является значением.

    Например:
    employee_count = 0
    не должно считаться отсутствием данных.
    """

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
        sources = (
            session.execute(
                select(DataSource)
                .order_by(
                    DataSource.priority,
                    DataSource.name,
                )
            )
            .scalars()
            .all()
        )

        return {
            source.code: {
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
            for source in sources
        }

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
    Загружает API snapshots,
    которые уже сохранены в PostgreSQL.

    Внешних запросов здесь нет.
    """

    session = get_session()

    try:
        snapshots = (
            session.execute(
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

            source = (
                sources_by_id.get(
                    snapshot.source_id
                )
            )

            if source is None:
                continue

            payload = (
                snapshot.normalized_payload
                or {}
            )

            if not payload:
                continue

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
                    "payload": (
                        normalize_provider_payload(
                            payload
                        )
                    ),
                    "cached": True,
                }
            )

        return results

    finally:
        session.close()


# =========================================================
# DOMAIN DATASETS
# =========================================================


def load_domain_candidates(
    company_id,
):
    """
    Загружает нормализованные данные
    специализированных datasets.

    В отличие от API snapshots,
    это bulk/delta данные, уже
    разложенные по domain-таблицам.

    Сейчас подключён:
    - fns_headcount

    Позже здесь появятся:
    - girbo_reports
    - fns_tax_debt
    - fns_tax_paid
    - fssp_enforcement
    - и другие.
    """

    candidates = []

    # -----------------------------------------------------
    # FNS HEADCOUNT
    # -----------------------------------------------------

    headcount = (
        get_latest_headcount_for_company(
            company_id
        )
    )

    if headcount is not None:

        candidates.append(
            {
                "source": (
                    headcount[
                        "dataset_code"
                    ]
                ),
                "priority": (
                    headcount[
                        "priority"
                    ]
                ),
                "payload": {
                    "employee_count": (
                        headcount[
                            "employee_count"
                        ]
                    ),
                    "employee_count_year": (
                        headcount[
                            "year"
                        ]
                    ),
                },
                "cached": True,
            }
        )

    return candidates


# =========================================================
# EXTERNAL API PROVIDERS
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
    """
    Объединяет источники по priority.

    Чем меньше priority,
    тем выше доверие.

    Для scalar:
    берём первое непустое значение.

    Для list:
    объединяем значения.
    """

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

        # ---------------------------------------------
        # SCALAR
        # ---------------------------------------------

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
                and has_value(
                    value
                )
            ):

                merged[field] = value

                field_sources[
                    field
                ] = source_code

        # ---------------------------------------------
        # LIST
        # ---------------------------------------------

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

        Только локальные данные:

        - companies
        - API snapshots
        - domain datasets

        Никаких запросов наружу.


    refresh_external=True

        Дополнительно вызывает
        включённые API providers.
    """

    inn = str(
        inn
    ).strip()

    registry = (
        get_source_registry()
    )

    candidates_by_source = {}

    company_id = None

    # =====================================================
    # 1. MASTER / BASE COMPANY
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
    # 2. API SNAPSHOT CACHE
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
    # 3. DOMAIN DATASETS
    # =====================================================

    if company_id is not None:

        domain_candidates = (
            load_domain_candidates(
                company_id
            )
        )

        for candidate in (
            domain_candidates
        ):

            candidates_by_source[
                candidate["source"]
            ] = candidate

    # =====================================================
    # 4. OPTIONAL EXTERNAL API REFRESH
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

            # ---------------------------------------------
            # Компании ещё нет в companies
            # ---------------------------------------------

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

            # ---------------------------------------------
            # Сохраняем API snapshot
            # ---------------------------------------------

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

            # ---------------------------------------------
            # Свежий API результат
            # заменяет cached результат
            # того же provider.
            # ---------------------------------------------

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
                    "payload": (
                        payload
                    ),
                }

        # Если компания была создана
        # только что через API,
        # пробуем также найти
        # domain-data для неё.
        if company_id is not None:

            domain_candidates = (
                load_domain_candidates(
                    company_id
                )
            )

            for candidate in (
                domain_candidates
            ):

                candidates_by_source[
                    candidate["source"]
                ] = candidate

    # =====================================================
    # 5. NOTHING FOUND
    # =====================================================

    if not candidates_by_source:
        return None

    # =====================================================
    # 6. MERGE
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
    Основной режим сайта.

    Существующая компания:

    PostgreSQL
        +
    cached API snapshots
        +
    official domain datasets

    Никаких внешних API-вызовов.

    Только если компании вообще
    нет локально, допускается
    fallback API.
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