"""Bounded on-demand orchestration before Risk Engine v2 calculation.

The orchestrator never converts an error, unavailable source, or human action
into a negative result. QUICK is cache-only. FULL may call explicitly registered
source runners and creates official human-action sessions for protected sources.
REFRESH_DUE calls only runners whose cached result is missing, stale, or failed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
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
    completed_at = datetime.now(timezone.utc)
    queue = tuple(item for item in outcomes if item.status == CompanyCheckStatus.HUMAN_ACTION_REQUIRED)
    return CompanyCheckResult(
        run_id=str(uuid4()), company_inn=inn, mode=mode,
        started_at=now, completed_at=completed_at,
        outcomes=tuple(sorted(outcomes, key=lambda item: item.check_code)),
        risk=risk, summary=summary, human_action_queue=queue,
    )
