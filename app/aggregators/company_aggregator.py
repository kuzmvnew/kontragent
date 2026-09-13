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
from app.services.tax_debt_service import (
    get_latest_tax_debt_for_company,
    get_tax_debt_history,
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
    Проверяет наличие значения.

    ВАЖНО:
    0 является реальным значением.
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
    Загружает сохранённые API snapshots.

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
    Domain datasets, которые можно
    представить как обычные scalar fields.

    Сейчас:
    - FNS headcount

    Tax debt подключается отдельно,
    потому что это самостоятельный
    сложный объект с детализацией.
    """

    candidates = []

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


def load_structured_domain_data(
    company_id,
):
    """
    Сложные domain objects,
    которые не нужно смешивать
    с обычными scalar fields.
    """

    tax_debt = (
        get_latest_tax_debt_for_company(
            company_id=company_id,
            include_items=True,
        )
    )

    tax_debt_history = (
        get_tax_debt_history(
            company_id=company_id,
            limit=24,
        )
    )

    return {
        "tax_debt": (
            tax_debt
        ),
        "tax_debt_history": (
            tax_debt_history
        ),
    }


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
    Объединяет обычные поля
    по priority.

    Чем меньше priority,
    тем выше доверие.
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

                merged[field] = (
                    value
                )

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
    Собирает компанию из:

    - master company
    - cached API snapshots
    - FNS headcount
    - FNS tax debt
    - optional external API refresh
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
            "priority": (
                priority
            ),
            "payload": (
                normalize_base_company(
                    base_company
                )
            ),
        }

    # =====================================================
    # 2. API CACHE
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
    # 3. SIMPLE DOMAIN DATASETS
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

        structured = (
            load_structured_domain_data(
                company_id
            )
        )

        result.update(
            structured
        )

        if (
            structured[
                "tax_debt"
            ]
            is not None
            and "fns_tax_debt"
            not in result[
                "sources_used"
            ]
        ):

            result[
                "sources_used"
            ].append(
                "fns_tax_debt"
            )

    else:

        result["tax_debt"] = None
        result[
            "tax_debt_history"
        ] = []

    return result


# =========================================================
# WEB READ MODE
# =========================================================


def get_company_for_web(
    inn: str,
):
    """
    Режим сайта.

    Сначала только PostgreSQL.

    Внешний API вызывается только,
    если компании вообще нет
    в нашей локальной базе.
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