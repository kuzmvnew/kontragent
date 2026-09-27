"""Shared company/source applicability rules for queues and backfills."""

from sqlalchemy import and_, func, not_, or_


LEGAL_ONLY_DATASET_CODES = frozenset(
    {
        "fns_egrul",
        "fns_revenue_expenses",
        "fns_tax_offence",
        "fns_tax_paid",
        "fns_tax_debt",
        "fns_headcount",
        "girbo_accounting",
        "nostroy_sro_members_on_demand",
        "nopriz_sro_members_on_demand",
        "prime_corporate_disclosure",
        "rkn_personal_data_operators",
        "rkn_communications_licenses",
        "rkn_broadcast_licenses",
        "rkn_registered_media",
        "rkn_information_distributors",
        "rkn_hosting_providers",
    }
)
IP_ONLY_DATASET_CODES = frozenset({"fns_egrip", "fns_npd"})


def company_scope(company) -> str:
    value = str(company.entity_type or "").strip().lower()
    if value in {"legal", "legal_entity", "organization"} or len(company.inn) == 10:
        return "legal"
    if value in {"individual_entrepreneur", "ip", "entrepreneur"} or len(company.inn) == 12:
        return "ip"
    return "unknown"


def source_is_applicable(company, dataset_code: str) -> bool:
    scope = company_scope(company)
    if dataset_code in LEGAL_ONLY_DATASET_CODES:
        return scope == "legal"
    if dataset_code in IP_ONLY_DATASET_CODES:
        return scope == "ip"
    return True


def source_applicability_clause(dataset_code_column, company_inn_column):
    scoped = LEGAL_ONLY_DATASET_CODES | IP_ONLY_DATASET_CODES
    return or_(
        and_(
            dataset_code_column.in_(tuple(LEGAL_ONLY_DATASET_CODES)),
            func.length(company_inn_column) == 10,
        ),
        and_(
            dataset_code_column.in_(tuple(IP_ONLY_DATASET_CODES)),
            func.length(company_inn_column) == 12,
        ),
        not_(dataset_code_column.in_(tuple(scoped))),
    )
