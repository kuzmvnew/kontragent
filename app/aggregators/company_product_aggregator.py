from app.aggregators.company_aggregator import (
    get_company_for_web as get_base_company_for_web,
)
from app.services.cbr_finorg_service import (
    get_cached_cbr_finorg_check_for_inn,
)
from app.services.cbr_warning_list_service import (
    get_cbr_warning_list_check_for_inn,
)
from app.services.corporate_disclosure_service import get_cached_corporate_disclosure_check
from app.services.arbitration_court_service import get_cached_arbitration_court_check
from app.services.general_court_service import get_cached_general_court_check
from app.services.protected_source_session_service import get_latest_protected_source_check
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
from app.services.roszdrav_service import (
    get_cached_roszdrav_unified_license_check,
    get_roszdrav_bulk_license_check_for_inn,
    get_roszdrav_clinical_org_check_for_inn,
    get_roszdrav_medical_device_company_check,
)
from app.services.roskomnadzor_service import get_roskomnadzor_checks
from app.services.nopriz_service import get_cached_nopriz_check
from app.services.nostroy_service import get_cached_nostroy_check


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


def enrich_company_with_corporate_disclosure(company):
    if company is None:
        return None
    result = _copy_company(company)
    check = get_cached_corporate_disclosure_check(result.get("inn"))
    result["corporate_disclosure_check"] = check
    if check.get("result") in {"found", "not_found"}:
        _append_source_once(result, "prime_disclosure")
    return result


def enrich_company_with_stage15_on_demand_checks(company):
    """Cache reads only: this function must never contact an external source."""
    if company is None:
        return None
    result = _copy_company(company)
    inn = result.get("inn")
    result["arbitration_court_check"] = get_cached_arbitration_court_check(inn)
    result["general_court_check"] = get_cached_general_court_check(inn)
    result["fns_bankinform_check"] = get_latest_protected_source_check(inn, "fns_bankinform")
    result["cbr_zsk_check"] = get_latest_protected_source_check(inn, "cbr_zsk")
    for code, check in (
        ("checko_arbitration_cases", result["arbitration_court_check"]),
        ("moscow_general_court_cases", result["general_court_check"]),
        ("fns_bankinform", result["fns_bankinform_check"]),
        ("cbr_zsk", result["cbr_zsk_check"]),
    ):
        if check.get("checked"):
            _append_source_once(result, code)
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


def enrich_company_with_roszdrav(company):
    if company is None:
        return None
    result = _copy_company(company)
    inn = result.get("inn")
    result["roszdrav_bulk_license_check"] = get_roszdrav_bulk_license_check_for_inn(inn)
    result["roszdrav_unified_license_check"] = get_cached_roszdrav_unified_license_check(inn)
    result["roszdrav_clinical_org_check"] = get_roszdrav_clinical_org_check_for_inn(inn)
    result["roszdrav_medical_device_check"] = get_roszdrav_medical_device_company_check()
    if any(
        check.get("result") in {"found", "not_found"}
        for check in (
            result["roszdrav_bulk_license_check"],
            result["roszdrav_unified_license_check"],
            result["roszdrav_clinical_org_check"],
        )
    ):
        _append_source_once(result, "roszdravnadzor")
    return result


def enrich_company_with_roskomnadzor(company):
    if company is None:
        return None
    result = _copy_company(company)
    checks = get_roskomnadzor_checks(result.get("inn"))
    result["roskomnadzor_checks"] = checks
    if any(check.get("result") in {"found", "not_found"} for check in checks.values()):
        _append_source_once(result, "roskomnadzor")
    return result


def enrich_company_with_sro(company):
    if company is None:
        return None
    result = _copy_company(company)
    inn = result.get("inn")
    checks = {
        "nostroy": get_cached_nostroy_check(inn, applicable=True),
        "nopriz": get_cached_nopriz_check(inn, applicable=True),
    }
    result["sro_checks"] = checks
    if any(check.get("result") in {"found", "not_found"} for check in checks.values()):
        _append_source_once(result, "sro_registries")
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

    company = enrich_company_with_cbr_finorg(company)
    company = enrich_company_with_roszdrav(company)
    company = enrich_company_with_roskomnadzor(company)
    company = enrich_company_with_sro(company)
    company = enrich_company_with_corporate_disclosure(company)
    return enrich_company_with_stage15_on_demand_checks(company)
