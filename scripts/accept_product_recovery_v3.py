#!/usr/bin/env python3
"""Run the bounded 40-company Интеграционная проверка v3 acceptance.

Uses the existing Firmoteka pilot cache and local official datasets.  Direct
protected sources fail closed when no authorized transport/session is present.
The script never downloads the 500-page cache and never starts a 100k run.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.aggregators.company_product_aggregator import get_company_for_web
from app.contracts.risk import RiskProfile
from app.contracts.source_architecture import FreshnessStatus, NormalizedCheckResult, NormalizedEvidence, NormalizedResultStatus, SourceClass
from app.services.company_check_orchestrator import _normalize_legacy_company
from app.services.capability_applicability_service import apply_capability_applicability
from app.services.risk_engine_v3_service import build_risk_v3
from app.services.risk_v3_persistence_service import persist_v3_assessment
from app.services.source_resolution_service import DEFAULT_RESOLVER
from app.services.summary_engine_v3_service import build_summary_v3
from app.services.source_capability_catalog import CATALOG
from app.sources.direct_runners import CbrZskRunner, EfrsbDirectRunner, FnsBankinformRunner, FsspDirectRunner
from app.sources.firmoteka import FirmotekaSourceAdapter, concrete_bankruptcy_event

GROUPS = {
    "Банкротство — bridge-кандидат":"Банкротство",
    "Налоговая проблема":"Налоговые факторы",
    "Обычная действующая":"Действующие",
    "Низкий наблюдаемый риск — кандидат":"Низкий наблюдаемый риск — кандидат",
}


def read_csv(path):
    with path.open(newline="",encoding="utf-8") as f: return list(csv.DictReader(f))


def evidence_result(code, source, status, value, *, as_of=None):
    return NormalizedCheckResult(check_code=code,result=status,source_class=SourceClass.OFFICIAL_DOWNLOADED_DATASET,
        source_code=source,original_source="ФНС, официальный скачанный набор данных",exact_identifier_match=True,
        checked_at=datetime.now(timezone.utc),source_as_of=as_of,freshness=FreshnessStatus.CURRENT,coverage=1,confidence=1,
        evidence=(NormalizedEvidence(fact=code,value=value,evidence_id=f"{source}:{value.get('inn','unknown')}"),))


def select_candidates(root: Path):
    cards=json.loads((root/"acceptance_40_candidates.json").read_text(encoding="utf-8"))
    used={c["inn"] for c in cards}; bad="7730709480"
    cards=[c for c in cards if c["inn"] != bad]
    if len(cards)==40: return cards
    for row in read_csv(root/"bankruptcy_candidates.csv"):
        inn=row["inn"]
        if inn in used: continue
        payload=json.loads((root/"normalized"/f"{inn}.json").read_text(encoding="utf-8"))
        if concrete_bankruptcy_event(payload):
            cards.insert(9,{"group":"Банкротство — bridge-кандидат","name":payload.get("name"),"inn":inn,"ogrn":payload.get("ogrn"),"status":payload.get("status_normalized"),"registration_date":payload.get("registration_date"),"address":payload.get("address"),"activity":payload.get("okved_name")})
            break
    if len(cards)!=40 or len({c["inn"] for c in cards})!=40: raise RuntimeError("Could not form 40 unique candidates after GGP removal")
    return cards


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--pilot",type=Path,required=True); parser.add_argument("--output",type=Path,required=True); parser.add_argument("--persist",action="store_true"); args=parser.parse_args()
    root=args.pilot; args.output.mkdir(parents=True,exist_ok=True); now=datetime.now(timezone.utc)
    candidates=select_candidates(root); manifest={r["inn"]:r for r in read_csv(root/"manifest_500.csv")}
    fssp_validation={r["inn"]:r for r in read_csv(root/"fssp_cross_validation.csv")}
    adapter=FirmotekaSourceAdapter(); matrix=[]; persisted=0; reused=0
    for card in candidates:
        inn=card["inn"]; payload=json.loads((root/"normalized"/f"{inn}.json").read_text(encoding="utf-8")); local=manifest.get(inn,{})
        company=get_company_for_web(inn)
        if company is None: raise RuntimeError(f"Company absent from master registry: {inn}")
        inputs=list(_normalize_legacy_company(company,now)); inputs.extend(adapter.normalize(payload,requested_inn=inn))
        debt=local.get("local_tax_debt")
        if debt not in (None,""):
            inputs.append(evidence_result("tax_debt","fns_tax_debt",NormalizedResultStatus.FOUND if float(debt)>0 else NormalizedResultStatus.NOT_FOUND,
                {"inn":inn,"total_debt":debt,"revenue":local.get("local_revenue"),"data_date":local.get("local_tax_date")},as_of=now))
        offence=local.get("local_tax_offence")
        if offence not in (None,""):
            inputs.append(evidence_result("tax_offence","fns_tax_offence",NormalizedResultStatus.FOUND if float(offence)>0 else NormalizedResultStatus.NOT_FOUND,
                {"inn":inn,"fine_amount":offence},as_of=now))
        revenue=local.get("local_revenue")
        if revenue not in (None,""):
            inputs.append(evidence_result("finance","fns_revenue_expenses",NormalizedResultStatus.FOUND,
                {"inn":inn,"revenue":revenue,"expenses":local.get("local_expenses"),"calculated_difference":local.get("local_profit_loss"),"year":local.get("local_finance_year")},as_of=now))
        inputs.extend((FsspDirectRunner().run(inn,checked_at=now),EfrsbDirectRunner().run(inn,checked_at=now),
            CbrZskRunner().run(inn,purpose="Проверка контрагента",initiator="Kontragent",checked_at=now),
            FnsBankinformRunner().run(inn,bik=None,checked_at=now)))
        resolved=apply_capability_applicability(
            DEFAULT_RESOLVER.resolve(inputs), company=company, context={}, now=now,
        ); profile=RiskProfile.IP if company.get("entity_type")=="individual_entrepreneur" else RiskProfile.GENERAL_LE
        risk=build_risk_v3(resolved,profile=profile); summary=build_summary_v3(risk,resolved)
        if args.persist:
            risk,summary,_,was_reused=persist_v3_assessment(inn,resolved,profile=profile,now=now); reused+=int(was_reused); persisted+=int(not was_reused)
        mismatch=fssp_validation.get(inn,{}).get("classification")
        matrix.append({"group":GROUPS[card["group"]],"company":card.get("name") or payload.get("name"),"inn":inn,"ogrn":card.get("ogrn") or payload.get("ogrn"),
            "status":payload.get("status_normalized"),"risk":risk.model_dump(mode="json"),"summary":summary.model_dump(mode="json"),
            "sources":[x.model_dump(mode="json") for x in resolved.values()],"data_quality_flags":["FSSP direct/bridge mismatch: 96 vs 74"] if mismatch=="MISMATCH" else [],
            "fssp_validation":mismatch or "NOT_IN_VALIDATION_MATRIX"})
    (args.output/"product_recovery_v3_40.json").write_text(json.dumps(matrix,ensure_ascii=False,indent=2),encoding="utf-8")
    fields=("group","company","inn","risk_score","risk_label","overall","coverage_score","mandatory_score","workflow_completion_percent","positive_allowed","resolved","partial","unavailable_or_error","primary_sources","fallback_sources","top_factors","fssp_validation","data_quality_flags")
    with (args.output/"product_recovery_v3_40.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields,lineterminator="\n"); w.writeheader()
        for x in matrix:
            sources=x["sources"]
            unavailable=[v["check_code"] for v in sources if v["result"] in {"UNAVAILABLE","ERROR"}]
            primary=[f"{v['check_code']}={v['source_code']}" for v in sources]
            fallback=[f"{v['check_code']}={','.join(s for s in v.get('resolved_by',[]) if s != v['source_code'])}" for v in sources if any(s != v["source_code"] for s in v.get("resolved_by",[]))]
            w.writerow({"group":x["group"],"company":x["company"],"inn":x["inn"],"risk_score":x["risk"]["risk_score"],"risk_label":x["risk"]["label"],"overall":x["risk"]["overall"],"coverage_score":x["risk"]["coverage"]["coverage_score"],"mandatory_score":x["risk"]["coverage"]["mandatory_score"],"workflow_completion_percent":x["risk"]["coverage"]["workflow_completion_percent"],"positive_allowed":x["summary"]["positive_conclusion_allowed"],"resolved":" | ".join(x["risk"]["coverage"]["resolved_capabilities"]),"partial":" | ".join(x["risk"]["coverage"]["partial_capabilities"]),"unavailable_or_error":" | ".join(unavailable),"primary_sources":" | ".join(primary),"fallback_sources":" | ".join(fallback),"top_factors":" | ".join(x["summary"]["main_reasons"]),"fssp_validation":x["fssp_validation"],"data_quality_flags":" | ".join(x["data_quality_flags"])})
    buckets=Counter(x["group"] for x in matrix); coverage=[x["risk"]["coverage"]["coverage_score"] for x in matrix]; accepted=sum(x["summary"]["positive_conclusion_allowed"] for x in matrix)
    workflows=[x["risk"]["coverage"]["workflow_completion_percent"] for x in matrix]
    unresolved=Counter(code for x in matrix for code in x["risk"]["coverage"]["unresolved_capabilities"])
    runtime_states=Counter(f"{source['check_code']}:{source['result']}" for x in matrix for source in x["sources"])
    report={"generated_at":now.isoformat(),"target":40,"actual":len(matrix),"buckets":buckets,"coverage":{"min":min(coverage),"max":max(coverage),"average":round(sum(coverage)/len(coverage),1)},"workflow_completion":{"min":min(workflows),"max":max(workflows),"target":100},"positive_gate_passed":accepted,"persisted":persisted,"reused":reused,"unresolved":unresolved,"terminal_states":runtime_states,"fssp_validation":Counter(x["fssp_validation"] for x in matrix),"runtime":{"fssp_direct":"UNAVAILABLE: official exact-INN flow returned CAPTCHA; no authorized machine transport is configured","efrsb_direct":"UNAVAILABLE: official public endpoint returned an anti-bot challenge; no bypass attempted","cbr_zsk":"UNAVAILABLE: official flow requires interactive SmartCaptcha; no bypass attempted","bankinform":"NOT_APPLICABLE without bank/account context and querying-bank BIK; a positive cached fact would still be retained","checko":"ACCESS_PENDING when CHECKO_API_KEY is absent","eis_rnp":"DEFERRED_EXTERNAL_ACCESS; no universal N/A is inferred from missing procurement context"}}
    (args.output/"product_recovery_v3_40_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    policy = [item.model_dump(mode="json") for item in CATALOG.all()]
    (args.output/"capability_applicability_policy.json").write_text(
        json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    cards=[]
    for x in matrix:
        reasons="".join(f"<li>{html.escape(v)}</li>" for v in x["summary"]["main_reasons"]) or "<li>Подтверждённые риск-факторы не выделены</li>"
        positives="".join(f"<li>{html.escape(v)}</li>" for v in x["summary"]["positive_checks"]) or "<li>Завершённые проверки без неблагоприятных сведений отсутствуют</li>"
        limits="".join(f"<li>{html.escape(v)}</li>" for v in x["summary"]["limitations"])
        recs="".join(f"<li>{html.escape(v)}</li>" for v in x["summary"]["recommendations"])
        sources="".join(f"<tr><td>{html.escape(v['source'])}</td><td>{html.escape(v['source_type'])}</td><td>{html.escape(v.get('date') or 'дата не получена')}</td><td>{html.escape(v['coverage'])}</td><td>{v['risk_points']:g}</td></tr>" for v in x["summary"]["source_details"])
        cards.append(f'''<article class="card" data-group="{html.escape(x['group'])}" data-search="{html.escape((x['company']+' '+x['inn']).lower())}"><span>{html.escape(x['group'])}</span><h2>{html.escape(x['company'])}</h2><p>ИНН {x['inn']} · ОГРН {html.escape(str(x['ogrn'] or '—'))}</p><div class="scores"><b>{html.escape(x['summary']['risk_line'])}</b><b>{html.escape(x['summary']['coverage_line'])}</b><b>{html.escape(x['summary']['workflow_line'])}</b></div><p>{html.escape(x['summary']['conclusion'])}</p><h3>Главные причины</h3><ul>{reasons}</ul><h3>Подтверждённые проверки</h3><ul>{positives}</ul><h3>Ограничения</h3><ul>{limits}</ul><h3>Что сделать</h3><ul>{recs}</ul><details><summary>Источники и методика</summary><div class="scroll"><table><tr><th>Источник</th><th>Тип</th><th>Дата</th><th>Покрытие</th><th>Баллы</th></tr>{sources}</table></div></details></article>''')
    page=f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Интеграционная проверка v3 — 40 компаний</title><style>*{{box-sizing:border-box}}body{{margin:0;font:15px/1.5 system-ui;background:linear-gradient(135deg,#e7f5fb,#f8fbff);color:#14263d}}header{{position:sticky;top:0;background:#f8fbffec;backdrop-filter:blur(15px);padding:24px 5vw;border-bottom:1px solid #bfd7e4;z-index:2}}h1{{margin:0}}.tools{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}}input,button{{padding:9px 13px;border:1px solid #b5cedd;border-radius:99px;background:white}}button.active{{background:#08738e;color:white}}main{{padding:22px 5vw}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,440px),1fr));gap:18px}}.card{{padding:22px;border:1px solid #c3d9e5;border-radius:22px;background:#ffffffcf;box-shadow:0 18px 45px #2e65811c}}.scores{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}.scores b{{padding:12px;border-radius:13px;background:#eaf5f8}}h2{{margin:8px 0}}h3{{font-size:14px}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:12px}}td,th{{padding:7px;border-bottom:1px solid #d6e4eb;text-align:left}}.hidden{{display:none}}@media(max-width:600px){{header{{position:relative}}.scores{{grid-template-columns:1fr}}}}</style></head><body><header><h1>Интеграционная проверка v3 — 40 компаний</h1><p>Индекс риска и полнота показаны раздельно</p><div class="tools"><input id="q" placeholder="Название или ИНН"><button class="active">Все</button>{''.join(f'<button>{html.escape(v)}</button>' for v in GROUPS.values())}</div></header><main><p id="count">40 из 40</p><section class="grid">{''.join(cards)}</section></main><script>const cs=[...document.querySelectorAll('.card')],q=document.querySelector('#q'),bs=[...document.querySelectorAll('button')],count=document.querySelector('#count');let f='Все';function draw(){{let n=0,t=q.value.toLowerCase();cs.forEach(c=>{{let ok=(f==='Все'||c.dataset.group===f)&&c.dataset.search.includes(t);c.classList.toggle('hidden',!ok);n+=ok}});count.textContent=n+' из 40'}}q.oninput=draw;bs.forEach(b=>b.onclick=()=>{{bs.forEach(x=>x.classList.remove('active'));b.classList.add('active');f=b.textContent;draw()}});draw()</script></body></html>'''
    (args.output/"product_recovery_v3_40.html").write_text(page,encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,default=dict))


if __name__ == "__main__": main()
