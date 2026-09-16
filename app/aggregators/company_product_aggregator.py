from app.aggregators.company_aggregator import (
    get_company_for_web as get_base_company_for_web,
)
from app.services.cbr_finorg_service import (
    get_cached_cbr_finorg_check_for_inn,
)
from app.services.cbr_warning_list_service import (
    get_cbr_warning_list_check_for_inn,
)
from app.services.disqualified_service import (
    get_disqualified_check_for_inn,
)
from app.services.erknm_service import (
    get_erknm_check_for_company,
)
from app.services.fns_sme_support_service import (
    get_fns_sme_support_check_for_inn,
)
from app.services.npd_service import (
    get_cached_npd_check_for_inn,
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


def _copy_company(company):
    result = dict(company)
    result["sources_used"] = list(
        company.get("sources_used")
        or []
    )
    return result


def enrich_company_with_disqualified(
    company,
):
    if company is None:
        return None

    result = _copy_company(company)

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


def enrich_company_with_npd(
    company,
):
    if company is None:
        return None

    result = _copy_company(company)

    check = get_cached_npd_check_for_inn(
        result.get("inn")
    )

    result["npd_check"] = check

    if (
        isinstance(check, dict)
        and check.get("result")
        in {"found", "not_found"}
    ):
        _append_source_once(
            result,
            "fns_npd",
        )

    return result


def enrich_company_with_erknm(
    company,
):
    if company is None:
        return None

    result = _copy_company(company)

    check = get_erknm_check_for_company(
        inn=result.get("inn"),
        ogrn=result.get("ogrn"),
    )

    result["erknm_check"] = check

    if (
        isinstance(check, dict)
        and check.get("result")
        in {"found", "not_found"}
    ):
        _append_source_once(
            result,
            "erknm_inspections",
        )

    return result


def enrich_company_with_fns_sme_support(
    company,
):
    if company is None:
        return None

    result = _copy_company(company)

    check = get_fns_sme_support_check_for_inn(
        result.get("inn")
    )

    result["fns_sme_support_check"] = check

    if (
        isinstance(check, dict)
        and check.get("result")
        in {"found", "not_found"}
    ):
        _append_source_once(
            result,
            "fns_sme_support",
        )

    return result


def enrich_company_with_cbr_warning_list(
    company,
):
    if company is None:
        return None

    result = _copy_company(company)

    check = get_cbr_warning_list_check_for_inn(
        result.get("inn")
    )

    result["cbr_warning_list_check"] = check

    if (
        isinstance(check, dict)
        and check.get("result")
        in {"found", "not_found"}
    ):
        _append_source_once(
            result,
            "cbr_warning_list",
        )

    return result


def enrich_company_with_cbr_finorg(
    company,
):
    if company is None:
        return None

    result = _copy_company(company)

    check = get_cached_cbr_finorg_check_for_inn(
        result.get("inn")
    )

    result["cbr_finorg_check"] = check

    if (
        isinstance(check, dict)
        and check.get("result")
        in {"found", "not_found"}
    ):
        _append_source_once(
            result,
            "cbr_finorg",
        )

    return result


def get_company_for_web(
    inn: str,
):
    company = get_base_company_for_web(
        inn
    )

    company = enrich_company_with_disqualified(
        company
    )

    company = enrich_company_with_npd(
        company
    )

    company = enrich_company_with_erknm(
        company
    )

    company = enrich_company_with_fns_sme_support(
        company
    )

    company = enrich_company_with_cbr_warning_list(
        company
    )

    return enrich_company_with_cbr_finorg(
        company
    )
