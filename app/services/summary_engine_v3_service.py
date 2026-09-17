"""Client-grade Summary v3 generated only from normalized/resolved facts."""

from __future__ import annotations

from collections.abc import Mapping

from app.contracts.risk_v3 import RiskAssessmentV3
from app.contracts.source_architecture import NormalizedCheckResult, NormalizedResultStatus, SourceClass
from app.contracts.summary_v3 import SummarySourceDetailV3, SummaryV3

SOURCE_TYPE = {SourceClass.OFFICIAL_DIRECT:"официальный прямой источник",SourceClass.OFFICIAL_DOWNLOADED_DATASET:"официальный набор данных",SourceClass.AUTHORIZED_BRIDGE:"разрешённый информационный мост",SourceClass.DISCOVERY_ONLY:"поисковый сигнал"}
NAMES = {"registration":"Регистрационный статус","bankruptcy":"ЕФРСБ / сведения о банкротстве","fssp":"ФССП","tax_debt":"Налоговая задолженность ФНС","tax_offence":"Налоговые правонарушения ФНС","finance":"Финансовая отчётность ФНС","arbitration":"Арбитражные дела","general_courts":"Суды общей юрисдикции","cbr_zsk":"Платформа ЗСК Банка России","cbr_warning":"Предупредительный список Банка России","bankinform":"Приостановления операций ФНС","management":"Реестр дисквалифицированных лиц ФНС","licences_sro":"Лицензии и СРО","procurement_rnp":"РНП","regulatory_inspections":"Контрольные мероприятия"}


def build_summary_v3(risk: RiskAssessmentV3, resolved: Mapping[str, NormalizedCheckResult]) -> SummaryV3:
    coverage=risk.coverage
    positive_allowed=coverage.coverage_score>=90 and coverage.mandatory_hard_checks_resolved and risk.risk_score<20
    reasons=tuple(f"{point.fact} — {point.points:g} балла" for point in sorted(risk.points,key=lambda x:x.points,reverse=True)[:3])
    positives=[]; limitations=[]; recommendations=[]
    for code,item in resolved.items():
        name=NAMES.get(code,code)
        if item.result==NormalizedResultStatus.NOT_FOUND and item.coverage>=1:
            positives.append(f"{name} — совпадений или неблагоприятных сведений не найдено")
        elif item.result in {NormalizedResultStatus.UNAVAILABLE,NormalizedResultStatus.ERROR,NormalizedResultStatus.PARTIAL} or item.coverage<1:
            reason=item.limitation or "проверка не дала полного результата"
            limitations.append(f"{name}: {reason} Это ограничивает полноту, но не добавляет риск-баллы.")
    for point in sorted(risk.points,key=lambda x:x.points,reverse=True)[:3]:
        recommendations.append({
            "bankruptcy":"Проверить текущую стадию банкротства и последнее сообщение ЕФРСБ.",
            "enforcement":"Изучить действующие исполнительные производства, суммы и даты в ФССП.",
            "tax":"Запросить справку ФНС о состоянии расчётов и подтверждение погашения задолженности.",
            "courts":"Открыть последние судебные акты и проверить роль компании и текущую стадию дел.",
            "finance":"Сверить финансовую отчётность и причины убытка за указанные периоды.",
            "compliance":"Проверить официальный результат и основания регуляторного сигнала.",
        }.get(point.section,"Проверить первичный документ источника по указанному фактору."))
    details=[]
    point_by_cap={p.capability_id:p for p in risk.points}
    for code,item in resolved.items():
        point=point_by_cap.get(code)
        details.append(SummarySourceDetailV3(source=NAMES.get(code,item.source_code),source_type=SOURCE_TYPE[item.source_class],
            date=item.source_as_of.strftime("%d.%m.%Y") if item.source_as_of else item.checked_at.strftime("%d.%m.%Y") if item.checked_at else None,
            coverage=f"{round(item.coverage*100)}/100",rule=point.rule if point else None,risk_points=point.points if point else 0))
    if positive_allowed:
        conclusion="Существенных рисков по выполненным проверкам не выявлено. Обязательные проверки завершены."
    elif risk.overall=="Недостаточно данных для полного вывода":
        conclusion="Недостаточно данных для полного вывода. Найденные факты и незавершённые проверки показаны отдельно."
    elif reasons:
        conclusion=f"Индекс отражает подтверждённые факторы, главный из них: {reasons[0]}. Это не вероятность дефолта или мошенничества."
    else:
        conclusion="Подтверждённые риск-факторы не выявлены, но положительный вывод ограничен полнотой проверки."
    prefix="Предварительный индекс риска" if risk.preliminary else "Риск"
    return SummaryV3(risk_line=f"{prefix}: {risk.risk_score}/100 — {risk.label.lower()}",coverage_line=f"Полнота проверки: {coverage.coverage_score}/100",
        conclusion=conclusion,main_reasons=reasons,positive_checks=tuple(positives),limitations=tuple(limitations),recommendations=tuple(dict.fromkeys(recommendations)),
        source_details=tuple(details),positive_conclusion_allowed=positive_allowed)
