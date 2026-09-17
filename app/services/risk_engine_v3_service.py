"""Risk Engine v3: arithmetic product index independent of coverage."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from app.contracts.risk import RiskProfile
from app.contracts.risk_v3 import RiskAssessmentV3, RiskPoint
from app.contracts.source_architecture import NormalizedCheckResult, NormalizedResultStatus, SourceClass
from app.services.coverage_engine_service import build_coverage_v2

SECTION_WEIGHTS = {
    "registration":12,"bankruptcy":18,"enforcement":12,"tax":14,"finance":12,
    "courts":10,"compliance":10,"management":5,"licences":3,"procurement":2,"inspections":2,
}
STRENGTH = {SourceClass.OFFICIAL_DIRECT:1.0,SourceClass.OFFICIAL_DOWNLOADED_DATASET:1.0,SourceClass.AUTHORIZED_BRIDGE:.9,SourceClass.DISCOVERY_ONLY:0.0}
REGISTRATION_LABELS = {"ACTIVE":"Действует","INACTIVE":"Не действует","LIQUIDATED":"Ликвидирована","EXCLUDED":"Исключена из реестра","TERMINATED":"Деятельность прекращена","REORGANIZATION":"Реорганизация","LIQUIDATING":"Ликвидация"}
PROCEDURE_LABELS = {"competitive_proceedings":"конкурсное производство","observation":"наблюдение","external_management":"внешнее управление","declared_bankrupt":"признание банкротом","case_initiated":"возбуждено дело о банкротстве"}


def _num(value, default=0.0):
    try: return float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError): return default


def _money(value: float) -> str:
    rounded = round(value, 2)
    text = f"{rounded:,.2f}".replace(",", " ")
    text = text.rstrip("0").rstrip(".")
    return f"{text} ₽"


def _value(item: NormalizedCheckResult) -> dict:
    for evidence in item.evidence:
        if isinstance(evidence.value, dict): return evidence.value
    return {}


def _add(points, sections, item, section, raw, fact, rule, calculation, details=None):
    raw = max(0.0, min(float(raw), SECTION_WEIGHTS[section]))
    strength = STRENGTH[item.source_class]
    awarded = round(raw * strength, 2)
    if awarded <= 0: return
    point = RiskPoint(capability_id=item.check_code, section=section, source_code=item.source_code,
        source_class=item.source_class, fact=fact, rule=rule, raw_points=raw,
        evidence_strength=strength, points=awarded, calculation=calculation,
        source_as_of=item.source_as_of.isoformat() if item.source_as_of else None,
        evidence_ids=tuple(e.evidence_id for e in item.evidence), details=details or {})
    points.append(point); sections[section] = sections.get(section, 0) + awarded


def _label(score: int) -> str:
    if score >= 80: return "Критический риск"
    if score >= 60: return "Высокий риск"
    if score >= 40: return "Требует внимания"
    if score >= 20: return "Умеренный риск"
    return "Низкий наблюдаемый риск"


def build_risk_v3(resolved: Mapping[str, NormalizedCheckResult], *, profile: RiskProfile) -> RiskAssessmentV3:
    coverage = build_coverage_v2(resolved, profile=profile)
    points=[]; sections={key:0.0 for key in SECTION_WEIGHTS}; official_active_bankruptcy=False; strong_high=False
    for code,item in resolved.items():
        if item.result != NormalizedResultStatus.FOUND: continue
        value=_value(item)
        if code=="registration":
            status=str(value.get("status_normalized") or value.get("status") or "").upper()
            raw=12 if status in {"LIQUIDATED","EXCLUDED","TERMINATED","INACTIVE"} else 6 if status in {"REORGANIZATION","LIQUIDATING"} else 0
            _add(points,sections,item,"registration",raw,f"Регистрационный статус: {REGISTRATION_LABELS.get(status, 'статус требует уточнения')}","REGISTRATION_STATUS",f"section={raw}/12")
        elif code=="bankruptcy":
            event=value
            procedure=str(event.get("procedure") or "")
            active=procedure in {"competitive_proceedings","observation","external_management","declared_bankrupt"} or bool(event.get("active_procedure"))
            raw=18 if active else 9 if procedure=="case_initiated" else 3 if event.get("historical") else 0
            _add(points,sections,item,"bankruptcy",raw,f"Процедура: {PROCEDURE_LABELS.get(procedure, 'событие банкротства')}","BANKRUPTCY_PROCEDURE",f"{raw} × {STRENGTH[item.source_class]:.2f}",event)
            official_active_bankruptcy = active and item.source_class==SourceClass.OFFICIAL_DIRECT
            strong_high = strong_high or (active and item.source_class==SourceClass.AUTHORIZED_BRIDGE)
        elif code=="fssp":
            count=_num(value.get("count")); amount=_num(value.get("remaining_amount") or value.get("amount")); revenue=_num(value.get("revenue"))
            raw=min(12, (2 if count else 0)+(min(5,count/5) if count else 0)+(5 if revenue and amount/revenue>=.25 else 3 if amount>=10_000_000 else 1 if amount>0 else 0))
            _add(points,sections,item,"enforcement",raw,f"Действующих производств: {int(count)}, остаток: {_money(amount)}","FSSP_MATERIALITY",f"count+amount ratio = {raw}/12",value)
        elif code=="tax_debt":
            debt=_num(value.get("total_debt") or value.get("total") or value.get("amount")); revenue=_num(value.get("revenue")); ratio=debt/revenue if revenue>0 else None
            raw=min(10, 1+(min(5,debt/10_000_000) if debt else 0)+(4 if ratio is not None and ratio>=.25 else 0)) if debt>0 else 0
            _add(points,sections,item,"tax",raw,f"Налоговая задолженность: {_money(debt)}","TAX_DEBT_SCALE",f"absolute+ratio={raw}/10",{"debt":debt,"revenue":revenue,"ratio":ratio})
        elif code=="tax_offence":
            amount=_num(value.get("fine_amount") or value.get("amount")); raw=min(4,1+amount/1_000_000) if value else 2
            _add(points,sections,item,"tax",raw,"Опубликовано налоговое правонарушение","TAX_OFFENCE",f"min(4, 1 + {amount:g}/1000000)",value)
        elif code=="finance":
            revenue=_num(value.get("revenue")); profit=_num(value.get("profit") or value.get("calculated_difference")); history=value.get("history") or []
            loss=max(0,-profit); ratio=loss/revenue if revenue>0 else None; multi=sum(_num(x.get("profit"))<0 for x in history if isinstance(x,dict))
            raw=min(12,(5 if ratio is not None and ratio>=.25 else 3 if loss else 0)+(3 if multi>=2 else 0)+(4 if revenue==0 and loss else 0))
            _add(points,sections,item,"finance",raw,f"Финансовый результат: {_money(profit)}","FINANCE_LOSS",f"loss/revenue={ratio}; years={multi}",value)
        elif code in {"arbitration","general_courts"}:
            defendant=_num(value.get("defendant_count")); amount=_num(value.get("defendant_claim_amount")); revenue=_num(value.get("revenue")); ratio=amount/revenue if revenue>0 else 0
            raw=min(10, min(4,defendant)+(4 if ratio>=.25 else 2 if amount>=10_000_000 else 0)+(2 if value.get("recent") else 0))
            _add(points,sections,item,"courts",raw,f"Дел в роли ответчика: {int(defendant)}; заявлено {_money(amount)}","COURT_DEFENDANT_ACTIVITY",f"role+recency+amount={raw}/10",value)
        elif code=="cbr_zsk": _add(points,sections,item,"compliance",5,"Банк России сообщил сведения о высокой группе риска","CBR_ZSK_HIGH", "5/5")
        elif code=="cbr_warning": _add(points,sections,item,"compliance",3,"Точное совпадение в предупредительном списке Банка России","CBR_WARNING_EXACT","3/3")
        elif code=="bankinform": _add(points,sections,item,"compliance",2,"Найдены действующие решения о приостановлении операций","FNS_BANKINFORM_ACTIVE","2/2")
        elif code=="management" and value.get("current_director_disqualified"):
            _add(points,sections,item,"management",5,"Текущий руководитель подтверждён в реестре дисквалифицированных лиц","DIRECTOR_DISQUALIFIED","5/5",value)
        elif code=="licences_sro" and value.get("required") and (value.get("revoked") or value.get("missing")):
            _add(points,sections,item,"licences",3,"Необходимая лицензия/членство отсутствует или прекращено","LICENCE_REQUIRED","3/3",value)
        elif code=="procurement_rnp" and value.get("rnp_found"):
            _add(points,sections,item,"procurement",2,"Подтверждена запись в РНП","RNP_VERIFIED","2/2",value)
        elif code=="regulatory_inspections" and value.get("violations_found"):
            _add(points,sections,item,"inspections",2,"По проверке подтверждены нарушения","ERKNM_OUTCOME","2/2",value)
    total=min(100,round(sum(p.points for p in points)))
    if official_active_bankruptcy: total=max(90,total)
    if strong_high: total=max(60,total)
    preliminary=coverage.coverage_score<90
    has_high_fact=official_active_bankruptcy or strong_high or total>=60
    overall="Недостаточно данных для полного вывода" if coverage.coverage_score<70 and not has_high_fact else _label(total)
    return RiskAssessmentV3(profile=profile,risk_score=total,reliability_index=100-total,label=_label(total),overall=overall,
        preliminary=preliminary,coverage=coverage,points=tuple(points),section_scores={k:round(v,2) for k,v in sections.items()},
        explanation=f"Сумма подтверждённых баллов: {sum(p.points for p in points):g}; итог после floors/cap: {total}. Coverage не умножается на risk.")
