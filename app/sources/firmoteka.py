"""Firmoteka authorized-bridge adapter for normalized cached page facts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from app.contracts.source_architecture import (
    FreshnessStatus,
    NormalizedCheckResult,
    NormalizedEvidence,
    NormalizedResultStatus,
    SourceClass,
)


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _evidence(check: str, fact: str, value: Any, payload: dict[str, Any]) -> NormalizedEvidence:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return NormalizedEvidence(
        fact=fact, value=value, evidence_id=f"firmoteka:{payload.get('requested_inn')}:{check}",
        snapshot_hash=payload.get("page_sha256") or hashlib.sha256(encoded).hexdigest(),
    )


def concrete_bankruptcy_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return concrete procedure evidence; never trust a generic badge/boolean."""
    text = " ".join(filter(None, (
        str(payload.get("status_detail") or ""),
        str(payload.get("status_reason") or ""),
        str(payload.get("bankruptcy_excerpt") or ""),
    )))
    patterns = (
        ("competitive_proceedings", r"открыт[оа].{0,40}конкурсн(?:ое|ого) производств"),
        ("observation", r"введен[оа].{0,30}наблюдени"),
        ("external_management", r"введен[оа].{0,30}внешн(?:ее|его) управлен"),
        ("declared_bankrupt", r"признан[оа]?.{0,30}(?:несостоятельн|банкрот)"),
        ("case_initiated", r"возбужден[оа]?.{0,40}(?:дел[оа]|производств).{0,40}(?:несостоятельн|банкрот)"),
    )
    for procedure, pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return {"procedure": procedure, "text": match.group(0), "full_text": text}
    return None


class FirmotekaSourceAdapter:
    source_class = SourceClass.AUTHORIZED_BRIDGE
    source_code = "firmoteka"

    def normalize(self, payload: dict[str, Any], *, requested_inn: str | None = None) -> tuple[NormalizedCheckResult, ...]:
        inn = requested_inn or str(payload.get("requested_inn") or "")
        exact = bool(inn and str(payload.get("rendered_inn") or payload.get("requested_inn")) == inn and payload.get("identity_match", True))
        checked_at = _dt(payload.get("fetched_at"))
        as_of = _dt(payload.get("fns_egrul_as_of"))
        common = dict(source_class=self.source_class, exact_identifier_match=exact, checked_at=checked_at,
                      source_as_of=as_of, freshness=FreshnessStatus.CURRENT, source_url=payload.get("url"))
        results: list[NormalizedCheckResult] = []
        registration = {key: payload.get(key) for key in (
            "requested_inn", "ogrn", "kpp", "name", "full_name", "entity_type", "legal_form",
            "status_normalized", "status_detail", "registration_date", "termination_date", "address",
            "region", "okved", "okved_name",
        )}
        registration_ok = exact and all(payload.get(key) for key in ("requested_inn", "status_normalized", "registration_date"))
        results.append(NormalizedCheckResult(
            check_code="registration", result=NormalizedResultStatus.FOUND if registration_ok else NormalizedResultStatus.PARTIAL,
            source_code="firmoteka_registration", original_source="ФНС ЕГРЮЛ/ЕГРИП (как заявлено страницей)",
            coverage=1 if registration_ok else .5, confidence=.9 if registration_ok else .5,
            evidence=(_evidence("registration", "Регистрационные сведения", registration, payload),),
            limitation=None if registration_ok else "Нет exact-INN, статуса или даты регистрации.", **common,
        ))
        fssp_count = payload.get("fssp_count")
        fssp_found = isinstance(fssp_count, int) and fssp_count > 0
        fssp_known = isinstance(fssp_count, int)
        enforcements = payload.get("enforcements") if isinstance(payload.get("enforcements"), dict) else {}
        enforcement_items = enforcements.get("items") if isinstance(enforcements.get("items"), list) else []
        results.append(NormalizedCheckResult(
            check_code="fssp",
            result=(NormalizedResultStatus.FOUND if fssp_found else NormalizedResultStatus.NOT_FOUND if fssp_known else NormalizedResultStatus.UNAVAILABLE),
            source_code="firmoteka_fssp", original_source="Банк данных исполнительных производств ФССП",
            coverage=1 if fssp_found else .75 if fssp_known else 0, confidence=.9 if fssp_known else 0,
            evidence=(_evidence("fssp", "Исполнительные производства", {
                "count": fssp_count,
                "total_due": enforcements.get("total_due"),
                "remaining_amount": payload.get("fssp_remaining_amount"),
                "completed_count": enforcements.get("completed_count"),
                "closed_count": enforcements.get("closed_count"),
                "snapshot": enforcements.get("snapshot"),
                "stats": enforcements.get("stats") or [],
                "items": enforcement_items,
                "production_types": sorted({
                    str(item.get("subject")) for item in enforcement_items
                    if isinstance(item, dict) and item.get("subject")
                }),
                "as_collector": enforcements.get("as_collector"),
            }, payload),) if fssp_known else (),
            limitation=None if fssp_found else "Отрицательный bridge-результат не эквивалентен прямой проверке ФССП." if fssp_known else "Раздел ФССП отсутствует.", **common,
        ))
        event = concrete_bankruptcy_event(payload)
        generic = bool(payload.get("bankruptcy_indicator"))
        results.append(NormalizedCheckResult(
            check_code="bankruptcy",
            result=NormalizedResultStatus.FOUND if event else NormalizedResultStatus.PARTIAL if generic else NormalizedResultStatus.UNAVAILABLE,
            source_code="firmoteka_bankruptcy" if event else "firmoteka_bankruptcy_discovery",
            source_class=SourceClass.AUTHORIZED_BRIDGE if event else SourceClass.DISCOVERY_ONLY,
            original_source="ЕГРЮЛ/ЕФРСБ (как заявлено страницей)", exact_identifier_match=exact,
            checked_at=checked_at, source_as_of=as_of, freshness=FreshnessStatus.CURRENT,
            coverage=.9 if event else .2 if generic else 0, confidence=.9 if event else .25 if generic else 0,
            evidence=(_evidence("bankruptcy", "Конкретное событие банкротства", event, payload),) if event else (),
            source_url=payload.get("url"), limitation=None if event else "Нет конкретного события банкротства; общий badge не является подтверждением.",
        ))
        for code, source_code, fact, value in (
            ("finance", "firmoteka_finance", "Финансовые показатели", payload.get("financials")),
            ("tax_debt", "firmoteka_tax", "Налоговая задолженность", payload.get("tax_debts")),
            ("management", "firmoteka_management", "Руководители и учредители", {"manager": payload.get("manager"), "position": payload.get("manager_position"), "founders": payload.get("founders")}),
            ("licences_sro", "firmoteka_licences_sro", "Лицензии и СРО", {"licenses": payload.get("licenses"), "sro": payload.get("sro")}),
            ("procurement_rnp", "firmoteka_procurement", "Закупки и РНП", payload.get("procurement")),
        ):
            known = bool(value) and (not isinstance(value, dict) or any(v for v in value.values()))
            results.append(NormalizedCheckResult(
                check_code=code, result=NormalizedResultStatus.FOUND if known else NormalizedResultStatus.UNAVAILABLE,
                source_code=source_code, original_source="Источник, указанный на странице Firmoteka",
                coverage=.8 if known else 0, confidence=.85 if known else 0,
                evidence=(_evidence(code, fact, value, payload),) if known else (),
                limitation=None if known else f"Firmoteka не дала структурированный результат: {fact.lower()}.", **common,
            ))
        return tuple(results)
