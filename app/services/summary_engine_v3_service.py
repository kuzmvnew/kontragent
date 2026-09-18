"""Client-grade Summary v3 generated only from normalized/resolved facts."""

from __future__ import annotations

from collections.abc import Mapping

from app.contracts.risk_v3 import RiskAssessmentV3
from app.contracts.source_architecture import NormalizedCheckResult, NormalizedResultStatus, SourceClass
from app.contracts.summary_v3 import SummarySourceDetailV3, SummaryV3

SOURCE_TYPE = {SourceClass.OFFICIAL_DIRECT:"официальный прямой источник",SourceClass.OFFICIAL_DOWNLOADED_DATASET:"официальный набор данных",SourceClass.AUTHORIZED_BRIDGE:"разрешённый информационный мост",SourceClass.DISCOVERY_ONLY:"поисковый сигнал",SourceClass.POLICY_RULE:"правило применимости"}
NAMES = {"registration":"Регистрационный статус","bankruptcy":"ЕФРСБ / сведения о банкротстве","fssp":"ФССП","tax_debt":"Налоговая задолженность ФНС","tax_offence":"Налоговые правонарушения ФНС","finance":"Финансовая отчётность ФНС","arbitration":"Арбитражные дела","general_courts":"Суды общей юрисдикции","cbr_zsk":"Платформа ЗСК Банка России","cbr_warning":"Предупредительный список Банка России","bankinform":"Приостановления операций ФНС","management":"Реестр дисквалифицированных лиц ФНС","licences_sro":"Лицензии и СРО","procurement_rnp":"РНП","regulatory_inspections":"Контрольные мероприятия"}
POSITIVE_TEXT = {
    "cbr_warning": "Предупредительный список Банка России — совпадений не найдено.",
    "management": "Реестр дисквалифицированных лиц ФНС — совпадений не найдено.",
    "fssp": "ФССП — действующие производства не найдены.",
    "cbr_zsk": "Платформа ЗСК Банка России — сведения о высокой группе риска не найдены.",
}


def _points_text(value: float) -> str:
    """Return compact Russian client copy for a risk-point amount."""

    absolute = abs(value)
    if absolute == int(absolute):
        integer = int(absolute) % 100
        tail = integer % 10
        word = "балл" if tail == 1 and integer != 11 else "балла" if tail in {2, 3, 4} and integer not in {12, 13, 14} else "баллов"
    else:
        word = "балла"
    return f"{value:g} {word}"


def _client_limitation(value: str | None) -> str:
    """Keep transport terminology and parser details out of the first view."""

    text = (value or "").strip()
    lowered = text.lower()
    if not text:
        return "проверка не дала полного результата"
    if "не настроен" in lowered or "не подключен" in lowered:
        return "проверка источника пока недоступна в текущей среде"
    if "challenge" in lowered or "интерактив" in lowered:
        return "источник требует ручного подтверждения"
    if "exact-inn" in lowered:
        return "источник не подтвердил точное совпадение по ИНН"
    if "firmoteka" in lowered and "структурирован" in lowered:
        return "результат проверки пока неполный"
    if "official flow" in lowered or "публичный/машинный" in lowered:
        return "проверка официального источника пока недоступна"
    return text


def _group_limitations(values: list[tuple[str, str]]) -> tuple[str, ...]:
    grouped: dict[str, list[str]] = {}
    for name, reason in values:
        grouped.setdefault(reason, []).append(name)
    output = []
    for reason, names in grouped.items():
        shown = ", ".join(names[:3])
        if len(names) > 3:
            shown += f" и ещё {len(names) - 3}"
        output.append(f"{shown}: {reason}")
    if len(output) > 4:
        hidden = len(output) - 3
        output = output[:3] + [f"Ещё {hidden} группы проверок имеют ограничения; детали доступны в источниках."]
    return tuple(output)


def build_summary_v3(risk: RiskAssessmentV3, resolved: Mapping[str, NormalizedCheckResult]) -> SummaryV3:
    coverage=risk.coverage
    positive_allowed=(
        coverage.coverage_score >= 90
        and coverage.mandatory_hard_checks_resolved
        and risk.risk_score < 20
        and not risk.points
    )
    reasons=tuple(f"{point.fact} — {_points_text(point.points)}" for point in sorted(risk.points,key=lambda x:x.points,reverse=True)[:3])
    positives=[]; limitation_items=[]; recommendations=[]
    for code,item in resolved.items():
        name=NAMES.get(code,code)
        if item.result==NormalizedResultStatus.NOT_FOUND and item.coverage>=1:
            positives.append(POSITIVE_TEXT.get(code, f"{name} — неблагоприятные сведения не найдены."))
        elif item.result in {NormalizedResultStatus.UNAVAILABLE,NormalizedResultStatus.ERROR,NormalizedResultStatus.PARTIAL} or item.coverage<1:
            reason=_client_limitation(item.limitation)
            limitation_items.append((name, reason))
            recommendations.append({
                "bankruptcy": "Завершить проверку банкротства на Федресурсе и сохранить дату результата.",
                "fssp": "Завершить точную проверку по ИНН на сайте ФССП и сверить суммы производств.",
                "arbitration": "Проверить арбитражные дела по ИНН и отдельно оценить роль компании и текущую стадию.",
                "general_courts": "Проверить суды общей юрисдикции в нужных регионах и зафиксировать охваченный период.",
                "cbr_zsk": "Завершить проверку высокой группы риска на сайте Банка России.",
                "licences_sro": "Сверить разрешения и членство в профильных реестрах для заявленного вида деятельности.",
                "procurement_rnp": "При закупочном контексте отдельно проверить актуальную запись в РНП ЕИС.",
            }.get(code, "Обновить незавершённую проверку в официальном источнике."))
    for point in sorted(risk.points,key=lambda x:x.points,reverse=True)[:3]:
        recommendations.append({
            "bankruptcy":"Проверить текущую стадию банкротства и последнее сообщение ЕФРСБ.",
            "enforcement":"Изучить действующие исполнительные производства, суммы и даты в ФССП.",
            "tax":"Запросить справку ФНС о состоянии расчётов и подтверждение погашения задолженности.",
            "courts":"Открыть последние судебные акты и проверить роль компании и текущую стадию дел.",
            "finance":"Сверить финансовую отчётность и причины убытка за указанные периоды.",
            "compliance":"Проверить официальный результат и основания регуляторного сигнала.",
        }.get(point.section,"Сверить сведения с документами контрагента."))
    details=[]
    point_by_cap={p.capability_id:p for p in risk.points}
    for code,item in resolved.items():
        if item.result == NormalizedResultStatus.NOT_APPLICABLE:
            continue
        point=point_by_cap.get(code)
        details.append(SummarySourceDetailV3(source=NAMES.get(code,item.source_code),source_type=SOURCE_TYPE[item.source_class],
            date=item.source_as_of.strftime("%d.%m.%Y") if item.source_as_of else item.checked_at.strftime("%d.%m.%Y") if item.checked_at else None,
            coverage=f"{round(item.coverage*100)}/100",rule=point.rule if point else None,
            calculation=point.calculation if point else None,risk_points=point.points if point else 0))
    if positive_allowed:
        conclusion="Существенных рисков по выполненным проверкам не выявлено. Обязательные проверки завершены."
    elif risk.overall=="Недостаточно данных для полного вывода":
        conclusion="Недостаточно данных для полного вывода. Найденные факты и незавершённые проверки показаны отдельно."
    elif reasons:
        conclusion=f"Индекс отражает подтверждённые факторы, главный из них: {reasons[0]}. Это не вероятность дефолта или мошенничества."
    else:
        conclusion="Подтверждённые риск-факторы не выявлены, но положительный вывод ограничен полнотой проверки."
    return SummaryV3(risk_line=f"Индекс риска: {risk.risk_score}/100 — {risk.label.lower()}",coverage_line=f"Полнота данных: {coverage.coverage_score}/100",
        workflow_line=f"Проверки завершены: {coverage.workflow_completed}/{coverage.workflow_total} ({coverage.workflow_completion_percent}%)",
        conclusion=conclusion,main_reasons=reasons,positive_checks=tuple(positives),limitations=_group_limitations(limitation_items),recommendations=tuple(dict.fromkeys(recommendations))[:5],
        source_details=tuple(details),positive_conclusion_allowed=positive_allowed)
