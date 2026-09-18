"""Bounded on-demand orchestration before Risk Engine v2 calculation.

The orchestrator never converts an error, unavailable source, or human action
into a negative result. QUICK is cache-only. FULL may call explicitly registered
source runners and creates official human-action sessions for protected sources.
REFRESH_DUE calls only runners whose cached result is missing, stale, or failed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from collections.abc import Iterable
from typing import Any, Callable
from uuid import uuid4

from app.aggregators.company_product_aggregator import get_company_for_web
from app.contracts.orchestrator import (
    CompanyCheckMode,
    CompanyCheckOutcome,
    CompanyCheckResult,
    CompanyCheckStatus,
)
from app.services.protected_source_session_service import (
    get_latest_protected_source_check,
    start_protected_source_session,
)
from app.services.risk_engine_service import (
    build_risk_assessment,
    calculate_company_risk,
    get_dataset_states,
)
from app.services.summary_engine_service import build_summary, generate_company_summary
from app.contracts.risk import RiskProfile
from app.contracts.source_architecture import (
    FreshnessStatus,
    NormalizedCheckResult,
    NormalizedEvidence,
    NormalizedResultStatus,
    SourceClass,
)
from app.services.risk_engine_v3_service import build_risk_v3
from app.services.source_resolution_service import DEFAULT_RESOLVER
from app.services.summary_engine_v3_service import build_summary_v3
from app.services.capability_applicability_service import apply_capability_applicability
from app.services.risk_v3_persistence_service import persist_v3_assessment


Runner = Callable[[str], dict[str, Any]]
PROTECTED_SOURCE_URLS = {
    "fssp": "https://fssp.gov.ru/iss/ip/",
    "fns_bankinform": "https://service.nalog.ru/bi.do",
    "cbr_zsk": "https://cbr.ru/counteraction_m_ter/platform_zsk/proverka-po-inn/",
}


def _default_runners(company: dict[str, Any]) -> dict[str, Runner]:
    """Return bounded, user-triggered provider calls applicable to one company."""
    from app.services.arbitration_court_service import refresh_arbitration_court_check
    from app.services.cbr_finorg_service import refresh_cbr_finorg_check_for_inn
    from app.services.corporate_disclosure_service import refresh_corporate_disclosure_check
    from app.services.general_court_service import refresh_general_court_check
    from app.services.npd_service import refresh_npd_check_for_inn
    from app.services.roskomnadzor_service import refresh_pd_operator_check

    values: dict[str, Runner] = {
        "arbitration": refresh_arbitration_court_check,
        "general_courts": refresh_general_court_check,
        "cbr_finorg": refresh_cbr_finorg_check_for_inn,
        "corporate_disclosure": refresh_corporate_disclosure_check,
        "roskomnadzor_pd": refresh_pd_operator_check,
    }
    if company.get("entity_type") == "individual_entrepreneur":
        values["npd"] = refresh_npd_check_for_inn
    return values


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _status(check: dict[str, Any]) -> CompanyCheckStatus:
    result = check.get("result")
    reason = str(check.get("reason") or check.get("status") or "")
    if result == "found":
        return CompanyCheckStatus.SUCCESS_FOUND
    if result == "not_found":
        return CompanyCheckStatus.SUCCESS_NOT_FOUND
    if result == "not_applicable":
        return CompanyCheckStatus.NOT_APPLICABLE
    if reason in {"challenge_required", "waiting_for_user", "created", "browser_started"}:
        return CompanyCheckStatus.HUMAN_ACTION_REQUIRED
    if reason == "stale":
        return CompanyCheckStatus.STALE
    if reason == "access_pending":
        return CompanyCheckStatus.ACCESS_PENDING
    if reason == "source_blocked":
        return CompanyCheckStatus.SOURCE_BLOCKED
    if reason in {"error", "source_error", "provider_error"}:
        return CompanyCheckStatus.ERROR
    return CompanyCheckStatus.UNAVAILABLE


def _outcome(code: str, source: str, check: dict[str, Any]) -> CompanyCheckOutcome:
    status = _status(check)
    messages = {
        CompanyCheckStatus.SUCCESS_FOUND: "Сведения найдены.",
        CompanyCheckStatus.SUCCESS_NOT_FOUND: "Источник проверен, сведения не найдены.",
        CompanyCheckStatus.NOT_APPLICABLE: "Проверка не применяется к компании.",
        CompanyCheckStatus.STALE: "Сохранённый результат требует обновления.",
        CompanyCheckStatus.UNAVAILABLE: "Проверку не удалось завершить.",
        CompanyCheckStatus.HUMAN_ACTION_REQUIRED: "Требуется ручное подтверждение на официальном сайте.",
        CompanyCheckStatus.ACCESS_PENDING: "Для источника требуется оформить официальный доступ.",
        CompanyCheckStatus.SOURCE_BLOCKED: "Официальный источник блокирует автоматическое получение результата.",
        CompanyCheckStatus.ERROR: "Источник вернул ошибку; это не считается отсутствием сведений.",
    }
    return CompanyCheckOutcome(
        check_code=code,
        status=status,
        source_code=source,
        checked_at=_as_datetime(check.get("checked_at")),
        source_as_of=_as_datetime(check.get("data_date") or check.get("source_as_of")),
        source_url=check.get("source_url"),
        message=messages[status],
        evidence={
            key: value for key, value in check.items()
            if key in {"record_count", "total_count", "amount", "coverage", "reason", "status"}
        },
    )


def _cached_outcomes(company: dict[str, Any]) -> list[CompanyCheckOutcome]:
    definitions = (
        ("registration", "fns_egrul_egrip", {
            "result": "found" if company.get("status") else "unavailable",
            "reason": None if company.get("status") else "not_checked",
            "data_date": company.get("master_data_date"),
        }),
        ("tax_debt", "fns", company.get("tax_debt_check") or {}),
        ("tax_offence", "fns", company.get("tax_offence_check") or {}),
        ("finance", "fns", company.get("revenue_expense_check") or {}),
        ("bankruptcy", "fedresurs", company.get("bankruptcy_check") or {"reason": "access_pending"}),
        ("fssp", "fssp", company.get("fssp_check") or {}),
        ("arbitration", "checko_legal_cases", company.get("arbitration_court_check") or {}),
        ("general_courts", "official_courts", company.get("general_court_check") or {}),
        ("cbr_warning", "cbr", company.get("cbr_warning_list_check") or {}),
    )
    return [_outcome(code, source, check) for code, source, check in definitions]


def _profile(company: dict[str, Any]) -> RiskProfile:
    if company.get("entity_type") == "individual_entrepreneur":
        return RiskProfile.IP
    return RiskProfile.GENERAL_LE


def _source_class(check: dict[str, Any]) -> SourceClass:
    source = str(check.get("source") or check.get("dataset_code") or "").casefold()
    if any(token in source for token in ("firmoteka", "checko")):
        return SourceClass.AUTHORIZED_BRIDGE
    if any(token in source for token in ("direct", "court", "fssp", "bankinform", "zsk")):
        return SourceClass.OFFICIAL_DIRECT
    return SourceClass.OFFICIAL_DOWNLOADED_DATASET


def _licence_sro_check(company: dict[str, Any]) -> dict[str, Any]:
    """Combine already-loaded official licence/SRO checks without new I/O."""

    checks: list[tuple[str, dict[str, Any]]] = []
    for code, check in (company.get("sro_checks") or {}).items():
        if isinstance(check, dict) and check.get("result") != "not_applicable":
            checks.append((code, check))
    for code in (
        "roszdrav_bulk_license_check",
        "roszdrav_unified_license_check",
        "roszdrav_clinical_org_check",
    ):
        check = company.get(code)
        if isinstance(check, dict) and check.get("result") != "not_applicable":
            checks.append((code, check))
    if not checks:
        return {}
    completed = [(code, check) for code, check in checks if check.get("result") in {"found", "not_found"}]
    found = [(code, check) for code, check in completed if check.get("result") == "found"]
    evidence_checks = found or completed
    if evidence_checks:
        return {
            "result": "found" if found else "not_found",
            "source": "official_licence_registries",
            "dataset_code": "official_licence_registries",
            "data_date": next((check.get("data_date") for _, check in evidence_checks if check.get("data_date")), None),
            "checks": {code: check for code, check in evidence_checks},
            # These registries cover their own regulated scopes, not every
            # licence/SRO obligation a company may have.
            "normalized_coverage": .75 if found else .5,
            "coverage_complete": False,
            "coverage_note": "Проверены применимые подключённые реестры; иные разрешения могут требовать отдельной проверки.",
        }
    return {
        "result": "unavailable",
        "source": "official_licence_registries",
        "dataset_code": "official_licence_registries",
        "reason": "Подключённые профильные реестры не дали завершённого результата.",
        "checks": {code: check for code, check in checks},
    }


def _erknm_check(company: dict[str, Any]) -> dict[str, Any]:
    check = dict(company.get("erknm_check") or {})
    if check.get("result") != "found":
        return check
    violation_tokens = ("нарушен", "выявлен", "предписан", "штраф")
    texts = " ".join(
        str(record.get(field) or "")
        for record in check.get("records") or ()
        if isinstance(record, dict)
        for field in ("result_text", "warning_caption")
    ).casefold()
    check["violations_found"] = any(token in texts for token in violation_tokens)
    return check


def _normalize_legacy_company(company: dict[str, Any], now: datetime) -> tuple[NormalizedCheckResult, ...]:
    definitions = (
        ("tax_debt", company.get("tax_debt_check") or {}),
        ("tax_offence", company.get("tax_offence_check") or {}),
        ("finance", company.get("revenue_expense_check") or {}),
        ("bankruptcy", company.get("bankruptcy_check") or {}),
        ("fssp", company.get("fssp_check") or {}),
        ("arbitration", company.get("arbitration_court_check") or {}),
        ("general_courts", company.get("general_court_check") or {}),
        ("cbr_warning", company.get("cbr_warning_list_check") or {}),
        ("cbr_zsk", company.get("cbr_zsk_check") or {}),
        ("bankinform", company.get("fns_bankinform_check") or {}),
        ("management", company.get("disqualified_check") or {}),
        ("licences_sro", _licence_sro_check(company)),
        ("regulatory_inspections", _erknm_check(company)),
    )
    output: list[NormalizedCheckResult] = []
    status = company.get("status")
    registration_value = {
        "status": status, "registration_date": company.get("registration_date"),
        "termination_date": company.get("termination_date"), "ogrn": company.get("ogrn"),
    }
    output.append(NormalizedCheckResult(
        check_code="registration",
        result=NormalizedResultStatus.FOUND if status else NormalizedResultStatus.UNAVAILABLE,
        source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
        source_code="fns_egrul_egrip", original_source="ФНС ЕГРЮЛ/ЕГРИП",
        exact_identifier_match=True if status else None, checked_at=now,
        source_as_of=_as_datetime(company.get("master_data_date")),
        freshness=FreshnessStatus.CURRENT if status else FreshnessStatus.UNKNOWN,
        coverage=1 if status else 0, confidence=1 if status else 0,
        evidence=(NormalizedEvidence(fact="Регистрационные сведения", value=registration_value,
            evidence_id=f"company:{company.get('inn')}:registration"),) if status else (),
        limitation=None if status else "Регистрационный статус не получен.",
    ))
    result_map = {
        "found": NormalizedResultStatus.FOUND, "not_found": NormalizedResultStatus.NOT_FOUND,
        "high_risk_information_found": NormalizedResultStatus.FOUND,
        "high_risk_information_not_found": NormalizedResultStatus.NOT_FOUND,
        "active_suspensions_found": NormalizedResultStatus.FOUND,
        "active_suspensions_not_found": NormalizedResultStatus.NOT_FOUND,
        "enforcement_found": NormalizedResultStatus.FOUND,
        "enforcement_not_found": NormalizedResultStatus.NOT_FOUND,
        "not_applicable": NormalizedResultStatus.NOT_APPLICABLE,
        "unavailable": NormalizedResultStatus.UNAVAILABLE,
    }
    for code, check in definitions:
        raw_result = check.get("result")
        protected_incomplete = (
            code in {"cbr_zsk", "bankinform", "fssp"}
            and check.get("status") not in {None, "completed"}
        )
        result = (
            NormalizedResultStatus.UNAVAILABLE
            if protected_incomplete
            else result_map.get(raw_result, NormalizedResultStatus.UNAVAILABLE)
        )
        partial = check.get("coverage_complete") is False or (
            code in {"arbitration", "general_courts"} and bool(check.get("coverage"))
        )
        if partial and result in {NormalizedResultStatus.FOUND, NormalizedResultStatus.NOT_FOUND}:
            result = NormalizedResultStatus.PARTIAL
        default_coverage = 1 if result in {NormalizedResultStatus.FOUND, NormalizedResultStatus.NOT_FOUND, NormalizedResultStatus.NOT_APPLICABLE} else .35 if result == NormalizedResultStatus.PARTIAL else 0
        coverage = float(check.get("normalized_coverage", default_coverage))
        source_code = str(check.get("dataset_code") or check.get("source") or code)
        evidence = tuple(
            [NormalizedEvidence(fact=code, value=check, evidence_id=f"{source_code}:{company.get('inn')}:{code}")]
            if result in {NormalizedResultStatus.FOUND, NormalizedResultStatus.NOT_FOUND, NormalizedResultStatus.PARTIAL} else []
        )
        raw_limitation = str(check.get("coverage_note") or check.get("reason") or "Проверка не завершена.")
        limitation = {
            "not_checked": "Источник ещё не проверен.",
            "access_pending": "Для источника требуется официальный доступ.",
            "source_blocked": "Источник не позволил завершить проверку.",
            "provider_error": "Поставщик данных вернул ошибку.",
            "error": "Проверка завершилась ошибкой.",
        }.get(raw_limitation, raw_limitation)
        output.append(NormalizedCheckResult(
            check_code=code, result=result, source_class=_source_class(check), source_code=source_code,
            original_source=str(check.get("source") or source_code), exact_identifier_match=True if coverage else None,
            checked_at=_as_datetime(check.get("checked_at")) or now,
            source_as_of=_as_datetime(check.get("data_date") or check.get("source_as_of")),
            freshness=FreshnessStatus.CURRENT if coverage else FreshnessStatus.UNKNOWN,
            coverage=coverage, confidence=1 if coverage == 1 else .5 if coverage else 0,
            evidence=evidence, source_url=check.get("source_url"),
            limitation=limitation if coverage < 1 else None,
        ))
    return tuple(output)


def _ensure_human_action(inn: str, source_code: str) -> CompanyCheckOutcome:
    latest = get_latest_protected_source_check(inn, source_code)
    if latest.get("status") in {None, "not_checked", "expired", "failed", "closed"}:
        latest = start_protected_source_session(inn, source_code)
    return CompanyCheckOutcome(
        check_code=source_code,
        status=(
            CompanyCheckStatus.SUCCESS_FOUND
            if latest.get("status") == "completed" and str(latest.get("result") or "").endswith("_found")
            else CompanyCheckStatus.SUCCESS_NOT_FOUND
            if latest.get("status") == "completed" and str(latest.get("result") or "").endswith("_not_found")
            else CompanyCheckStatus.HUMAN_ACTION_REQUIRED
        ),
        source_code=source_code,
        checked_at=_as_datetime(latest.get("checked_at")),
        source_url=latest.get("source_url") or PROTECTED_SOURCE_URLS[source_code],
        message=(
            "Официальная проверка завершена."
            if latest.get("status") == "completed"
            else "Откройте официальный источник и завершите проверку вручную; CAPTCHA автоматически не решается."
        ),
        evidence={"session_id": latest.get("id"), "session_status": latest.get("status")},
    )


def run_company_check(
    inn: str, *, mode: CompanyCheckMode = CompanyCheckMode.QUICK,
    runners: dict[str, Runner] | None = None, persist: bool = True,
    now: datetime | None = None,
    source_results: Iterable[NormalizedCheckResult] | None = None,
) -> CompanyCheckResult:
    now = now or datetime.now(timezone.utc)
    company = get_company_for_web(inn)
    if company is None:
        raise ValueError("Компания отсутствует в master registry")
    outcomes = _cached_outcomes(company)
    if runners is None:
        runners = _default_runners(company)

    if mode in {CompanyCheckMode.FULL, CompanyCheckMode.REFRESH_DUE}:
        due = {
            item.check_code for item in outcomes
            if item.status in {
                CompanyCheckStatus.STALE, CompanyCheckStatus.UNAVAILABLE,
                CompanyCheckStatus.ERROR, CompanyCheckStatus.SOURCE_BLOCKED,
            }
        }
        for code, runner in runners.items():
            if mode == CompanyCheckMode.REFRESH_DUE and code not in due:
                continue
            try:
                result = runner(inn)
                outcomes = [item for item in outcomes if item.check_code != code]
                outcomes.append(_outcome(code, str(result.get("source") or code), result))
            except Exception as error:  # fail closed by contract
                outcomes = [item for item in outcomes if item.check_code != code]
                outcomes.append(CompanyCheckOutcome(
                    check_code=code, status=CompanyCheckStatus.ERROR,
                    source_code=code, message="Источник вернул ошибку; отрицательный результат не сформирован.",
                    evidence={"error_type": type(error).__name__},
                ))
        if mode == CompanyCheckMode.FULL:
            for source_code in ("fssp", "fns_bankinform", "cbr_zsk"):
                outcomes = [item for item in outcomes if item.check_code != source_code]
                outcomes.append(_ensure_human_action(inn, source_code))

    refreshed = get_company_for_web(inn) or company
    if persist:
        risk = calculate_company_risk(refreshed, force_recalculate=True)
        summary = generate_company_summary(inn)
    else:
        risk = build_risk_assessment(refreshed, datasets=get_dataset_states(), now=now)
        summary = build_summary(risk, now=now)
    normalized = (*_normalize_legacy_company(refreshed, now), *(source_results or ()))
    resolved = apply_capability_applicability(
        DEFAULT_RESOLVER.resolve(normalized), company=refreshed, now=now,
    )
    risk_v3 = build_risk_v3(resolved, profile=_profile(refreshed))
    summary_v3 = build_summary_v3(risk_v3, resolved)
    if persist:
        risk_v3, summary_v3, _, _ = persist_v3_assessment(
            inn, resolved, profile=_profile(refreshed), now=now,
        )
    completed_at = datetime.now(timezone.utc)
    queue = tuple(item for item in outcomes if item.status == CompanyCheckStatus.HUMAN_ACTION_REQUIRED)
    return CompanyCheckResult(
        run_id=str(uuid4()), company_inn=inn, mode=mode,
        started_at=now, completed_at=completed_at,
        outcomes=tuple(sorted(outcomes, key=lambda item: item.check_code)),
        risk=risk, summary=summary, human_action_queue=queue,
        normalized_results=tuple(resolved.values()), coverage_v2=risk_v3.coverage,
        risk_v3=risk_v3, summary_v3=summary_v3,
    )
