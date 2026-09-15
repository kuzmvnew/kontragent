from app.aggregators.company_aggregator import (
    get_company_for_web as get_base_company_for_web,
)
from app.services.disqualified_service import (
    get_disqualified_check_for_inn,
)


def _append_source_once(
    company,
    source_code,
):
    sources_used = company.setdefault(
        "sources_used",
        [],
    )

    if source_code not in sources_used:
        sources_used.append(source_code)


def enrich_company_with_disqualified(
    company,
):
    if company is None:
        return None

    result = dict(company)

    check = get_disqualified_check_for_inn(
        result.get("inn")
    )

    result[
        "disqualified_check"
    ] = check

    if (
        isinstance(check, dict)
        and check.get("result")
        in {"found", "not_found"}
    ):
        _append_source_once(
            result,
            "fns_disqualified",
        )

    return result


def get_company_for_web(
    inn: str,
):
    company = get_base_company_for_web(
        inn
    )

    return enrich_company_with_disqualified(
        company
    )
