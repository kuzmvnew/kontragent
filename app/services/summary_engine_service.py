"""Deterministic Summary Engine over persisted Risk Engine assessments."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.contracts.risk import (
    ChangeOrigin,
    RiskAssessmentResult,
    RiskConfidence,
    RiskSeverity,
    RiskSignal,
    RiskSignalStatus,
    RiskOverallStatus,
)
from app.contracts.summary import (
    MonitoringComparison,
    SummaryMode,
    SummaryResult,
    SummaryStatement,
    SummaryStatementKind,
    SummaryTextBlocks,
)
from app.database.postgres import get_session
from app.models.company import Company
from app.models.risk import CompanyRiskAssessment
from app.models.summary import CompanySummary
from app.services.risk_engine_service import load_ruleset


ENGINE_VERSION = "summary-engine-2.0.1"
PUBLIC_PROJECTION_POLICY_VERSION = "public-projection-unapproved-1.0.0"
DEFAULT_PROJECTION_POLICY_VERSION = "internal-projection-1.0.0"
PRIVATE_MARKERS = ("private_internal", "private-person", "private_person")
SEVERITY_ORDER = {
    RiskSeverity.NONE: 0,
    RiskSeverity.LOW: 1,
    RiskSeverity.MEDIUM: 2,
    RiskSeverity.HIGH: 3,
    RiskSeverity.CRITICAL: 4,
}
CONFIDENCE_ORDER = {
    RiskConfidence.NONE: 0,
    RiskConfidence.LOW: 1,
    RiskConfidence.MEDIUM: 2,
    RiskConfidence.HIGH: 3,
}
FACTOR_STATUSES = {RiskSignalStatus.CONFIRMED_RISK, RiskSignalStatus.WARNING}
LIMITATION_STATUSES = {
    RiskSignalStatus.NOT_CHECKED,
    RiskSignalStatus.UNAVAILABLE,
    RiskSignalStatus.STALE,
    RiskSignalStatus.PARTIAL_COVERAGE,
    RiskSignalStatus.DATA_QUALITY_REVIEW_REQUIRED,
}

OVERALL_LABELS = {
    RiskOverallStatus.CRITICAL: "Критический риск",
    RiskOverallStatus.HIGH: "Высокий риск",
    RiskOverallStatus.ATTENTION: "Требует внимания",
    RiskOverallStatus.NO_MATERIAL_RISKS: "Существенных рисков не выявлено",
    RiskOverallStatus.NO_MATERIAL_SIGNALS: "Существенных рисков не выявлено",
    RiskOverallStatus.INSUFFICIENT_DATA: "Недостаточно данных",
}


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _format_number(value: Any) -> str:
    number = _decimal(value)
    if number is None:
        return str(value)
    rendered = f"{number:,.2f}".replace(",", " ")
    return rendered.rstrip("0").rstrip(".")


def _date_text(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "актуальная дата отсутствует"


def _rule_metadata() -> dict[str, dict[str, Any]]:
    _, _, rules = load_ruleset()
    return {
        code: {"hard_blocker": rule.hard_blocker, "public_visibility": rule.public_visibility}
        for code, rule in rules.items()
    }


def _is_private(signal: RiskSignal) -> bool:
    text = " ".join(
        (signal.source_code, signal.dataset_code, *signal.evidence_refs)
    ).lower()
    return any(marker in text for marker in PRIVATE_MARKERS)


def _materiality(signal: RiskSignal) -> int:
    if signal.calculation:
        return 2
    if _decimal(signal.observed_value) is not None:
        return 1
    if isinstance(signal.observed_value, dict) and any(
        _decimal(value) is not None for value in signal.observed_value.values()
    ):
        return 1
    return 0


def _factor_sort_key(signal: RiskSignal, rules: dict[str, dict[str, Any]]) -> tuple[Any, ...]:
    metadata = rules.get(signal.rule_code, {})
    timestamp = signal.source_as_of or signal.checked_at or signal.calculated_at
    return (
        -int(bool(metadata.get("hard_blocker"))),
        -SEVERITY_ORDER[signal.severity],
        -CONFIDENCE_ORDER[signal.confidence],
        -_materiality(signal),
        -timestamp.timestamp(),
        signal.signal_code,
    )


def _tax_text(signal: RiskSignal) -> str:
    debt = _format_number(signal.observed_value)
    text = f"Налоговая задолженность — {debt} ₽ по данным на {_date_text(signal.source_as_of)}."
    if signal.calculation:
        parts = signal.calculation.split(" = ", 1)
        operands = parts[0].split(" / ", 1)
        ratio = _decimal(parts[1]) if len(parts) == 2 else None
        if len(operands) == 2 and ratio is not None:
            revenue = _format_number(operands[1])
            year = (signal.period or {}).get("revenue_year")
            text += f" Выручка{f' за {year}' if year else ''} — {revenue} ₽. Отношение — {_format_number(ratio * 100)}%."
    else:
        text += " Материальность относительно выручки не рассчитана, поскольку соответствующие финансовые данные недоступны."
    return text


def _court_text(signal: RiskSignal) -> str:
    observed = signal.observed_value if isinstance(signal.observed_value, dict) else {}
    loaded = observed.get("loaded_case_count")
    reported = observed.get("reported_total_cases")
    metrics = observed.get("metrics") if isinstance(observed.get("metrics"), dict) else {}
    amount = None
    for key in (
        "active_defendant_claim_amount", "defendant_claim_amount", "total_claim_amount",
        "claims_amount", "claim_amount",
    ):
        candidate = metrics.get(key)
        if isinstance(candidate, dict):
            candidate = candidate.get("value", candidate.get("numerator"))
        if _decimal(candidate) is not None:
            amount = candidate
            break
    text = f"Арбитражные данные: загружено {loaded if loaded is not None else '—'} дел"
    if reported is not None:
        text += f" из {reported}, заявленных источником"
    text += "."
    if amount is not None:
        text += f" Сумма заявленных требований — {_format_number(amount)} ₽."
    text += " Сумма заявленных требований не является подтверждённым долгом."
    ratio_metric = metrics.get("claims_to_revenue") if isinstance(metrics, dict) else None
    if amount is not None and isinstance(ratio_metric, dict) and ratio_metric.get("denominator") is None:
        text += " Материальность относительно выручки не рассчитана: валидный знаменатель отсутствует в сохранённой метрике."
    if signal.coverage not in {"complete", "targeted_complete"}:
        text += " Арбитражные данные загружены частично."
    return text


def _finance_text(signal: RiskSignal) -> str:
    value = signal.observed_value if isinstance(signal.observed_value, dict) else {}
    year = (signal.period or {}).get("year")
    return (
        f"Выручка — {_format_number(value.get('revenue'))} ₽; расходы — "
        f"{_format_number(value.get('expenses'))} ₽; разница — "
        f"{_format_number(value.get('profit_loss'))} ₽{f' за {year}' if year else ''}."
    )


def _tax_offence_text(signal: RiskSignal) -> str:
    value = signal.observed_value if isinstance(signal.observed_value, dict) else {}
    amount = value.get("fine_amount")
    document_date = value.get("document_date")
    text = "Опубликованы сведения о налоговом правонарушении"
    if amount is not None:
        text += f": сумма штрафа — {_format_number(amount)} ₽"
    if document_date:
        text += f", дата документа — {document_date}"
    return text + ". Вид правонарушения источник не публикует, поэтому он не указан."


def _factor_text(signal: RiskSignal) -> str:
    if signal.signal_code == "tax.debt":
        return _tax_text(signal)
    if signal.signal_code == "litigation.arbitration":
        return _court_text(signal)
    if signal.signal_code == "litigation.general_courts":
        return "Найдены сведения судов общей юрисдикции в проверенных регионах. Покрытие частичное и не является общероссийским."
    if signal.signal_code == "finance.revenue_expense":
        return _finance_text(signal)
    if signal.signal_code == "tax.offence":
        return _tax_offence_text(signal)
    if signal.signal_code == "compliance.cbr_warning":
        return (
            f"В предупредительном списке Банка России на {_date_text(signal.source_as_of)} "
            "найдено совпадение по точному идентификатору. Это опубликованный регуляторный сигнал, а не судебный вывод."
        )
    if signal.signal_code == "compliance.cbr_zsk" and signal.status == RiskSignalStatus.NO_RISK_FOUND:
        return (
            f"В публичной проверке Банка России на {_date_text(signal.checked_at or signal.source_as_of)} "
            "сведения о высокой группе риска не обнаружены."
        )
    if signal.signal_code == "bankruptcy.active_procedure":
        return "Официальный источник подтверждает процедуру банкротства. Это отдельный юридический факт, а не финансовый прогноз."
    if signal.signal_code == "bankruptcy.liquidation_separate":
        return "Зафиксировано событие ликвидации. Ликвидация не приравнивается к банкротству."
    if signal.signal_code == "compliance.account_suspension":
        freshness = ""
        if signal.status == RiskSignalStatus.STALE:
            freshness = f" Последняя сохранённая проверка датирована {_date_text(signal.checked_at)}; актуальность не подтверждена."
        return signal.explanation + freshness
    return signal.explanation


def _limitation_text(signal: RiskSignal) -> str:
    if signal.signal_code == "enforcement.fssp":
        return "ФССП — проверка не завершена, поэтому отсутствие исполнительных производств не подтверждено."
    if signal.signal_code.startswith("bankruptcy."):
        return "ЕФРСБ — проверка не завершена, поэтому отсутствие действующей процедуры не подтверждено."
    if signal.signal_code == "litigation.general_courts":
        return "Проверка судов общей юрисдикции имеет частичное региональное покрытие; отсутствие дел за пределами проверенных регионов не подтверждено."
    if signal.signal_code == "litigation.arbitration" and signal.status == RiskSignalStatus.PARTIAL_COVERAGE:
        return "Арбитражные данные загружены частично; отсутствие иных дел не подтверждено."
    if signal.signal_code == "compliance.roskomnadzor_blocked":
        return "Источник Роскомнадзора недоступен; чистый результат не сформирован."
    if signal.signal_code == "compliance.account_suspension":
        return (
            f"Актуальная проверка приостановлений операций по счетам не подтверждена; "
            f"последняя дата — {_date_text(signal.checked_at)}."
            if signal.checked_at else
            "Актуальная дата проверки приостановлений операций по счетам отсутствует."
        )
    if signal.status == RiskSignalStatus.DATA_QUALITY_REVIEW_REQUIRED:
        return "Качество данных — требуется сверить исходные суммы, единицы и периоды до сильного вывода."
    if signal.status == RiskSignalStatus.NOT_CHECKED:
        return f"{signal.title}: проверка не выполнена; отсутствие сведений не подтверждено."
    if signal.status == RiskSignalStatus.UNAVAILABLE:
        return f"{signal.title}: источник недоступен; чистый результат не сформирован."
    if signal.status == RiskSignalStatus.STALE:
        return f"{signal.title}: сохранённый результат устарел; актуальная проверка не подтверждена."
    return f"{signal.title}: покрытие частичное; отсутствие сведений вне покрытия не подтверждено."


def _recommendation_text(signal: RiskSignal) -> str:
    if signal.signal_code == "tax.debt":
        return "Запросить подтверждение погашения налоговой задолженности и повторить проверку после обновления источника."
    if signal.signal_code.startswith("litigation."):
        return "Проверить карточки дел, процессуальные стадии и судебные акты; не трактовать заявленные требования как подтверждённый долг."
    if signal.signal_code == "bankruptcy.active_procedure":
        return "Уточнить текущую стадию процедуры банкротства и её влияние на планируемую сделку."
    if signal.signal_code.startswith("bankruptcy."):
        return "Завершить официальную проверку ЕФРСБ и подтвердить текущую стадию процедуры, если сведения найдены."
    if signal.signal_code == "enforcement.fssp":
        return "Завершить официальную проверку ФССП и изучить найденные производства, суммы и даты."
    if signal.signal_code == "compliance.account_suspension":
        return "Запросить актуальное подтверждение статуса приостановления операций и повторить официальную проверку."
    if signal.status in LIMITATION_STATUSES:
        return f"Повторить проверку «{signal.title}», когда источник или полное покрытие станут доступны."
    return f"Проверить доказательства по фактору «{signal.title}» и оценить его применимость к условиям сделки."


def _statement(
    *, summary_id: str, assessment: RiskAssessmentResult, kind: SummaryStatementKind,
    index: int, text: str, signal: RiskSignal | None = None,
    hard_blocker: bool = False, public_visible: bool = False,
) -> SummaryStatement:
    source_dates = tuple(
        value for value in (
            signal.source_as_of if signal else None,
            signal.checked_at if signal else None,
        ) if value is not None
    )
    return SummaryStatement(
        statement_id=f"{summary_id}:{kind.value.lower()}:{index}",
        summary_id=summary_id,
        risk_assessment_id=assessment.assessment_id,
        kind=kind,
        text=text,
        signal_ids=(signal.signal_code,) if signal else (),
        rule_id=signal.rule_code if signal else None,
        rule_version=signal.rule_version if signal else None,
        source_refs=(f"{signal.source_code}/{signal.dataset_code}",) if signal else (),
        evidence_refs=signal.evidence_refs if signal else assessment.input_snapshot_refs,
        data_dates=source_dates,
        calculation=signal.calculation if signal else None,
        severity=signal.severity if signal else RiskSeverity.NONE,
        hard_blocker=hard_blocker,
        public_visible=public_visible,
        summary_engine_version=ENGINE_VERSION,
    )


def _grouped_limitation_statement(
    *, summary_id: str, assessment: RiskAssessmentResult,
    signals: tuple[RiskSignal, ...], rules: dict[str, dict[str, Any]],
) -> SummaryStatement | None:
    if not signals:
        return None
    lines = tuple(dict.fromkeys(_limitation_text(signal) for signal in signals))
    text = f"Не удалось завершить проверок: {len(signals)}. " + " ".join(
        f"• {line}" for line in lines
    )
    first = signals[0]
    statement = _statement(
        summary_id=summary_id, assessment=assessment,
        kind=SummaryStatementKind.LIMITATION, index=1,
        text=text, signal=first,
        public_visible=all(
            bool(rules.get(signal.rule_code, {}).get("public_visibility"))
            for signal in signals
        ),
    )
    return statement.model_copy(update={
        "signal_ids": tuple(signal.signal_code for signal in signals),
        "source_refs": tuple(dict.fromkeys(
            f"{signal.source_code}/{signal.dataset_code}" for signal in signals
        )),
        "evidence_refs": tuple(dict.fromkeys(
            ref for signal in signals for ref in signal.evidence_refs
        )),
        "data_dates": tuple(dict.fromkeys(
            value for signal in signals
            for value in (signal.source_as_of, signal.checked_at)
            if value is not None
        )),
        "rule_id": None,
        "rule_version": None,
    })


def _short_conclusion(assessment: RiskAssessmentResult, signals: Iterable[RiskSignal]) -> str:
    items = tuple(signals)
    critical = sum(item.severity == RiskSeverity.CRITICAL for item in items)
    attention = sum(
        item.status in FACTOR_STATUSES
        or (item.status == RiskSignalStatus.PARTIAL_COVERAGE and item.severity != RiskSeverity.NONE)
        for item in items
    )
    incomplete = assessment.completeness.not_checked + assessment.completeness.unavailable + assessment.completeness.stale + assessment.completeness.partial
    label = OVERALL_LABELS[assessment.overall_status]
    if critical:
        text = f"Выявлено критических факторов: {critical}; всего факторов, требующих внимания: {attention}."
    elif attention:
        text = f"Выявлены факторы, требующие внимания: {attention}."
    else:
        text = "В выполненных проверках материальные риск-сигналы не выявлены."
    text = f"{label}. {text}"
    if incomplete:
        text += f" Не завершено обязательных проверок: {incomplete}."
    return text


def _monitoring_statement(
    summary_id: str, assessment: RiskAssessmentResult, previous: RiskAssessmentResult | None,
) -> tuple[tuple[SummaryStatement, ...], MonitoringComparison]:
    origin = assessment.change_origin
    methodology_only = origin == ChangeOrigin.RULESET_CHANGE
    company_change = origin == ChangeOrigin.SOURCE_CHANGE
    if previous is None:
        text = "Предыдущая сопоставимая принятая оценка отсутствует; изменение компании не рассчитано."
    elif methodology_only:
        text = "Изменилась версия правил оценки; это методологическое изменение, а не событие компании."
    elif origin == ChangeOrigin.COVERAGE_CHANGE:
        text = "Изменилось покрытие источников; это не подтверждает изменение самой компании."
    elif origin == ChangeOrigin.DEAL_CONTEXT_CHANGE:
        text = "Изменился контекст сделки; это не подтверждает изменение самой компании."
    else:
        before = {item.signal_code: item.status for item in previous.signals}
        changed = [item.signal_code for item in assessment.signals if before.get(item.signal_code) != item.status]
        text = f"По данным источников изменились состояния проверок: {', '.join(changed)}." if changed else "Новый source snapshot сохранён; изменение значимых состояний сигналов не выявлено."
    statement = _statement(
        summary_id=summary_id, assessment=assessment, kind=SummaryStatementKind.CHANGE,
        index=1, text=text,
    )
    return (statement,), MonitoringComparison(
        previous_risk_assessment_id=previous.assessment_id if previous else None,
        current_risk_assessment_id=assessment.assessment_id,
        change_origin=origin,
        company_change=company_change,
        methodology_only=methodology_only,
    )


def build_summary(
    assessment: RiskAssessmentResult, *, mode: SummaryMode = SummaryMode.USER,
    previous_assessment: RiskAssessmentResult | None = None,
    now: datetime | None = None, summary_id: str | None = None,
    max_factors: int = 5,
) -> SummaryResult:
    """Build text only from a structured RiskAssessmentResult and versioned rules."""
    now = now or datetime.now(timezone.utc)
    summary_id = summary_id or str(uuid4())
    rules = _rule_metadata()
    eligible = [
        item for item in assessment.signals
        if not (mode in {SummaryMode.PUBLIC, SummaryMode.PERSON} and _is_private(item))
        and not (
            mode == SummaryMode.PUBLIC
            and not bool(rules.get(item.rule_code, {}).get("public_visibility"))
        )
    ]
    factor_candidates = sorted(
        (
            item for item in eligible
            if item.status in FACTOR_STATUSES
            or (item.status == RiskSignalStatus.PARTIAL_COVERAGE and item.severity != RiskSeverity.NONE)
        ),
        key=lambda item: _factor_sort_key(item, rules),
    )
    critical = [item for item in factor_candidates if item.severity == RiskSeverity.CRITICAL]
    selected = critical + [item for item in factor_candidates if item not in critical][:max(0, max_factors - len(critical))]
    if not selected:
        completed = [
            item for item in eligible
            if item.status == RiskSignalStatus.NO_RISK_FOUND and item.signal_code == "compliance.cbr_zsk"
        ]
        selected = completed[:1]

    factors = tuple(
        _statement(
            summary_id=summary_id, assessment=assessment, kind=SummaryStatementKind.FACTOR,
            index=index, text=_factor_text(signal), signal=signal,
            hard_blocker=bool(rules.get(signal.rule_code, {}).get("hard_blocker")),
            public_visible=bool(rules.get(signal.rule_code, {}).get("public_visibility")),
        )
        for index, signal in enumerate(selected, 1)
    )
    limitation_signals = tuple(sorted(
        (item for item in eligible if item.status in LIMITATION_STATUSES),
        key=lambda item: item.signal_code,
    ))
    grouped_limitation = _grouped_limitation_statement(
        summary_id=summary_id, assessment=assessment,
        signals=limitation_signals, rules=rules,
    )
    limitations = (grouped_limitation,) if grouped_limitation else ()
    material_selected = [item for item in selected if item.status in FACTOR_STATUSES]
    recommendation_signals = material_selected or list(limitation_signals[:3])
    recommendations = tuple(
        _statement(
            summary_id=summary_id, assessment=assessment, kind=SummaryStatementKind.RECOMMENDATION,
            index=index, text=_recommendation_text(signal), signal=signal,
            hard_blocker=bool(rules.get(signal.rule_code, {}).get("hard_blocker")),
            public_visible=bool(rules.get(signal.rule_code, {}).get("public_visibility")),
        )
        for index, signal in enumerate(recommendation_signals, 1)
    )
    changes: tuple[SummaryStatement, ...] = ()
    comparison = None
    if mode == SummaryMode.MONITORING:
        changes, comparison = _monitoring_statement(summary_id, assessment, previous_assessment)

    conclusion = _statement(
        summary_id=summary_id, assessment=assessment, kind=SummaryStatementKind.CONCLUSION,
        index=1, text=_short_conclusion(assessment, eligible),
    )
    if mode == SummaryMode.PUBLIC:
        factors = tuple(item for item in factors if item.public_visible)
        limitations = tuple(item for item in limitations if item.public_visible)
        recommendations = tuple(item for item in recommendations if item.public_visible)

    compact = None
    if mode == SummaryMode.BULK:
        warnings = sum(item.status == RiskSignalStatus.WARNING for item in eligible)
        critical_count = sum(item.severity == RiskSeverity.CRITICAL for item in eligible)
        core = assessment.completeness.core
        compact = (
            f"Факторов внимания: {warnings}; критических: {critical_count}; "
            f"основных проверок: {core.get('completed', 0)}/{core.get('applicable', 0)}; "
            f"недоступных источников: {assessment.completeness.unavailable}."
        )

    blocks = SummaryTextBlocks(
        short_conclusion=conclusion, main_factors=factors, changes=changes,
        limitations=limitations, recommendations=recommendations,
    )
    explainability = (conclusion, *factors, *changes, *limitations, *recommendations)
    return SummaryResult(
        summary_id=summary_id, company_id=assessment.company_id, company_inn=assessment.company_inn,
        risk_assessment_id=assessment.assessment_id, mode=mode,
        summary_engine_version=ENGINE_VERSION, risk_engine_version=assessment.risk_engine_version,
        ruleset_version=assessment.ruleset_version, generated_at=now,
        overall_status=assessment.overall_status,
        overall_label=OVERALL_LABELS[assessment.overall_status], text_blocks=blocks,
        explainability=explainability, completeness=assessment.completeness.model_dump(mode="json"),
        comparison=comparison, compact_text=compact, public_projection_approved=False,
    )


def _projection_policy(mode: SummaryMode) -> str:
    return PUBLIC_PROJECTION_POLICY_VERSION if mode == SummaryMode.PUBLIC else DEFAULT_PROJECTION_POLICY_VERSION


def can_reuse_summary(
    previous: CompanySummary | None, *, risk_assessment_id: str, mode: SummaryMode,
    deal_context_hash: str, projection_policy_version: str,
) -> bool:
    return bool(
        previous
        and previous.risk_assessment_id == risk_assessment_id
        and previous.mode == mode.value
        and previous.summary_engine_version == ENGINE_VERSION
        and previous.deal_context_hash == deal_context_hash
        and previous.projection_policy_version == projection_policy_version
    )


def _find_previous_assessment(row: CompanyRiskAssessment) -> RiskAssessmentResult | None:
    with get_session() as session:
        previous = session.scalar(
            select(CompanyRiskAssessment)
            .where(
                CompanyRiskAssessment.company_id == row.company_id,
                CompanyRiskAssessment.id < row.id,
            )
            .order_by(CompanyRiskAssessment.id.desc())
            .limit(1)
        )
        return RiskAssessmentResult.model_validate(previous.result_payload) if previous else None


def generate_company_summary(
    company: int | str, *, mode: SummaryMode = SummaryMode.USER,
    now: datetime | None = None,
) -> SummaryResult:
    """Persist a summary for the latest saved assessment; never invokes source providers."""
    with get_session() as session:
        if isinstance(company, int):
            company_id = company
        else:
            company_id = session.scalar(select(Company.id).where(Company.inn == str(company)))
        if company_id is None:
            raise ValueError("Company not found")
        risk_row = session.scalar(
            select(CompanyRiskAssessment)
            .where(CompanyRiskAssessment.company_id == company_id)
            .order_by(CompanyRiskAssessment.calculated_at.desc(), CompanyRiskAssessment.id.desc())
            .limit(1)
        )
        if risk_row is None:
            raise ValueError("Risk assessment not found")
        policy = _projection_policy(mode)
        previous_summary = session.scalar(
            select(CompanySummary)
            .where(
                CompanySummary.risk_assessment_id == risk_row.assessment_id,
                CompanySummary.mode == mode.value,
                CompanySummary.summary_engine_version == ENGINE_VERSION,
                CompanySummary.deal_context_hash == risk_row.deal_context_hash,
                CompanySummary.projection_policy_version == policy,
            )
            .limit(1)
        )
        if can_reuse_summary(
            previous_summary, risk_assessment_id=risk_row.assessment_id, mode=mode,
            deal_context_hash=risk_row.deal_context_hash, projection_policy_version=policy,
        ):
            return SummaryResult.model_validate(previous_summary.structured_payload).model_copy(update={"reused": True})
        risk_result = RiskAssessmentResult.model_validate(risk_row.result_payload)

    previous_risk = _find_previous_assessment(risk_row) if mode == SummaryMode.MONITORING else None
    result = build_summary(risk_result, mode=mode, previous_assessment=previous_risk, now=now)
    payload = result.model_dump(mode="json")
    row = CompanySummary(
        summary_id=result.summary_id, company_id=result.company_id,
        risk_assessment_id=result.risk_assessment_id, mode=result.mode.value,
        summary_engine_version=ENGINE_VERSION, risk_engine_version=result.risk_engine_version,
        ruleset_version=result.ruleset_version, deal_context_hash=risk_row.deal_context_hash,
        projection_policy_version=policy, generated_at=result.generated_at,
        structured_payload=payload, text_blocks=payload["text_blocks"],
        explainability_refs=[item["statement_id"] for item in payload["explainability"]],
    )
    try:
        with get_session() as session:
            session.add(row)
            session.commit()
    except IntegrityError:
        return get_latest_company_summary(str(risk_result.company_inn), mode=mode)  # type: ignore[return-value]
    return result


def get_latest_company_summary(inn: str, *, mode: SummaryMode = SummaryMode.USER) -> SummaryResult | None:
    with get_session() as session:
        company_id = session.scalar(select(Company.id).where(Company.inn == inn))
        if company_id is None:
            return None
        row = session.scalar(
            select(CompanySummary)
            .where(CompanySummary.company_id == company_id, CompanySummary.mode == mode.value)
            .order_by(CompanySummary.generated_at.desc(), CompanySummary.id.desc())
            .limit(1)
        )
        return SummaryResult.model_validate(row.structured_payload) if row else None
