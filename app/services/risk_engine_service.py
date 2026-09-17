"""Explainable, on-demand Risk Engine over normalized product contracts."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import select

from app.aggregators.company_product_aggregator import get_company_for_web
from app.contracts.report import DealContext
from app.contracts.risk import (
    ApplicabilityStatus,
    ChangeOrigin,
    RiskAssessmentResult,
    RiskCategory,
    RiskCompleteness,
    RiskConfidence,
    RiskOverallStatus,
    RiskProfile,
    RiskRule,
    RiskSectionAssessment,
    RiskSeverity,
    RiskSignal,
    RiskSignalStatus,
)
from app.database.postgres import get_session
from app.models.company import Company
from app.models.company_fact import CompanyPublicFact
from app.models.risk import CompanyRiskAssessment
from app.models.source import DataSet


ENGINE_VERSION = "risk-engine-2.0.1"
RULESET_PATH = Path(__file__).resolve().parent.parent / "risk_rules" / "v2.json"
CORE_CATEGORIES = {
    RiskCategory.REGISTRATION,
    RiskCategory.OWNERSHIP_MANAGEMENT,
    RiskCategory.FINANCE,
    RiskCategory.TAXES,
    RiskCategory.ENFORCEMENT,
    RiskCategory.BANKRUPTCY,
    RiskCategory.LITIGATION,
    RiskCategory.COMPLIANCE,
}
SEVERITY_ORDER = {
    RiskSeverity.NONE: 0,
    RiskSeverity.LOW: 1,
    RiskSeverity.MEDIUM: 2,
    RiskSeverity.HIGH: 3,
    RiskSeverity.CRITICAL: 4,
}
STATUS_ORDER = {
    RiskSignalStatus.NOT_APPLICABLE: 0,
    RiskSignalStatus.NO_RISK_FOUND: 1,
    RiskSignalStatus.INFO: 2,
    RiskSignalStatus.NOT_CHECKED: 3,
    RiskSignalStatus.UNAVAILABLE: 4,
    RiskSignalStatus.STALE: 5,
    RiskSignalStatus.PARTIAL_COVERAGE: 6,
    RiskSignalStatus.DATA_QUALITY_REVIEW_REQUIRED: 6,
    RiskSignalStatus.WARNING: 7,
    RiskSignalStatus.CONFIRMED_RISK: 8,
}


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime, Decimal)):
        return value.isoformat() if not isinstance(value, Decimal) else str(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"Unsupported JSON value: {type(value)!r}")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def load_ruleset() -> tuple[str, str, dict[str, RiskRule]]:
    raw = RULESET_PATH.read_bytes()
    payload = json.loads(raw)
    rules = {item["rule_id"]: RiskRule.model_validate(item) for item in payload["rules"]}
    return payload["ruleset_version"], hashlib.sha256(raw).hexdigest(), rules


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
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


def _source_as_of(check: dict[str, Any], dataset: dict[str, Any]) -> datetime | None:
    return _dt(dataset.get("source_as_of") or check.get("data_date"))


def _checked_at(check: dict[str, Any], dataset: dict[str, Any]) -> datetime | None:
    return _dt(check.get("checked_at") or dataset.get("checked_at") or check.get("data_date"))


def resolve_profile(company: dict[str, Any], *, now: datetime) -> RiskProfile:
    inn = str(company.get("inn") or "")
    if len(inn) == 12 or company.get("entity_type") == "individual_entrepreneur":
        return RiskProfile.IP
    finorg = company.get("cbr_finorg_check") or {}
    if finorg.get("result") == "found":
        return RiskProfile.FINANCIAL_ORG
    legal_form = " ".join(str(company.get(key) or "") for key in ("name", "full_name")).upper()
    if any(value in legal_form for value in ("НЕКОММЕРЧ", "ФОНД", "АССОЦИАЦ", "АНО ")):
        return RiskProfile.NON_PROFIT
    registered = company.get("registration_date")
    if isinstance(registered, str):
        try:
            registered = date.fromisoformat(registered)
        except ValueError:
            try:
                registered = date(int(registered), 1, 1)
            except (TypeError, ValueError):
                registered = None
    elif isinstance(registered, int) and 1000 <= registered <= now.year:
        registered = date(registered, 1, 1)
    elif not isinstance(registered, date):
        registered = None
    if registered and (now.date() - registered).days < 365:
        return RiskProfile.NEW_COMPANY
    return RiskProfile.GENERAL_LE


def _dataset_state(datasets: dict[str, dict[str, Any]], code: str) -> dict[str, Any]:
    return datasets.get(code) or {}


def _availability_status(check: dict[str, Any], dataset: dict[str, Any]) -> tuple[RiskSignalStatus, str, str]:
    operational = str(dataset.get("operational_status") or "").lower()
    auto = str(dataset.get("auto_update_status") or "").lower()
    result = check.get("result")
    if result == "not_applicable":
        return RiskSignalStatus.NOT_APPLICABLE, "not_applicable", "not_applicable"
    if operational == "stale":
        return RiskSignalStatus.STALE, "known_dataset", "stale"
    if operational in {"source_blocked", "access_pending", "error", "unavailable"}:
        return RiskSignalStatus.UNAVAILABLE, operational, operational
    if result == "unavailable":
        reason = str(check.get("reason") or "unavailable")
        if reason in {"not_checked", "dataset_not_registered", "source_not_connected"}:
            return RiskSignalStatus.NOT_CHECKED, "not_checked", "unknown"
        return RiskSignalStatus.UNAVAILABLE, "unavailable", "unknown"
    return RiskSignalStatus.INFO, "known_dataset", operational or auto or "unknown"


def _signal(
    *, now: datetime, code: str, category: RiskCategory, status: RiskSignalStatus,
    severity: RiskSeverity, confidence: RiskConfidence, title: str, explanation: str,
    source_code: str, dataset_code: str, evidence_ref: str, coverage: str,
    freshness: str, rule: RiskRule, observed_value: Any = None,
    period: dict[str, Any] | None = None, threshold: dict[str, Any] | None = None,
    calculation: str | None = None, source_as_of: datetime | None = None,
    checked_at: datetime | None = None,
) -> RiskSignal:
    return RiskSignal(
        signal_code=code, category=category, status=status, severity=severity,
        confidence=confidence, title=title, explanation=explanation,
        source_code=source_code, dataset_code=dataset_code,
        evidence_refs=(evidence_ref,), observed_value=observed_value, period=period,
        threshold=threshold, calculation=calculation, source_as_of=source_as_of,
        checked_at=checked_at, calculated_at=now, coverage=coverage,
        freshness=freshness, rule_code=rule.rule_id, rule_version=rule.rule_version,
    )


def _generic_check_signal(
    *, now: datetime, category: RiskCategory, check_code: str, check: dict[str, Any] | None,
    dataset_code: str, source_code: str, datasets: dict[str, dict[str, Any]], rule: RiskRule,
    found_title: str, found_explanation: str, found_severity: RiskSeverity = RiskSeverity.MEDIUM,
    found_status: RiskSignalStatus = RiskSignalStatus.WARNING,
) -> RiskSignal:
    check = check or {"result": "unavailable", "reason": "not_checked", "checked": False}
    dataset = _dataset_state(datasets, dataset_code)
    availability, coverage, freshness = _availability_status(check, dataset)
    source_as_of = _source_as_of(check, dataset)
    checked_at = _checked_at(check, dataset)
    if check.get("result") == "found" and availability not in {RiskSignalStatus.STALE, RiskSignalStatus.UNAVAILABLE}:
        status, severity, confidence = found_status, found_severity, RiskConfidence.HIGH
        title, explanation = found_title, found_explanation
    elif check.get("result") == "not_found" and availability not in {RiskSignalStatus.STALE, RiskSignalStatus.UNAVAILABLE}:
        status, severity, confidence = RiskSignalStatus.NO_RISK_FOUND, RiskSeverity.NONE, RiskConfidence.HIGH
        coverage = "known_dataset"
        title = "Сведения по проверке не найдены"
        explanation = "Источник успешно проверен в указанном покрытии и на указанную дату; вывод не распространяется за его пределы."
    elif availability == RiskSignalStatus.NOT_APPLICABLE:
        status, severity, confidence = availability, RiskSeverity.NONE, RiskConfidence.NONE
        title = "Проверка неприменима"
        explanation = str(check.get("reason") or "Проверка не относится к профилю компании или контексту сделки.")
    else:
        status, severity = availability, RiskSeverity.NONE
        confidence = RiskConfidence.NONE if status in {RiskSignalStatus.NOT_CHECKED, RiskSignalStatus.UNAVAILABLE} else RiskConfidence.LOW
        title = "Проверка не дала актуального полного результата"
        reason = str(check.get("reason") or dataset.get("last_error") or "")
        explanation = {
            "not_checked": "Проверка ещё не выполнена.",
            "access_pending": "Для проверки требуется официальный доступ.",
            "source_blocked": "Официальный источник не позволил завершить проверку.",
            "challenge_required": "Требуется ручное подтверждение на официальном сайте.",
            "waiting_for_user": "Требуется ручное подтверждение на официальном сайте.",
        }.get(reason, "Источник не был надёжно проверен.")
    return _signal(
        now=now, code=check_code, category=category, status=status, severity=severity,
        confidence=confidence, title=title, explanation=explanation,
        source_code=source_code, dataset_code=dataset_code,
        evidence_ref=f"{dataset_code}:{check.get('data_date') or check.get('checked_at') or 'none'}",
        coverage=coverage, freshness=freshness, rule=rule,
        observed_value={k: v for k, v in check.items() if k not in {"items", "records", "cases"}},
        source_as_of=source_as_of, checked_at=checked_at,
    )


def _registration_signal(company: dict[str, Any], datasets: dict[str, dict[str, Any]], now: datetime, rule: RiskRule) -> RiskSignal:
    status = str(company.get("status") or "").upper()
    data_date = _dt(company.get("master_data_date"))
    active = status in set(rule.thresholds.get("active_values") or ())
    signal_status = (
        RiskSignalStatus.NO_RISK_FOUND if active
        else RiskSignalStatus.WARNING if status
        else RiskSignalStatus.NOT_CHECKED
    )
    severity = RiskSeverity.NONE if active or not status else RiskSeverity.HIGH
    return _signal(
        now=now, code="registration.status", category=RiskCategory.REGISTRATION,
        status=signal_status, severity=severity,
        confidence=RiskConfidence.HIGH if status else RiskConfidence.NONE,
        title="Компания действует" if active else ("Регистрационный статус требует внимания" if status else "Регистрационный статус не определён"),
        explanation=(f"Статус в master registry: {status}." if status else "Регистрационный статус отсутствует."),
        source_code="master_registry", dataset_code="master_registry",
        evidence_ref=f"company:{company.get('id')}:{company.get('master_data_date')}",
        coverage="known_dataset" if status else "not_checked",
        freshness="dataset_dated" if data_date else "unknown", rule=rule,
        observed_value=status or None, source_as_of=data_date, checked_at=now,
    )


def _finance_signal(company: dict[str, Any], datasets: dict[str, dict[str, Any]], now: datetime, rule: RiskRule) -> RiskSignal:
    check = company.get("revenue_expense_check") or {"result": "unavailable", "reason": "not_checked"}
    dataset = _dataset_state(datasets, str(check.get("dataset_code") or "fns_revenue_expenses"))
    availability, coverage, freshness = _availability_status(check, dataset)
    if check.get("result") == "found" and availability != RiskSignalStatus.STALE:
        profit = _decimal(check.get("calculated_difference"))
        is_loss = profit is not None and profit < 0
        status = RiskSignalStatus.WARNING if is_loss else RiskSignalStatus.INFO
        severity = RiskSeverity.MEDIUM if is_loss else RiskSeverity.NONE
        explanation = (
            f"Выручка {check.get('revenue')} ₽, расходы {check.get('expenses')} ₽, "
            f"разница {check.get('calculated_difference')} ₽ за {check.get('data_year')}."
        )
    else:
        status, severity = availability, RiskSeverity.NONE
        explanation = str(check.get("reason") or "Финансовые данные недоступны или не применимы.")
    return _signal(
        now=now, code="finance.revenue_expense", category=RiskCategory.FINANCE,
        status=status, severity=severity,
        confidence=RiskConfidence.HIGH if check.get("result") == "found" and status != RiskSignalStatus.STALE else RiskConfidence.NONE,
        title="Финансовый результат за опубликованный период", explanation=explanation,
        source_code=str(check.get("source") or "fns"),
        dataset_code=str(check.get("dataset_code") or "fns_revenue_expenses"),
        evidence_ref=f"revexp:{check.get('source_document_id') or check.get('data_date') or 'none'}",
        coverage=coverage, freshness=freshness, rule=rule,
        observed_value={"revenue": check.get("revenue"), "expenses": check.get("expenses"), "profit_loss": check.get("calculated_difference")},
        period={"year": check.get("data_year")}, source_as_of=_source_as_of(check, dataset), checked_at=_checked_at(check, dataset),
    )


def _tax_debt_signals(company: dict[str, Any], datasets: dict[str, dict[str, Any]], now: datetime, rule: RiskRule) -> list[RiskSignal]:
    check = company.get("tax_debt_check") or {"result": "unavailable", "reason": "not_checked"}
    dataset_code = str(check.get("dataset_code") or "fns_tax_debt")
    dataset = _dataset_state(datasets, dataset_code)
    availability, coverage, freshness = _availability_status(check, dataset)
    debt = _decimal(check.get("total_debt")) or Decimal("0")
    rev = _decimal((company.get("revenue_expense_check") or {}).get("revenue"))
    ratio = debt / rev if rev and rev > 0 else None
    threshold = {key: str(value) for key, value in rule.thresholds.items()}
    quality_review = (
        ratio is not None
        and ratio >= Decimal(str(rule.thresholds["ratio_quality_review"]))
    )
    if check.get("result") == "found" and check.get("has_debt") and availability != RiskSignalStatus.STALE:
        attention = debt >= Decimal(str(rule.thresholds["attention_absolute_rub"])) or (
            ratio is not None and ratio >= Decimal(str(rule.thresholds["attention_ratio"]))
        )
        high = (
            debt >= Decimal(str(rule.thresholds["high_absolute_rub"]))
            and ratio is not None
            and ratio >= Decimal(str(rule.thresholds["high_ratio"]))
            and not quality_review
        )
        severity = RiskSeverity.HIGH if high else RiskSeverity.MEDIUM if attention else RiskSeverity.LOW
        explanation = f"Налоговая задолженность: {debt} ₽ на {check.get('data_date')}."
        if ratio is not None:
            explanation += f" Выручка: {rev} ₽; отношение задолженности к выручке: {(ratio * 100).quantize(Decimal('0.01'))}%."
            if quality_review:
                explanation += " Экстремальное отношение требует проверки исходных сумм и периодов; оно не используется как единственное основание высокой тяжести."
        else:
            explanation += " Относительная материальность не рассчитана: валидный знаменатель выручки отсутствует."
        status = RiskSignalStatus.CONFIRMED_RISK if high else RiskSignalStatus.WARNING
        confidence = RiskConfidence.HIGH
    elif check.get("result") in {"found", "not_found"} and availability != RiskSignalStatus.STALE:
        status, severity, confidence, coverage = RiskSignalStatus.NO_RISK_FOUND, RiskSeverity.NONE, RiskConfidence.HIGH, "known_dataset"
        explanation = f"Положительная сумма задолженности не найдена в срезе на {check.get('data_date')}."
    else:
        status, severity = availability, RiskSeverity.NONE
        confidence = RiskConfidence.NONE if status != RiskSignalStatus.STALE else RiskConfidence.LOW
        explanation = str(check.get("reason") or "Проверка задолженности недоступна.")
    debt_signal = _signal(
        now=now, code="tax.debt", category=RiskCategory.TAXES, status=status,
        severity=severity, confidence=confidence, title="Налоговая задолженность",
        explanation=explanation, source_code=str(check.get("source") or "fns"),
        dataset_code=dataset_code, evidence_ref=f"tax-debt:{check.get('snapshot_id') or check.get('data_date') or 'none'}",
        coverage=coverage, freshness=freshness, rule=rule, observed_value=str(debt),
        period={"source_as_of": str(check.get("data_date"))}, threshold=threshold,
        calculation=(f"{debt} / {rev} = {ratio}" if ratio is not None else None),
        source_as_of=_source_as_of(check, dataset), checked_at=_checked_at(check, dataset),
    )
    result = [debt_signal]
    if quality_review:
        result.append(_signal(
            now=now, code="data_quality.tax_debt_ratio", category=RiskCategory.TAXES,
            status=RiskSignalStatus.DATA_QUALITY_REVIEW_REQUIRED,
            severity=RiskSeverity.NONE, confidence=RiskConfidence.LOW,
            title="Требуется проверка исходных финансовых данных",
            explanation=(
                "Отношение налоговой задолженности к выручке превышает контрольный "
                "порог. До сверки источника, единиц и периодов отношение не является "
                "самостоятельным основанием высокой тяжести."
            ),
            source_code=str(check.get("source") or "fns"), dataset_code=dataset_code,
            evidence_ref=f"data-quality:tax-debt:{check.get('snapshot_id') or check.get('data_date') or 'none'}",
            coverage="quality_review", freshness=freshness, rule=rule,
            observed_value={"numerator": str(debt), "denominator": str(rev), "ratio": str(ratio)},
            period={
                "tax_data_date": str(check.get("data_date")),
                "revenue_year": (company.get("revenue_expense_check") or {}).get("data_year"),
            },
            threshold={"ratio_quality_review": str(rule.thresholds["ratio_quality_review"])},
            calculation=f"{debt} / {rev} = {ratio}",
            source_as_of=_source_as_of(check, dataset), checked_at=_checked_at(check, dataset),
        ))
    return result


def _bankruptcy_signal(company: dict[str, Any], now: datetime, rule: RiskRule) -> RiskSignal:
    events = list(company.get("legal_events") or [])
    active = [event for event in events if event.get("bankruptcy_procedure_confirmed") and event.get("status") not in {"completed", "terminated", "closed"}]
    liquidation = [event for event in events if event.get("liquidation_event")]
    if active:
        event = active[0]
        return _signal(
            now=now, code="bankruptcy.active_procedure", category=RiskCategory.BANKRUPTCY,
            status=RiskSignalStatus.CONFIRMED_RISK, severity=RiskSeverity.CRITICAL,
            confidence=RiskConfidence.HIGH, title="Подтверждена процедура банкротства",
            explanation=f"Событие {event.get('event_type')} от {event.get('event_date')} подтверждено источником {event.get('source')}. Ликвидация оценивается отдельно.",
            source_code=str(event.get("source")), dataset_code=str(event.get("dataset_code") or "company_legal_events"),
            evidence_ref=f"legal-event:{event.get('source_identifier')}", coverage="known_dataset", freshness="event_dated",
            rule=rule, observed_value=event, source_as_of=_dt(event.get("publication_date") or event.get("event_date")),
            checked_at=_dt(event.get("checked_at")),
        )
    if liquidation:
        event = liquidation[0]
        return _signal(
            now=now, code="bankruptcy.liquidation_separate", category=RiskCategory.BANKRUPTCY,
            status=RiskSignalStatus.WARNING, severity=RiskSeverity.HIGH, confidence=RiskConfidence.HIGH,
            title="Есть отдельное событие ликвидации",
            explanation="Ликвидация не равна банкротству; событие показано отдельно без подмены правовой семантики.",
            source_code=str(event.get("source")), dataset_code=str(event.get("dataset_code") or "company_legal_events"),
            evidence_ref=f"legal-event:{event.get('source_identifier')}", coverage="known_dataset", freshness="event_dated",
            rule=rule, observed_value=event, source_as_of=_dt(event.get("publication_date") or event.get("event_date")), checked_at=_dt(event.get("checked_at")),
        )
    check = company.get("bankruptcy_check") or {}
    if check.get("result") == "not_found" and check.get("checked"):
        return _signal(
            now=now, code="bankruptcy.official_check", category=RiskCategory.BANKRUPTCY,
            status=RiskSignalStatus.NO_RISK_FOUND, severity=RiskSeverity.NONE,
            confidence=RiskConfidence.HIGH,
            title="Действующая процедура банкротства не найдена",
            explanation="Официальный источник проверен по точному идентификатору; вывод ограничен датой проверки.",
            source_code=str(check.get("source") or "fedresurs"),
            dataset_code=str(check.get("dataset_code") or "fedresurs_bankruptcy"),
            evidence_ref=f"bankruptcy-check:{check.get('evidence_id') or check.get('checked_at')}",
            coverage="targeted_complete", freshness="checked", rule=rule,
            observed_value={"result": "not_found"},
            source_as_of=_dt(check.get("source_as_of")), checked_at=_dt(check.get("checked_at")),
        )
    reason = str(check.get("reason") or "source_not_connected")
    status = (
        RiskSignalStatus.UNAVAILABLE
        if reason in {"source_blocked", "access_pending", "error", "unavailable"}
        else RiskSignalStatus.NOT_CHECKED
    )
    return _signal(
        now=now, code="bankruptcy.source_not_connected", category=RiskCategory.BANKRUPTCY,
        status=status, severity=RiskSeverity.NONE, confidence=RiskConfidence.NONE,
        title="Проверка банкротства не завершена",
        explanation="Официальная проверка ЕФРСБ не завершена; отсутствие локальных событий не означает отсутствие процедуры.",
        source_code="fedresurs", dataset_code="fedresurs_bankruptcy",
        evidence_ref="inventory:FEDRESURS_STAGE_1_5_INVENTORY.md", coverage=reason, freshness=reason, rule=rule,
    )


def _fssp_signal(company: dict[str, Any], datasets: dict[str, dict[str, Any]], now: datetime, rule: RiskRule) -> RiskSignal:
    check = company.get("fssp_check") or {
        "result": "unavailable", "reason": "not_checked", "checked": False,
    }
    return _generic_check_signal(
        now=now, category=RiskCategory.ENFORCEMENT, check_code="enforcement.fssp",
        check=check, dataset_code="fssp_enforcement", source_code="fssp",
        datasets=datasets, rule=rule,
        found_title="Найдены исполнительные производства",
        found_explanation=(
            "Официальный результат ФССП содержит исполнительные производства. "
            "Тяжесть оценивается только по опубликованным сумме, количеству, статусу и дате."
        ),
        found_severity=RiskSeverity.MEDIUM,
        found_status=RiskSignalStatus.WARNING,
    )


def _litigation_signals(company: dict[str, Any], datasets: dict[str, dict[str, Any]], now: datetime, rule: RiskRule) -> list[RiskSignal]:
    result: list[RiskSignal] = []
    arbitration = company.get("arbitration_court_check") or {"result": "unavailable", "reason": "not_checked"}
    dataset = _dataset_state(datasets, "checko_arbitration_cases")
    availability, coverage, freshness = _availability_status(arbitration, dataset)
    if arbitration.get("result") == "found" and availability != RiskSignalStatus.STALE:
        metrics = arbitration.get("signals") or {}
        loaded = arbitration.get("loaded_case_count", len(arbitration.get("cases") or []))
        total = arbitration.get("reported_total_cases")
        complete = bool(arbitration.get("coverage_complete"))
        status = RiskSignalStatus.WARNING if complete else RiskSignalStatus.PARTIAL_COVERAGE
        explanation = f"Загружено дел: {loaded}; provider reported total: {total}."
        if not complete:
            explanation += " Выборка частичная и не выдаётся за полный период."
        explanation += " Сумма иска не является подтверждённым долгом."
        result.append(_signal(
            now=now, code="litigation.arbitration", category=RiskCategory.LITIGATION,
            status=status, severity=RiskSeverity.MEDIUM, confidence=RiskConfidence.HIGH if complete else RiskConfidence.MEDIUM,
            title="Арбитражная активность", explanation=explanation,
            source_code=str(arbitration.get("source") or "checko_legal_cases"), dataset_code="checko_arbitration_cases",
            evidence_ref=f"arbitration:{arbitration.get('data_date') or 'none'}", coverage="complete" if complete else "partial",
            freshness=freshness, rule=rule,
            observed_value={"reported_total_cases": total, "loaded_case_count": loaded, "coverage_complete": complete, "metrics": metrics},
            period=(metrics.get("loaded_case_count") or {}).get("period"), source_as_of=_source_as_of(arbitration, dataset), checked_at=_checked_at(arbitration, dataset),
        ))
    else:
        result.append(_generic_check_signal(
            now=now, category=RiskCategory.LITIGATION, check_code="litigation.arbitration",
            check=arbitration, dataset_code="checko_arbitration_cases", source_code="checko_legal_cases",
            datasets=datasets, rule=rule, found_title="Арбитражная активность", found_explanation="Найдены арбитражные дела.",
        ))
    general = company.get("general_court_check") or {"result": "unavailable", "reason": "not_checked"}
    signal = _generic_check_signal(
        now=now, category=RiskCategory.LITIGATION, check_code="litigation.general_courts",
        check=general, dataset_code="moscow_general_court_cases", source_code="moscow_courts_official",
        datasets=datasets, rule=rule, found_title="Найдены дела судов общей юрисдикции",
        found_explanation="Результат имеет ограниченное покрытие по сохранённым регионам и официальным порталам.",
    )
    if general.get("result") in {"found", "not_found"}:
        signal = signal.model_copy(update={
            "status": RiskSignalStatus.PARTIAL_COVERAGE,
            "coverage": "partial_targeted",
            "confidence": RiskConfidence.MEDIUM,
            "severity": RiskSeverity.NONE,
            "explanation": signal.explanation + (
                " Найденные дела показаны как факты, но без доказанной суммы и "
                "материальности сами по себе не повышают итоговый бизнес-риск. "
                "Нельзя утверждать отсутствие дел за пределами проверенных порталов и регионов."
            ),
        })
    result.append(signal)
    return result


def _section(category: RiskCategory, signals: Iterable[RiskSignal]) -> RiskSectionAssessment:
    items = tuple(signals)
    applicability = (
        ApplicabilityStatus.NOT_APPLICABLE if items and all(s.status == RiskSignalStatus.NOT_APPLICABLE for s in items)
        else ApplicabilityStatus.UNKNOWN_UNAVAILABLE if items and all(s.status in {RiskSignalStatus.NOT_CHECKED, RiskSignalStatus.UNAVAILABLE} for s in items)
        else ApplicabilityStatus.APPLICABLE
    )
    status = max(items, key=lambda item: STATUS_ORDER[item.status]).status
    severity = max((item.severity for item in items), key=lambda value: SEVERITY_ORDER[value])
    confidence = min((item.confidence for item in items), key=lambda value: {RiskConfidence.NONE: 0, RiskConfidence.LOW: 1, RiskConfidence.MEDIUM: 2, RiskConfidence.HIGH: 3}[value])
    counts = {
        "completed_checks": sum(s.status in {RiskSignalStatus.CONFIRMED_RISK, RiskSignalStatus.WARNING, RiskSignalStatus.INFO, RiskSignalStatus.NO_RISK_FOUND} for s in items),
        "unavailable_checks": sum(s.status == RiskSignalStatus.UNAVAILABLE for s in items),
        "not_checked_checks": sum(s.status == RiskSignalStatus.NOT_CHECKED for s in items),
        "stale_checks": sum(s.status == RiskSignalStatus.STALE for s in items),
        "partial_checks": sum(s.status == RiskSignalStatus.PARTIAL_COVERAGE for s in items),
    }
    return RiskSectionAssessment(
        section_code=category, applicability=applicability, status=status, severity=severity,
        confidence=confidence, headline=items[0].title if len(items) == 1 else f"Проверок в разделе: {len(items)}",
        explanation="Рисковые факты и полнота источников оцениваются раздельно; недоступность источника не уменьшает известный риск.",
        signal_codes=tuple(item.signal_code for item in items), **counts,
    )


def _context_applicable(company: dict[str, Any], deal_context: DealContext | None, keywords: tuple[str, ...]) -> bool:
    text = " ".join(str(company.get(key) or "") for key in ("activity", "okved"))
    if deal_context:
        text += " " + " ".join(str(value or "") for value in (deal_context.subject, deal_context.activity_description))
    text = text.lower()
    return any(keyword in text for keyword in keywords)


def build_risk_assessment(
    company: dict[str, Any], *, datasets: dict[str, dict[str, Any]] | None = None,
    deal_context: DealContext | None = None, now: datetime | None = None,
    assessment_id: str | None = None, change_origin: ChangeOrigin = ChangeOrigin.SOURCE_CHANGE,
) -> RiskAssessmentResult:
    """Pure calculation over normalized/cached facts. No provider or raw-page access."""
    now = now or datetime.now(timezone.utc)
    datasets = datasets or {}
    ruleset_version, ruleset_hash, rules = load_ruleset()
    profile = resolve_profile(company, now=now)
    signals: list[RiskSignal] = []
    signals.append(_registration_signal(company, datasets, now, rules["REGISTRATION_STATUS"]))

    disqualified = company.get("disqualified_check")
    signals.append(_generic_check_signal(
        now=now, category=RiskCategory.OWNERSHIP_MANAGEMENT, check_code="management.disqualified_record",
        check=disqualified, dataset_code="fns_disqualified", source_code="fns",
        datasets=datasets, rule=rules["PROTECTED_REGULATORY_CHECK"],
        found_title="Организация указана в записи о дисквалифицированном лице",
        found_explanation="Связь с организацией не доказывает, что лицо является текущим директором; сильный вывод без надёжной identity link запрещён.",
        found_severity=RiskSeverity.MEDIUM,
    ))
    address_facts = [
        fact for fact in (company.get("company_public_facts") or [])
        if fact.get("fact_type") == "mass_address"
    ]
    if address_facts:
        fact = address_facts[0]
        value = fact.get("value") or {}
        signals.append(_signal(
            now=now, code="management.address_context", category=RiskCategory.OWNERSHIP_MANAGEMENT,
            status=RiskSignalStatus.INFO, severity=RiskSeverity.LOW, confidence=RiskConfidence.MEDIUM,
            title="Контекст концентрации по адресу",
            explanation=(
                f"Действующих компаний по точному нормализованному адресу: "
                f"{value.get('exact_full_address_active_count')}. Это контекст, а не автоматический вывод о нарушении; бизнес-центр сам по себе не является риском."
            ),
            source_code=str(fact.get("source_code") or "master_registry"),
            dataset_code=str(fact.get("dataset_code") or "company_public_facts"),
            evidence_ref=f"company-fact:{fact.get('id') or fact.get('source_identifier')}",
            coverage=str(value.get("coverage") or "partial"), freshness=str(fact.get("currentness") or "unknown"),
            rule=rules["PROTECTED_REGULATORY_CHECK"], observed_value=value,
            threshold={"classification": "context_only", "rule_version": rules["PROTECTED_REGULATORY_CHECK"].rule_version},
            source_as_of=_dt(fact.get("publication_date")), checked_at=_dt(fact.get("observed_at")),
        ))
    signals.append(_finance_signal(company, datasets, now, rules["FINANCIAL_RESULT"]))
    signals.extend(_tax_debt_signals(company, datasets, now, rules["TAX_DEBT_TIERED"]))
    signals.append(_generic_check_signal(
        now=now, category=RiskCategory.TAXES, check_code="tax.offence",
        check=company.get("tax_offence_check"), dataset_code="fns_tax_offence", source_code="fns",
        datasets=datasets, rule=rules["TAX_OFFENCE_PRESENT"],
        found_title="Опубликованы сведения о налоговом правонарушении",
        found_explanation="Показаны только опубликованные дата и сумма штрафа; вид нарушения источник не доказывает.",
        found_severity=RiskSeverity.MEDIUM,
    ))
    signals.append(_fssp_signal(company, datasets, now, rules["PROTECTED_REGULATORY_CHECK"]))
    signals.append(_bankruptcy_signal(company, now, rules["ACTIVE_BANKRUPTCY_PROCEDURE"]))
    signals.extend(_litigation_signals(company, datasets, now, rules["COURT_ACTIVITY"]))

    procurement_applicable = _context_applicable(company, deal_context, ("закуп", "тендер", "44-фз", "223-фз"))
    signals.append(_signal(
        now=now, code="procurement.rnp_access", category=RiskCategory.PROCUREMENT,
        status=RiskSignalStatus.UNAVAILABLE if procurement_applicable else RiskSignalStatus.NOT_APPLICABLE,
        severity=RiskSeverity.NONE, confidence=RiskConfidence.NONE,
        title="Проверка РНП ожидает официальный доступ" if procurement_applicable else "Закупочный контекст не установлен",
        explanation="EIS RNP remains ACCESS_PENDING; отсутствие результата не является отсутствием записи." if procurement_applicable else "DealContext и деятельность не активировали закупочную проверку.",
        source_code="eis", dataset_code="eis_rnp", evidence_ref="dataset:eis_rnp",
        coverage="access_pending" if procurement_applicable else "not_applicable",
        freshness="access_pending" if procurement_applicable else "not_applicable", rule=rules["PROTECTED_REGULATORY_CHECK"],
    ))

    licence_applicable = _context_applicable(company, deal_context, ("строит", "медицин", "здравоохран", "архитект", "проектир"))
    if licence_applicable:
        sro_checks = company.get("sro_checks") or {}
        signals.append(_generic_check_signal(
            now=now, category=RiskCategory.LICENCES_REGULATORY, check_code="licence.sro",
            check=sro_checks.get("nostroy"), dataset_code="nostroy_members", source_code="nostroy",
            datasets=datasets, rule=rules["PROTECTED_REGULATORY_CHECK"],
            found_title="Найдены сведения СРО", found_explanation="Применимость к конкретной сделке требует сопоставления вида работ и DealContext.",
            found_severity=RiskSeverity.NONE, found_status=RiskSignalStatus.INFO,
        ))
    else:
        signals.append(_signal(
            now=now, code="licence.context_not_applicable", category=RiskCategory.LICENCES_REGULATORY,
            status=RiskSignalStatus.NOT_APPLICABLE, severity=RiskSeverity.NONE, confidence=RiskConfidence.NONE,
            title="Отраслевые лицензии/SRO не активированы", explanation="Текущие OKVED/activity/DealContext не доказывают применимость отраслевой проверки.",
            source_code="applicability_engine", dataset_code="company_profile", evidence_ref=f"company:{company.get('id')}:profile",
            coverage="not_applicable", freshness="not_applicable", rule=rules["PROTECTED_REGULATORY_CHECK"],
        ))

    signals.append(_generic_check_signal(
        now=now, category=RiskCategory.COMPLIANCE, check_code="compliance.cbr_warning",
        check=company.get("cbr_warning_list_check"), dataset_code="cbr_warning_list", source_code="cbr",
        datasets=datasets, rule=rules["PROTECTED_REGULATORY_CHECK"],
        found_title="Найдены сведения в warning list Банка России",
        found_explanation="Сигнал относится только к опубликованной записи и exact identifier evidence.",
        found_severity=RiskSeverity.HIGH, found_status=RiskSignalStatus.CONFIRMED_RISK,
    ))
    zsk = company.get("cbr_zsk_check") or {}
    zsk_result = zsk.get("result")
    if zsk_result == "high_risk_information_found":
        zsk_check = {"result": "found", "data_date": zsk.get("checked_at"), "checked_at": zsk.get("checked_at")}
    elif zsk_result == "high_risk_information_not_found" and zsk.get("checked"):
        zsk_check = {"result": "not_found", "data_date": zsk.get("checked_at"), "checked_at": zsk.get("checked_at")}
    else:
        zsk_check = {"result": "unavailable", "reason": zsk.get("status") or "not_checked", "checked_at": zsk.get("checked_at")}
    signals.append(_generic_check_signal(
        now=now, category=RiskCategory.COMPLIANCE, check_code="compliance.cbr_zsk",
        check=zsk_check, dataset_code="cbr_zsk", source_code="cbr",
        datasets=datasets, rule=rules["PROTECTED_REGULATORY_CHECK"],
        found_title="Публичная проверка ЦБ содержит сведения высокой степени риска",
        found_explanation="Это результат публичной проверки ZSK, а не полный внутренний scoring Банка России.",
        found_severity=RiskSeverity.HIGH, found_status=RiskSignalStatus.CONFIRMED_RISK,
    ))
    bankinform = company.get("fns_bankinform_check") or {}
    bankinform_result = bankinform.get("result")
    if bankinform_result == "active_suspensions_found":
        bankinform_check = {"result": "found", "data_date": bankinform.get("checked_at"), "checked_at": bankinform.get("checked_at"), "evidence": bankinform.get("evidence")}
    elif bankinform_result == "active_suspensions_not_found" and bankinform.get("checked"):
        bankinform_check = {"result": "not_found", "data_date": bankinform.get("checked_at"), "checked_at": bankinform.get("checked_at")}
    else:
        bankinform_check = {"result": "unavailable", "reason": bankinform.get("status") or "not_checked", "checked_at": bankinform.get("checked_at")}
    signals.append(_generic_check_signal(
        now=now, category=RiskCategory.COMPLIANCE, check_code="compliance.account_suspension",
        check=bankinform_check, dataset_code="fns_account_suspension", source_code="fns_bankinform",
        datasets=datasets, rule=rules["PROTECTED_REGULATORY_CHECK"],
        found_title="Сохранена активная приостановка операций по счетам",
        found_explanation="Сигнал основан только на сохранённом завершённом результате официальной проверки; он не утверждает блокировку конкретного счёта без прямого доказательства.",
        found_severity=RiskSeverity.HIGH, found_status=RiskSignalStatus.CONFIRMED_RISK,
    ))
    signals.append(_signal(
        now=now, code="compliance.roskomnadzor_blocked", category=RiskCategory.COMPLIANCE,
        status=RiskSignalStatus.UNAVAILABLE, severity=RiskSeverity.NONE, confidence=RiskConfidence.NONE,
        title="Часть проверок Роскомнадзора заблокирована источником",
        explanation="Часть официальных реестров Роскомнадзора не позволила завершить проверку; чистый результат не сформирован.",
        source_code="roskomnadzor", dataset_code="rkn_broadcast_licenses+rkn_registered_media",
        evidence_ref="status:W1-005-B-C", coverage="source_blocked", freshness="source_blocked",
        rule=rules["PROTECTED_REGULATORY_CHECK"],
    ))

    sections = tuple(_section(category, (s for s in signals if s.category == category)) for category in RiskCategory)
    applicable = [s for s in signals if s.status != RiskSignalStatus.NOT_APPLICABLE]
    def counts(items: Iterable[RiskSignal]) -> dict[str, int]:
        values = tuple(items)
        return {
            "applicable": len(values),
            "completed": sum(s.status in {RiskSignalStatus.CONFIRMED_RISK, RiskSignalStatus.WARNING, RiskSignalStatus.INFO, RiskSignalStatus.NO_RISK_FOUND} for s in values),
            "not_checked": sum(s.status == RiskSignalStatus.NOT_CHECKED for s in values),
            "unavailable": sum(s.status == RiskSignalStatus.UNAVAILABLE for s in values),
            "stale": sum(s.status == RiskSignalStatus.STALE for s in values),
            "partial": sum(s.status == RiskSignalStatus.PARTIAL_COVERAGE for s in values),
            "data_quality_review": sum(
                s.status == RiskSignalStatus.DATA_QUALITY_REVIEW_REQUIRED for s in values
            ),
        }
    core_counts = counts(s for s in applicable if s.category in CORE_CATEGORIES)
    context_counts = counts(s for s in applicable if s.category not in CORE_CATEGORIES)
    all_counts = counts(applicable)
    completeness = RiskCompleteness(
        total_applicable_checks=len(applicable), completed=all_counts["completed"],
        not_applicable=sum(s.status == RiskSignalStatus.NOT_APPLICABLE for s in signals),
        not_checked=all_counts["not_checked"], unavailable=all_counts["unavailable"],
        stale=all_counts["stale"], partial=all_counts["partial"],
        data_quality_review=all_counts["data_quality_review"],
        core=core_counts, context=context_counts,
    )
    mandatory_codes = {
        "registration.status",
        "finance.revenue_expense",
        "tax.debt",
        "tax.offence",
        "enforcement.fssp",
        "bankruptcy.active_procedure",
        "bankruptcy.official_check",
        "bankruptcy.source_not_connected",
        "compliance.cbr_warning",
        "litigation.arbitration",
        "litigation.general_courts",
    }
    completed_statuses = {
        RiskSignalStatus.CONFIRMED_RISK,
        RiskSignalStatus.WARNING,
        RiskSignalStatus.INFO,
        RiskSignalStatus.NO_RISK_FOUND,
    }
    mandatory = [signal for signal in signals if signal.signal_code in mandatory_codes]
    mandatory_complete = bool(mandatory) and all(
        signal.status in completed_statuses for signal in mandatory
    )
    if any(s.severity == RiskSeverity.CRITICAL for s in signals):
        overall = RiskOverallStatus.CRITICAL
    elif any(s.severity == RiskSeverity.HIGH for s in signals):
        overall = RiskOverallStatus.HIGH
    elif any(s.severity == RiskSeverity.MEDIUM for s in signals):
        overall = RiskOverallStatus.ATTENTION
    elif not mandatory_complete:
        overall = RiskOverallStatus.INSUFFICIENT_DATA
    else:
        overall = RiskOverallStatus.NO_MATERIAL_RISKS
    limitations = tuple(dict.fromkeys(
        s.explanation for s in signals if s.status in {
            RiskSignalStatus.NOT_CHECKED, RiskSignalStatus.UNAVAILABLE,
            RiskSignalStatus.STALE, RiskSignalStatus.PARTIAL_COVERAGE,
            RiskSignalStatus.DATA_QUALITY_REVIEW_REQUIRED,
        }
    ))
    refs = tuple(sorted({ref for signal in signals for ref in signal.evidence_refs}))
    return RiskAssessmentResult(
        assessment_id=assessment_id or str(uuid4()), company_id=int(company["id"]),
        company_inn=str(company["inn"]), profile=profile, risk_engine_version=ENGINE_VERSION,
        ruleset_version=ruleset_version, ruleset_hash=ruleset_hash, calculated_at=now,
        input_snapshot_refs=refs, signals=tuple(signals), section_assessments=sections,
        coverage={
            "core": core_counts,
            "context": context_counts,
            "mandatory_complete": mandatory_complete,
            "mandatory_completed": sum(signal.status in completed_statuses for signal in mandatory),
            "mandatory_total": len(mandatory),
        }, completeness=completeness,
        overall_status=overall, limitations=limitations, change_origin=change_origin,
    )


def get_dataset_states() -> dict[str, dict[str, Any]]:
    with get_session() as session:
        rows = session.scalars(select(DataSet)).all()
        return {
            row.code: {
                "operational_status": row.operational_status,
                "auto_update_status": row.auto_update_status,
                "source_as_of": row.source_as_of,
                "checked_at": row.checked_at,
                "last_success_at": row.last_success_at,
                "coverage": row.coverage,
                "last_error": row.last_error,
            }
            for row in rows
        }


def _resolve_company(company: int | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(company, dict):
        return company
    with get_session() as session:
        if isinstance(company, int):
            inn = session.scalar(select(Company.inn).where(Company.id == company))
        else:
            inn = str(company)
        if not inn:
            raise ValueError("Company not found")
    payload = get_company_for_web(inn)
    if payload is None:
        raise ValueError("Company not found")
    with get_session() as session:
        facts = session.scalars(
            select(CompanyPublicFact).where(CompanyPublicFact.company_id == int(payload["id"]))
        ).all()
        payload["company_public_facts"] = [
            {
                "id": row.id, "fact_type": row.fact_type, "value": row.value,
                "source_code": row.source_code, "source_identifier": row.source_identifier,
                "publication_date": row.publication_date, "currentness": row.currentness,
                "observed_at": row.observed_at,
            }
            for row in facts
        ]
    return payload


def _change_origin(previous: CompanyRiskAssessment | None, *, input_hash: str, context_hash: str, ruleset_hash: str, payload: dict[str, Any]) -> ChangeOrigin:
    if previous is None:
        return ChangeOrigin.SOURCE_CHANGE
    if previous.ruleset_hash != ruleset_hash:
        return ChangeOrigin.RULESET_CHANGE
    if previous.deal_context_hash != context_hash:
        return ChangeOrigin.DEAL_CONTEXT_CHANGE
    prior_domain = dict(previous.input_snapshot or {})
    prior_domain.pop("dataset_states", None)
    current_domain = dict(payload)
    current_domain.pop("dataset_states", None)
    if _hash(prior_domain) == _hash(current_domain) and previous.input_hash != input_hash:
        return ChangeOrigin.COVERAGE_CHANGE
    return ChangeOrigin.SOURCE_CHANGE


def can_reuse_assessment(previous: CompanyRiskAssessment | None, *, input_hash: str, context_hash: str, ruleset_hash: str) -> bool:
    return bool(
        previous is not None
        and previous.input_hash == input_hash
        and previous.deal_context_hash == context_hash
        and previous.ruleset_hash == ruleset_hash
        and previous.risk_engine_version == ENGINE_VERSION
    )


def calculate_company_risk(
    company: int | str | dict[str, Any], *, deal_context: DealContext | None = None,
    force_recalculate: bool = False, now: datetime | None = None,
) -> RiskAssessmentResult:
    """Calculate and persist on demand; reuse only for identical meaningful inputs."""
    company_payload = _resolve_company(company)
    datasets = get_dataset_states()
    snapshot = {"company": company_payload, "dataset_states": datasets}
    input_hash = _hash(snapshot)
    context_payload = deal_context.model_dump(mode="json") if deal_context else {}
    context_hash = _hash(context_payload)
    ruleset_version, ruleset_hash, _ = load_ruleset()
    with get_session() as session:
        previous = session.scalar(
            select(CompanyRiskAssessment)
            .where(CompanyRiskAssessment.company_id == int(company_payload["id"]))
            .order_by(CompanyRiskAssessment.calculated_at.desc(), CompanyRiskAssessment.id.desc())
            .limit(1)
        )
        if not force_recalculate and can_reuse_assessment(
            previous, input_hash=input_hash, context_hash=context_hash, ruleset_hash=ruleset_hash
        ):
            return RiskAssessmentResult.model_validate(previous.result_payload).model_copy(update={"reused": True})
        origin = _change_origin(previous, input_hash=input_hash, context_hash=context_hash, ruleset_hash=ruleset_hash, payload=snapshot)
    result = build_risk_assessment(
        company_payload, datasets=datasets, deal_context=deal_context, now=now,
        change_origin=origin,
    )
    payload = result.model_dump(mode="json")
    with get_session() as session:
        row = CompanyRiskAssessment(
            assessment_id=result.assessment_id, company_id=result.company_id,
            risk_engine_version=ENGINE_VERSION, ruleset_version=ruleset_version,
            ruleset_hash=ruleset_hash, input_hash=input_hash, deal_context_hash=context_hash,
            change_origin=result.change_origin.value, calculated_at=result.calculated_at,
            input_snapshot_refs=list(result.input_snapshot_refs), input_snapshot=json.loads(_canonical(snapshot)),
            signals=payload["signals"], section_assessments=payload["section_assessments"],
            coverage=payload["coverage"], completeness=payload["completeness"],
            overall_status=result.overall_status.value, limitations=list(result.limitations), result_payload=payload,
        )
        session.add(row)
        session.commit()
    return result


def recalculate_company_risk(company: int | str | dict[str, Any], *, deal_context: DealContext | None = None) -> RiskAssessmentResult:
    return calculate_company_risk(company, deal_context=deal_context, force_recalculate=True)


def get_latest_company_risk(inn: str) -> RiskAssessmentResult | None:
    with get_session() as session:
        company_id = session.scalar(select(Company.id).where(Company.inn == inn))
        if company_id is None:
            return None
        row = session.scalar(
            select(CompanyRiskAssessment)
            .where(CompanyRiskAssessment.company_id == company_id)
            .order_by(CompanyRiskAssessment.calculated_at.desc(), CompanyRiskAssessment.id.desc())
            .limit(1)
        )
        return RiskAssessmentResult.model_validate(row.result_payload) if row else None
