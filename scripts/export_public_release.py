#!/usr/bin/env python3
"""Build a strict public release bundle from persisted operational data only."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from public_app.contracts import (
    SCHEMA_VERSION,
    CanonicalManifest,
    CompanyInfo,
    Freshness,
    PublicationInfo,
    PublicProjection,
    PublicCompanyViewV1,
    PublicFactSource,
    PublicViewFact,
    PublicViewSection,
    PublicLimitation,
    PublicRecommendation,
    PublicRisk,
    PublicRiskFactor,
    PublicSourceBlock,
    PublicState,
    PublicSummary,
    ReleaseManifest,
    strongest_state,
)
from app.contracts.company_view_v1 import Audience, CompanyViewModelV1, DataState
from app.services.company_view_service import build_company_view_v1
from public_app.semantic import (
    MeaningInput,
    aggregate_clean_conclusion_proof,
    compile_aggregate_abstention_conclusion,
    compile_aggregate_clean_conclusion,
    compile_limitation,
    compile_meaning,
    compile_recommendation,
    compile_source_status,
)
from scripts.public_release_common import canonical_json, write_checksums

SOURCE_NAMES = {
    "REVEXP": "Доходы и расходы по данным ФНС",
    "PAYTAX": "Уплаченные налоги и сборы по данным ФНС",
    "DEBTAM": "Налоговая задолженность по данным ФНС",
    "TAXOFFENCE": "Налоговые правонарушения по данным ФНС",
}
SOURCE_CODE_NAMES = {
    "fns_tax_debt": "Налоговая задолженность ФНС",
    "fns_tax_offence": "Налоговые правонарушения ФНС",
    "firmoteka_registration": "Регистрационные сведения",
    "fssp_direct_historical_acceptance": "ФССП",
}
SOURCE_CHECK_CODES = {
    "REVEXP": "finance",
    "DEBTAM": "tax_debt",
    "TAXOFFENCE": "tax_offence",
}
CANONICAL_COHORT_PATH = "docs/releases/public-v1-cohort-40.json"
_PUBLIC_VIEW_NAMESPACE = UUID("9b9b3d87-82a6-40f8-bd1f-8a5e10b9bb96")
_PUBLIC_SOURCE_NAMES = {
    "MASTER_REGISTRY": "Единый государственный реестр юридических лиц",
    "REVEXP": "Доходы и расходы по данным ФНС",
    "PAYTAX": "Уплаченные налоги и сборы по данным ФНС",
    "DEBTAM": "Налоговая задолженность по данным ФНС",
    "TAXOFFENCE": "Налоговые правонарушения по данным ФНС",
    "HEADCOUNT": "Среднесписочная численность по данным ФНС",
    "ROSZDRAV_LICENSES": "Единый реестр лицензий Росздравнадзора",
    "FIRMOTEKA_AUTHORIZED_BRIDGE": "Firmoteka · вторичный источник",
}
_PUBLIC_SOURCE_CLASS_NAMES = {
    "OFFICIAL_PRIMARY": "Официальный первичный источник",
    "OFFICIAL_API_OPEN_DATA": "Официальные открытые данные",
    "AUTHORIZED_BRIDGE": "Публичный вторичный источник",
    "DERIVED": "Расчёт на основе опубликованных данных",
}
_PUBLIC_SECTION_TITLES = {
    "identity": "Основные сведения",
    "status": "Статус",
    "registration": "Регистрация",
    "address": "Адрес и подразделения",
    "activity": "Виды деятельности",
    "management": "Руководство",
    "founders": "Учредители",
    "contacts": "Контакты из публичных источников",
    "capital": "Уставный капитал",
    "finances": "Финансы",
    "employees": "Численность сотрудников",
    "tax": "Налоги",
    "enforcement": "Исполнительные производства",
    "licenses": "Лицензии",
    "events": "События компании",
    "risk": "Аналитическая оценка",
    "summary": "Краткий вывод",
    "source_coverage": "Покрытие источников",
    "freshness": "Актуальность",
    "limitations": "Ограничения",
    "anchors": "Ссылки на факты",
    "links": "Ссылки",
}
_PUBLIC_FIELD_LABELS = {
    "name": "Наименование",
    "short_name": "Краткое наименование",
    "full_name": "Полное наименование",
    "inn": "ИНН",
    "kpp": "КПП",
    "ogrn": "ОГРН",
    "entity_type": "Тип юридического лица",
    "legal_form": "Организационно-правовая форма",
    "status": "Статус",
    "registration_date": "Дата регистрации",
    "termination_date": "Дата прекращения",
    "registered_address": "Юридический адрес",
    "region": "Регион",
    "region_code": "Код региона",
    "division": "Подразделение",
    "okved": "ОКВЭД",
    "okved_name": "Основной вид деятельности",
    "additional_okved": "Дополнительный вид деятельности",
    "manager": "Руководитель",
    "founder": "Учредитель",
    "phone": "Телефон",
    "email": "Электронная почта",
    "authorized_capital": "Уставный капитал",
    "REVENUE": "Выручка",
    "EXPENSES": "Расходы",
    "PROFIT_LOSS": "Прибыль (убыток)",
    "NET_PROFIT": "Чистая прибыль",
    "EQUITY": "Капитал",
    "COMPANY_VALUE": "Стоимость компании",
    "EMPLOYEE_COUNT": "Сотрудники",
    "debt": "Налоговая задолженность",
    "paid": "Уплаченные налоги и сборы",
    "offence": "Налоговое правонарушение",
    "aggregate": "Сводные данные",
    "case": "Исполнительное производство",
    "license": "Лицензия",
    "event": "Событие",
    "legal_event": "Событие",
    "assessment": "Оценка",
    "conclusion": "Вывод",
    "source": "Источник",
}
_PUBLIC_STATE_LABELS = {
    DataState.FOUND: "Сведения найдены",
    DataState.NOT_FOUND: "Сведения не найдены в завершённой проверке",
    DataState.NOT_APPLICABLE: "Проверка неприменима",
    DataState.NOT_CHECKED: "Сведения пока не проверены",
    DataState.SOURCE_UNAVAILABLE: "Источник временно недоступен",
    DataState.TIMEOUT: "Источник не ответил вовремя",
    DataState.PARSING_ERROR: "Сведения источника не удалось обработать",
    DataState.STALE_DATA: "Сведения требуют обновления",
    DataState.UNKNOWN: "Статус сведений не определён",
    DataState.PARTIAL: "Доступна часть сведений",
    DataState.CONFLICTING_EVIDENCE: "Источники содержат различающиеся сведения",
}


def _json_default(value: Any):
    if isinstance(value, (datetime, date, Decimal)):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    raise TypeError(type(value).__name__)


def _date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _aware(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _money(value) -> str:
    return f"{Decimal(str(value or 0)):.2f}"


def _clean_reason(value: str) -> str:
    value = re.sub(r"\s*[—-]\s*\d+(?:[.,]\d+)?\s*балл\w*", "", str(value)).strip()
    value = re.sub(r"^Индекс риска:[^.]+\.\s*", "", value).strip()
    return value[:1000]


def _registration(risk: dict) -> dict:
    for check in risk.get("normalized_results") or risk.get("evidence_snapshot") or ():
        if check.get("check_code") == "registration" or check.get("capability_code") == "registration":
            evidence = check.get("evidence") or ()
            if evidence and isinstance(evidence[0], dict):
                value = evidence[0].get("value")
                if isinstance(value, dict):
                    return value
            payload = check.get("fact_payload")
            if isinstance(payload, dict):
                return payload
    return {}


def _director(cursor, company_id: int, risk: dict) -> tuple[str | None, str | None]:
    cursor.execute(
        """SELECT full_name, position FROM company_managers
           WHERE company_id=%s AND is_current=TRUE
           ORDER BY id DESC LIMIT 1""",
        (company_id,),
    )
    row = cursor.fetchone()
    if row and row.get("full_name"):
        return row["full_name"], row.get("position")
    for check in risk.get("normalized_results") or ():
        if check.get("check_code") != "management":
            continue
        for evidence in check.get("evidence") or ():
            value = evidence.get("value") or {}
            if isinstance(value, dict):
                name = value.get("director_name") or value.get("name") or value.get("full_name")
                position = value.get("director_position") or value.get("position")
                if name:
                    return str(name)[:600], str(position)[:300] if position else None
                for key in ("leaders", "directors", "management", "managers"):
                    candidates = value.get(key)
                    if not isinstance(candidates, list):
                        continue
                    for candidate in candidates:
                        if not isinstance(candidate, dict):
                            continue
                        name = candidate.get("name") or candidate.get("full_name")
                        position = candidate.get("position") or candidate.get("title")
                        if name:
                            return str(name)[:600], str(position)[:300] if position else None
    return None, None


def _latest_json(cursor, table: str, company_id: int, order: str) -> dict | None:
    cursor.execute(
        f"SELECT to_jsonb(row_value) AS value FROM (SELECT * FROM {table} WHERE company_id=%s ORDER BY {order} LIMIT 1) row_value",
        (company_id,),
    )
    row = cursor.fetchone()
    return row["value"] if row else None


def _dataset(cursor, dataset_id: int | None) -> dict:
    if dataset_id is None:
        return {}
    cursor.execute("SELECT to_jsonb(d) AS value FROM data_sets d WHERE id=%s", (dataset_id,))
    row = cursor.fetchone()
    return row["value"] if row else {}


def _freshness(dataset: dict, result_date: date) -> Freshness:
    actual_until = _date(dataset.get("official_actual_until"))
    if actual_until and actual_until < result_date:
        return Freshness.STALE
    if dataset.get("last_data_date") or dataset.get("source_as_of"):
        return Freshness.CURRENT
    return Freshness.UNKNOWN


def _risk_check(risk: dict, code: str) -> dict | None:
    capability = SOURCE_CHECK_CODES.get(code)
    if not capability:
        return None
    for check in risk.get("normalized_results") or risk.get("resolved_checks") or ():
        if check.get("check_code") == capability or check.get("capability_code") == capability:
            return check
    return None


def _check_state(check: dict | None) -> PublicState | None:
    if not check:
        return None
    execution = str(check.get("execution") or "").upper()
    result = str(check.get("result") or check.get("observation") or "").upper()
    resolution = str(check.get("resolution_state") or "").upper()
    freshness = str(check.get("freshness") or "").upper()
    if resolution == "CONFLICTING_EVIDENCE":
        return PublicState.CONFLICTING_EVIDENCE
    if execution in {"SOURCE_UNAVAILABLE", "TIMEOUT", "PARSING_ERROR", "NOT_CHECKED"}:
        return PublicState(execution)
    if result == "UNAVAILABLE":
        return PublicState.SOURCE_UNAVAILABLE
    if result in {"FOUND", "NOT_FOUND", "NOT_APPLICABLE", "PARTIAL", "UNKNOWN"}:
        state = PublicState(result)
    else:
        state = PublicState.UNKNOWN
    if freshness == "STALE":
        return PublicState.STALE_DATA
    return state


def _check_source_date(check: dict | None) -> date | None:
    if not check:
        return None
    direct = _date(check.get("source_as_of") or check.get("effective_at"))
    if direct:
        return direct
    for evidence in check.get("evidence") or ():
        value = evidence.get("value") if isinstance(evidence, dict) else None
        if isinstance(value, dict):
            candidate = _date(value.get("data_date") or value.get("source_data_date"))
            if candidate:
                return candidate
    return None


def _source_block(
    cursor, code: str, company_id: int, risk: dict, default_result_date: date
) -> PublicSourceBlock:
    check = _risk_check(risk, code)
    checked_state = _check_state(check)
    checked_result_date = _date(check.get("checked_at")) if check else None
    if code == "REVEXP":
        row = _latest_json(cursor, "company_revenue_expense_snapshots", company_id, "data_date DESC, id DESC")
        values = {} if not row else {
            "Год": row.get("data_year"), "Выручка, ₽": _money(row.get("revenue")),
            "Расходы, ₽": _money(row.get("expenses")), "Прибыль / убыток, ₽": _money(row.get("profit_loss")),
        }
    elif code == "PAYTAX":
        row = _latest_json(cursor, "company_tax_payment_snapshots", company_id, "data_date DESC, id DESC")
        values = {} if not row else {
            "Год": row.get("data_year"), "Всего уплачено, ₽": _money(row.get("total_amount")),
            "Налоги, ₽": _money(row.get("tax_amount")), "Страховые взносы, ₽": _money(row.get("insurance_amount")),
            "Пени, ₽": _money(row.get("penalty_amount")),
        }
    elif code == "DEBTAM":
        row = _latest_json(cursor, "company_tax_debt_snapshots", company_id, "data_date DESC, id DESC")
        values = {} if not row else {
            "Общая задолженность, ₽": _money(row.get("total_debt")), "Недоимка, ₽": _money(row.get("total_arrears")),
            "Пени, ₽": _money(row.get("total_penalties")), "Штрафы, ₽": _money(row.get("total_fines")),
        }
    else:
        row = _latest_json(cursor, "company_tax_offences", company_id, "data_date DESC, id DESC")
        values = {} if not row else {
            "Сумма штрафа, ₽": _money(row.get("fine_amount")),
            "Дата документа": str(row.get("document_date")) if row.get("document_date") else None,
        }
    if not row:
        state = checked_state or PublicState.UNKNOWN
        negative_closure_proven = bool(
            (check or {}).get("negative_closure_proven")
        )
        if state == PublicState.NOT_FOUND and not negative_closure_proven:
            state = PublicState.UNKNOWN
        check_freshness = str((check or {}).get("freshness") or "").upper()
        freshness = Freshness.STALE if state == PublicState.STALE_DATA else (
            Freshness.CURRENT if check_freshness == "CURRENT" else Freshness.UNKNOWN
        )
        status = compile_source_status(
            state.value,
            source_date=_check_source_date(check),
            negative_closure_proven=negative_closure_proven,
        )
        return PublicSourceBlock(
            code=code, state=state, values={}, source_name=SOURCE_NAMES[code],
            source_data_date=_check_source_date(check),
            result_date=checked_result_date or default_result_date,
            freshness=freshness, limitation=status.explanation,
            negative_closure_proven=negative_closure_proven,
        )
    dataset = _dataset(cursor, row.get("dataset_id"))
    row_result_date = _date(row.get("updated_at") or row.get("created_at")) or default_result_date
    freshness = _freshness(dataset, default_result_date)
    row_state = PublicState.STALE_DATA if freshness == Freshness.STALE else PublicState.FOUND
    if checked_state == PublicState.NOT_FOUND:
        state = PublicState.CONFLICTING_EVIDENCE
    else:
        state = strongest_state(row_state, checked_state) if checked_state else row_state
    if state == PublicState.STALE_DATA:
        freshness = Freshness.STALE
    negative_closure_proven = bool((check or {}).get("negative_closure_proven"))
    if state == PublicState.NOT_FOUND and not negative_closure_proven:
        state = PublicState.UNKNOWN
    status = compile_source_status(
        state.value,
        source_date=_date(row.get("data_date")),
        negative_closure_proven=negative_closure_proven,
    )
    limitation = (
        status.explanation
        if state not in {PublicState.FOUND, PublicState.NOT_FOUND, PublicState.NOT_APPLICABLE}
        else None
    )
    return PublicSourceBlock(
        code=code, state=state, values=values,
        source_name=str(dataset.get("name") or SOURCE_NAMES[code])[:250],
        source_data_date=_date(row.get("data_date")),
        result_date=checked_result_date or row_result_date,
        freshness=freshness, limitation=limitation,
        negative_closure_proven=negative_closure_proven,
    )


def _risk_projection(risk: dict) -> PublicRisk:
    result_payload = risk.get("result_payload") or {}
    raw_factors = risk.get("factors") or result_payload.get("points") or ()
    factors = []
    seen_factor_codes: set[str] = set()
    unknown_factor_present = False
    for factor in raw_factors[:12]:
        factor_code = factor.get("factor_code")
        factor_ref = factor.get("factor_ref")
        source_refs = tuple(str(item) for item in factor.get("source_refs") or () if item)
        if not (factor_code and factor_ref and source_refs):
            unknown_factor_present = True
            continue
        if str(factor_code) in seen_factor_codes:
            continue
        source_code = factor.get("source_code")
        source_date = _date(factor.get("source_as_of") or factor.get("effective_at"))
        try:
            meaning_input = MeaningInput(
                meaning_id=f"meaning:{factor_ref}",
                factor_code=str(factor_code),
                factor_refs=(str(factor_ref),),
                fact_refs=(str(factor["fact_ref"]),) if factor.get("fact_ref") else (),
                source_refs=source_refs,
                source_dates=(source_date,) if source_date else (),
                current_state=(
                    "Исторический факт"
                    if str(factor.get("recency") or "").upper() == "HISTORICAL"
                    else "Текущий подтверждённый факт"
                ),
                recency={
                    "CURRENT": "Текущие сведения",
                    "RECENT": "Недавние сведения",
                    "HISTORICAL": "Исторические сведения",
                    "UNKNOWN": "Давность не определена",
                }.get(str(factor.get("recency") or "").upper()),
                confidence=1.0,
                recommendation_code=factor.get("recommendation_code"),
                ruleset_version=str(factor.get("rule_version") or risk.get("ruleset_version") or "v3"),
            )
        except ValueError:
            unknown_factor_present = True
            continue
        compiled = compile_meaning(meaning_input)
        if compiled is None:
            unknown_factor_present = True
            continue
        seen_factor_codes.add(str(factor_code))
        factors.append(
            PublicRiskFactor(
                meaning_id=compiled.meaning_id,
                category=compiled.category,
                severity=compiled.severity,
                title=compiled.headline,
                explanation=compiled.short_explanation,
                full_explanation=compiled.full_explanation,
                client_meaning=compiled.client_meaning,
                what_it_does_not_mean=compiled.what_it_does_not_mean,
                recommendation_effect=compiled.recommendation_effect,
                current_state=compiled.current_state,
                previous_state=compiled.previous_state,
                change=compiled.change,
                trend=compiled.trend,
                frequency=compiled.frequency,
                recency=compiled.recency,
                duration=compiled.duration,
                materiality=compiled.materiality,
                counter_evidence=compiled.counter_evidence,
                confidence=compiled.confidence,
                source_name=SOURCE_CODE_NAMES.get(source_code, "Официальный источник"),
                source_data_date=source_date,
            )
        )
    limitations: list[PublicLimitation] = []
    for item in risk.get("limitations") or ():
        code = item.get("limitation_code") if isinstance(item, dict) else item
        limitations.append(PublicLimitation.from_compiled(compile_limitation(str(code or ""))))
    if unknown_factor_present:
        limitations.append(PublicLimitation.from_compiled(compile_limitation(None)))
    aggregate_proof = aggregate_clean_conclusion_proof(risk) if not factors else None
    if not factors and (aggregate_proof is None or not aggregate_proof.proven):
        limitations.append(
            PublicLimitation.from_compiled(
                compile_limitation("AGGREGATE_CLEAN_PROOF_NOT_PROVEN")
            )
        )
    limitations = list(dict.fromkeys(limitations))
    is_partial = bool(result_payload.get("preliminary")) or bool(limitations)
    clean_proven = bool(aggregate_proof and aggregate_proof.proven)
    state = (
        PublicState.PARTIAL
        if is_partial
        else PublicState.FOUND
        if factors
        else PublicState.NOT_FOUND
        if clean_proven
        else PublicState.PARTIAL
    )
    if state == PublicState.PARTIAL:
        title = "Оценка содержит ограничения"
    elif factors:
        title = "Выявлены факторы, требующие внимания"
    else:
        title = "Неблагоприятные факторы не выявлены в выполненных проверках"
    explanation = (
        "Факторы приведены по сохранённым результатам проверки. Ограничения полноты не позволяют трактовать отсутствие отдельных сведений как отсутствие риска."
        if is_partial else
        "Факторы приведены по сохранённым результатам проверки без рейтинга или вероятностной интерпретации."
    )
    return PublicRisk(
        state=state, title=title, explanation=explanation, factors=tuple(factors),
        limitations=tuple(limitations),
        assessment_date=_aware(risk["calculated_at"]).date(),
        model_version=risk.get("risk_model_version") or risk.get("risk_engine_version"),
        ruleset_version=risk.get("ruleset_version"),
    )


def _summary_projection(summary: dict, public_risk: PublicRisk) -> PublicSummary:
    payload = summary.get("structured_payload") or {}
    main = tuple(item.public_headline for item in public_risk.factors)
    limitations = list(public_risk.limitations)
    for item in payload.get("limitations") or ():
        code = item.get("limitation_code") if isinstance(item, dict) else item
        limitations.append(PublicLimitation.from_compiled(compile_limitation(str(code or ""))))
    limitations = tuple(dict.fromkeys(limitations))
    recommendations: list[PublicRecommendation] = []
    for item in payload.get("recommendations") or ():
        code = item.get("recommendation_code") if isinstance(item, dict) else item
        compiled = compile_recommendation(str(code or ""))
        if compiled is not None:
            recommendations.append(PublicRecommendation.from_compiled(compiled))
    recommendations = list(dict.fromkeys(recommendations))
    if public_risk.factors:
        conclusion = "Выявлены факторы, требующие внимания. Их значение следует оценивать вместе с полнотой и датами исходных данных."
    elif public_risk.state == PublicState.NOT_FOUND:
        conclusion = compile_aggregate_clean_conclusion()
    else:
        conclusion = compile_aggregate_abstention_conclusion()
    return PublicSummary(
        short_conclusion=conclusion, main_factors=main,
        limitations=limitations, recommendations=tuple(recommendations),
        generated_at=_aware(summary["generated_at"]),
    )


def _load_latest(cursor, table: str, company_id: int, date_column: str) -> dict | None:
    return _latest_json(cursor, table, company_id, f"{date_column} DESC, id DESC")


def _index_eligible(
    company_info: CompanyInfo,
    sources: tuple[PublicSourceBlock, ...],
    public_risk: PublicRisk,
) -> bool:
    return bool(
        company_info.ogrn
        and public_risk.state in {PublicState.FOUND, PublicState.NOT_FOUND}
        and not public_risk.limitations
        and all(
            source.state in {PublicState.FOUND, PublicState.NOT_FOUND}
            for source in sources
        )
    )


def _semantic_value(view: CompanyViewModelV1, section_key: str, field_key: str):
    for section in view.sections:
        if section.section_key != section_key:
            continue
        for fact in section.facts:
            if fact.anchor.field_key == field_key:
                return fact.selected_evidence.value
    return None


def _public_reference(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(str(value))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    return str(value)


def _public_source(source_code: str, evidence) -> PublicFactSource:
    return PublicFactSource(
        name=_PUBLIC_SOURCE_NAMES.get(source_code, "Открытый источник"),
        source_class=_PUBLIC_SOURCE_CLASS_NAMES[evidence.source_class.value],
        reference=_public_reference(evidence.source_ref),
        source_data_date=evidence.source_data_date,
        retrieved_at=evidence.retrieved_at,
        confidence=evidence.confidence,
        freshness=Freshness(evidence.freshness.value),
    )


def _virtual_view_fact(
    *,
    company_id: int,
    section_key: str,
    field_key: str,
    value: Any,
    generated_at: datetime,
    source_name: str,
    source_data_date: date | None,
    identity: str | None = None,
) -> PublicViewFact:
    coordinate = f"{company_id}|{section_key}|{field_key}|{identity or ''}"
    return PublicViewFact(
        fact_ref=f"fact:{uuid5(_PUBLIC_VIEW_NAMESPACE, 'fact|' + coordinate)}",
        item_ref=f"item:{uuid5(_PUBLIC_VIEW_NAMESPACE, 'item|' + coordinate)}",
        field_key=field_key,
        label=_PUBLIC_FIELD_LABELS.get(field_key, field_key),
        value=value,
        state="Сведения найдены",
        source=PublicFactSource(
            name=source_name,
            source_class="Сохранённый публичный результат",
            source_data_date=source_data_date,
            retrieved_at=generated_at,
            confidence=1.0,
            freshness=Freshness.CURRENT,
        ),
    )


def _public_company_view(
    semantic_view: CompanyViewModelV1,
    *,
    company_id: int,
    risk: PublicRisk,
    summary: PublicSummary,
    sources: tuple[PublicSourceBlock, ...],
) -> PublicCompanyViewV1:
    sections: list[PublicViewSection] = []
    for section in semantic_view.sections:
        items = [
            PublicViewFact(
                fact_ref=fact.fact_ref,
                item_ref=fact.item_ref,
                field_key=fact.anchor.field_key,
                label=_PUBLIC_FIELD_LABELS.get(
                    fact.anchor.field_key,
                    fact.anchor.field_key,
                ),
                period=fact.anchor.period_identity or None,
                value=fact.selected_evidence.value,
                state=_PUBLIC_STATE_LABELS[fact.state],
                source=_public_source(fact.selected_evidence.source_code, fact.selected_evidence),
                alternative_sources=tuple(
                    _public_source(item.source_code, item)
                    for item in fact.alternative_evidence
                ),
                limitations=fact.selected_evidence.limitations,
            )
            for fact in section.facts
        ]
        if section.section_key == "risk":
            items.append(
                _virtual_view_fact(
                    company_id=company_id,
                    section_key="risk",
                    field_key="assessment",
                    value={
                        "title": risk.public_title,
                        "explanation": risk.public_explanation,
                        "assessment_date": risk.assessment_date.isoformat(),
                    },
                    generated_at=semantic_view.generated_at,
                    source_name="Сохранённая аналитическая оценка",
                    source_data_date=risk.assessment_date,
                )
            )
        elif section.section_key == "summary":
            items.append(
                _virtual_view_fact(
                    company_id=company_id,
                    section_key="summary",
                    field_key="conclusion",
                    value={"text": summary.short_conclusion},
                    generated_at=summary.generated_at,
                    source_name="Сохранённое резюме проверки",
                    source_data_date=summary.generated_at.date(),
                )
            )
        elif section.section_key == "source_coverage":
            for source in sources:
                items.append(
                    _virtual_view_fact(
                        company_id=company_id,
                        section_key="source_coverage",
                        field_key="source",
                        value={
                            "name": source.public_name,
                            "status": source.public_status,
                            "explanation": source.public_explanation,
                        },
                        generated_at=semantic_view.generated_at,
                        source_name=source.public_name,
                        source_data_date=source.source_data_date,
                        identity=source.code,
                    )
                )
        state = "Сведения найдены" if items else _PUBLIC_STATE_LABELS[section.state]
        sections.append(
            PublicViewSection(
                section_key=section.section_key,
                title=_PUBLIC_SECTION_TITLES.get(
                    section.section_key,
                    section.section_key,
                ),
                state=state,
                items=tuple(items),
            )
        )
    return PublicCompanyViewV1(
        revision=semantic_view.revision,
        generated_at=semantic_view.generated_at,
        inn=semantic_view.inn,
        sections=tuple(sections),
        links=semantic_view.links,
    )


def build_projection(cursor, inn: str, publication: PublicationInfo) -> PublicProjection:
    cursor.execute("SELECT to_jsonb(c) AS value FROM companies c WHERE inn=%s", (inn,))
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"INN {inn}: company is absent from Master")
    company = row["value"]
    if company.get("entity_type") != "legal":
        raise ValueError(f"INN {inn}: IP/non-legal entity rejected")
    semantic_view = build_company_view_v1(
        cursor,
        company_id=int(company["id"]),
        audience=Audience.PUBLIC,
        generated_at=publication.published_at,
    )
    risk = _load_latest(cursor, "company_risk_assessments_v3", company["id"], "calculated_at")
    if not risk:
        raise ValueError(f"INN {inn}: persisted Risk v3 is missing")
    summary = _load_latest(cursor, "company_summaries_v3", company["id"], "generated_at")
    if not summary or summary.get("risk_assessment_id") != risk.get("assessment_id"):
        raise ValueError(f"INN {inn}: matching persisted Summary v3 is missing")
    registration = _registration(risk)
    if registration.get("requested_inn") not in {None, inn}:
        raise ValueError(f"INN {inn}: registration evidence identity mismatch")
    manager_value = _semantic_value(semantic_view, "management", "manager") or {}
    projection_result_date = _aware(risk["calculated_at"]).date()
    sources = tuple(
        _source_block(cursor, code, company["id"], risk, projection_result_date)
        for code in ("REVEXP", "PAYTAX", "DEBTAM", "TAXOFFENCE")
    )
    public_risk = _risk_projection(risk)
    status = _semantic_value(semantic_view, "status", "status")
    company_info = CompanyInfo(
        name=_semantic_value(semantic_view, "identity", "short_name") or _semantic_value(semantic_view, "identity", "name"),
        full_name=_semantic_value(semantic_view, "identity", "full_name"),
        legal_status=str(status)[:240] if status else None,
        inn=inn,
        kpp=_semantic_value(semantic_view, "identity", "kpp"),
        ogrn=_semantic_value(semantic_view, "identity", "ogrn"),
        address=_semantic_value(semantic_view, "address", "registered_address"),
        registration_date=_date(_semantic_value(semantic_view, "registration", "registration_date")),
        director_name=manager_value.get("name") if isinstance(manager_value, dict) else None,
        director_position=manager_value.get("position") if isinstance(manager_value, dict) else None,
    )
    index_eligible = _index_eligible(company_info, sources, public_risk)
    content_candidates = [_aware(risk["calculated_at"]), _aware(summary["generated_at"])]
    for field in ("updated_at", "source_updated_at"):
        if company.get(field):
            content_candidates.append(_aware(company[field]))
    per_company_publication = publication.model_copy(
        update={
            "index_eligible": index_eligible,
            "content_updated_at": max(content_candidates),
            "result_date": projection_result_date,
        }
    )
    public_summary = _summary_projection(summary, public_risk)
    return PublicProjection(
        publication=per_company_publication,
        company=company_info,
        risk=public_risk,
        summary=public_summary,
        sources=sources,
        company_view=_public_company_view(
            semantic_view,
            company_id=int(company["id"]),
            risk=public_risk,
            summary=public_summary,
            sources=sources,
        ),
    )


def release_id_for(now: datetime, source_sha: str, cohort_sha256: str) -> str:
    return (
        f"public-v1-{now.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{source_sha[:8]}-{cohort_sha256[:8]}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / CANONICAL_COHORT_PATH)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--source-main-sha")
    parser.add_argument("--previous-release-id")
    parser.add_argument("--release-id")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")
    manifest_bytes = args.manifest.read_bytes()
    manifest = CanonicalManifest.model_validate_json(manifest_bytes)
    cohort_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    checksum_sidecar = args.manifest.with_suffix(args.manifest.suffix + ".sha256")
    if checksum_sidecar.is_file():
        recorded = checksum_sidecar.read_text(encoding="utf-8").split()[0]
        if recorded != cohort_manifest_sha256:
            raise ValueError("canonical cohort manifest checksum mismatch")
    source_sha = args.source_main_sha or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("source main SHA must be a full Git SHA")
    now = datetime.now(UTC)
    release_id = args.release_id or release_id_for(now, source_sha, cohort_manifest_sha256)
    if release_id in {args.previous_release_id, manifest.supersedes_release_id}:
        raise ValueError("new cohort requires a new release ID")
    bundle_dir = args.output_root / release_id
    base_publication = PublicationInfo(
        schema_version=SCHEMA_VERSION, release_id=release_id, published_at=now,
        result_date=now.date(), content_updated_at=now, index_eligible=False,
    )
    url = args.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            projections = [build_projection(cursor, entity.inn, base_publication) for entity in manifest.entities]
    expected_count = len(manifest.entities)
    if len(projections) != expected_count:
        raise ValueError("export did not produce the exact accepted cohort")
    bundle_dir.mkdir(parents=True, exist_ok=False)
    content_updated_at = max(item.publication.content_updated_at for item in projections)
    with gzip.open(bundle_dir / "companies.jsonl.gz", "wt", encoding="utf-8", newline="\n") as stream:
        for projection in projections:
            stream.write(canonical_json(projection.model_dump(mode="json")).decode("utf-8") + "\n")
    release_manifest = ReleaseManifest(
        schema_version=SCHEMA_VERSION, release_id=release_id, source_main_sha=source_sha,
        cohort_manifest_path=CANONICAL_COHORT_PATH,
        cohort_manifest_sha256=cohort_manifest_sha256,
        cohort_source_main_sha=manifest.source_main_sha,
        previous_release_id=args.previous_release_id, created_at=now,
        result_date=max(item.publication.result_date for item in projections),
        content_updated_at=content_updated_at, record_count=expected_count,
        companies_file="companies.jsonl.gz",
    )
    (bundle_dir / "manifest.json").write_bytes(canonical_json(release_manifest.model_dump(mode="json")) + b"\n")
    write_checksums(bundle_dir)
    print(json.dumps({"release_id": release_id, "record_count": expected_count, "bundle": str(bundle_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
