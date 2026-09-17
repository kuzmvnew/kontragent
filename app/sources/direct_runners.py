"""Fail-closed official direct runners with injectable authorized transports.

The runners normalize actual official-flow responses.  They do not bypass
challenges and do not turn transport/challenge failures into NOT_FOUND.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from app.contracts.source_architecture import (
    FreshnessStatus,
    NormalizedCheckResult,
    NormalizedEvidence,
    NormalizedResultStatus,
    SourceClass,
)
from app.providers.protected_public_source import (
    parse_cbr_zsk_result_text,
    parse_fns_bankinform_result_text,
)

Transport = Callable[..., dict[str, Any]]


def _now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _evidence(code: str, value: Any) -> tuple[NormalizedEvidence, ...]:
    return (NormalizedEvidence(fact=code, value=value, evidence_id=f"{code}:{_hash(value)[:16]}", snapshot_hash=_hash(value)),)


def _unavailable(check_code: str, source_code: str, url: str, reason: str, checked_at: datetime) -> NormalizedCheckResult:
    return NormalizedCheckResult(
        check_code=check_code, result=NormalizedResultStatus.UNAVAILABLE,
        source_class=SourceClass.OFFICIAL_DIRECT, source_code=source_code,
        exact_identifier_match=None, checked_at=checked_at, freshness=FreshnessStatus.UNKNOWN,
        coverage=0, confidence=0, source_url=url, limitation=reason,
    )


class FsspDirectRunner:
    source_url = "https://fssp.gov.ru/iss/ip/"

    def __init__(self, transport: Transport | None = None): self.transport = transport

    def run(self, inn: str, *, checked_at: datetime | None = None) -> NormalizedCheckResult:
        checked_at = _now(checked_at)
        if self.transport is None:
            return _unavailable("fssp", "fssp_direct", self.source_url, "Машинный интерфейс не настроен; публичная проверка требует отдельной браузерной сессии.", checked_at)
        try:
            raw = self.transport(inn=inn)
        except Exception as error:
            return _unavailable("fssp", "fssp_direct", self.source_url, f"Ошибка official flow: {type(error).__name__}", checked_at)
        if raw.get("challenge_required"):
            return _unavailable("fssp", "fssp_direct", self.source_url, "Официальный flow требует challenge; секреты не сохраняются.", checked_at)
        if raw.get("exact_identifier_match") is not True:
            return _unavailable("fssp", "fssp_direct", self.source_url, "Прямой ответ не подтверждает exact-INN.", checked_at)
        records = raw.get("records") or []
        found = bool(records or raw.get("count", 0))
        value = {
            "active_proceedings": records,
            "count": raw.get("count", len(records)),
            "amount": raw.get("amount"), "remaining_amount": raw.get("remaining_amount"),
            "production_types": raw.get("production_types") or [], "dates": raw.get("dates") or [],
            "identifiers": raw.get("identifiers") or [],
        }
        return NormalizedCheckResult(
            check_code="fssp", result=NormalizedResultStatus.FOUND if found else NormalizedResultStatus.NOT_FOUND,
            source_class=SourceClass.OFFICIAL_DIRECT, source_code="fssp_direct", original_source="ФССП",
            exact_identifier_match=True, checked_at=checked_at, source_as_of=raw.get("source_as_of"),
            freshness=FreshnessStatus.CURRENT, coverage=1, confidence=1,
            evidence=_evidence("fssp_direct", value), source_url=raw.get("source_url") or self.source_url,
        )


class EfrsbDirectRunner:
    source_url = "https://bankrot.fedresurs.ru/"

    def __init__(self, transport: Transport | None = None): self.transport = transport

    def run(self, inn: str, *, checked_at: datetime | None = None) -> NormalizedCheckResult:
        checked_at = _now(checked_at)
        if self.transport is None:
            return _unavailable("bankruptcy", "efrsb_direct", self.source_url, "Публичный/машинный flow ЕФРСБ не настроен в текущей среде.", checked_at)
        try:
            raw = self.transport(inn=inn)
        except Exception as error:
            return _unavailable("bankruptcy", "efrsb_direct", self.source_url, f"Ошибка official flow: {type(error).__name__}", checked_at)
        if raw.get("challenge_required") or raw.get("http_status") in {401, 403, 429}:
            return _unavailable("bankruptcy", "efrsb_direct", self.source_url, "Официальный flow недоступен или требует challenge.", checked_at)
        if raw.get("exact_identifier_match") is not True:
            return _unavailable("bankruptcy", "efrsb_direct", self.source_url, "Прямой ответ не подтверждает exact-INN.", checked_at)
        events = raw.get("events") or []
        return NormalizedCheckResult(
            check_code="bankruptcy", result=NormalizedResultStatus.FOUND if events else NormalizedResultStatus.NOT_FOUND,
            source_class=SourceClass.OFFICIAL_DIRECT, source_code="efrsb_direct", original_source="ЕФРСБ/Fedresurs",
            exact_identifier_match=True, checked_at=checked_at, source_as_of=raw.get("source_as_of"),
            freshness=FreshnessStatus.CURRENT, coverage=1, confidence=1,
            evidence=_evidence("efrsb_direct", {"inn":inn,"debtor":raw.get("debtor"),"events":events}),
            source_url=raw.get("source_url") or self.source_url,
        )


class CbrZskRunner:
    source_url = "https://www.cbr.ru/counteraction_m_ter/platform_zsk/proverka-po-inn/"

    def __init__(self, transport: Transport | None = None): self.transport = transport

    def run(self, inn: str, *, purpose: str, initiator: str, checked_at: datetime | None = None) -> NormalizedCheckResult:
        checked_at = _now(checked_at)
        if not purpose or not initiator:
            raise ValueError("request purpose and initiator are required")
        if self.transport is None:
            return _unavailable("cbr_zsk", "cbr_zsk_direct", self.source_url, "Публичная проверка требует интерактивного подтверждения.", checked_at)
        raw = self.transport(inn=inn, purpose=purpose, initiator=initiator)
        parsed = parse_cbr_zsk_result_text(raw.get("text", ""))
        mapping = {
            "high_risk_information_found": NormalizedResultStatus.FOUND,
            "high_risk_information_not_found": NormalizedResultStatus.NOT_FOUND,
        }
        status = mapping.get(parsed["result"])
        if status is None:
            return _unavailable("cbr_zsk", "cbr_zsk_direct", self.source_url, "CBR flow не вернул завершённый результат.", checked_at)
        return NormalizedCheckResult(
            check_code="cbr_zsk", result=status, source_class=SourceClass.OFFICIAL_DIRECT,
            source_code="cbr_zsk_direct", original_source="Банк России, Платформа ЗСК",
            exact_identifier_match=True, checked_at=checked_at, source_as_of=raw.get("source_as_of"),
            freshness=FreshnessStatus.CURRENT, coverage=1, confidence=1,
            evidence=_evidence("cbr_zsk_direct", {"inn":inn,"result":parsed["result"],"purpose":purpose,"initiator":initiator,"evidence_hash":parsed["evidence_hash"]}),
            source_url=self.source_url,
        )


class FnsBankinformRunner:
    source_url = "https://service.nalog.ru/bi.do"

    def __init__(self, transport: Transport | None = None): self.transport = transport

    def run(self, inn: str, *, bik: str | None, checked_at: datetime | None = None) -> NormalizedCheckResult:
        checked_at = _now(checked_at)
        if not bik:
            return _unavailable("bankinform", "fns_bankinform_direct", self.source_url, "Сервис требует БИК; корректное значение не предоставлено.", checked_at)
        if len(bik) not in {9, 10} or not bik.isdigit():
            raise ValueError("BIK must contain 9 or 10 digits according to the selected public-flow contract")
        if self.transport is None:
            return _unavailable("bankinform", "fns_bankinform_direct", self.source_url, "Публичная проверка не подключена.", checked_at)
        raw = self.transport(inn=inn, bik=bik)
        parsed = parse_fns_bankinform_result_text(raw.get("text", ""))
        mapping = {
            "active_suspensions_found": NormalizedResultStatus.FOUND,
            "active_suspensions_not_found": NormalizedResultStatus.NOT_FOUND,
        }
        status = mapping.get(parsed["result"])
        if status is None:
            return _unavailable("bankinform", "fns_bankinform_direct", self.source_url, "ФНС flow не вернул завершённый результат.", checked_at)
        value = {"inn":inn,"bik":bik,"result":parsed["result"],"decisions":raw.get("decisions") or [],"evidence_hash":parsed["evidence_hash"]}
        return NormalizedCheckResult(
            check_code="bankinform", result=status, source_class=SourceClass.OFFICIAL_DIRECT,
            source_code="fns_bankinform_direct", original_source="ФНС БАНКИНФОРМ",
            exact_identifier_match=True, checked_at=checked_at, source_as_of=raw.get("source_as_of"),
            freshness=FreshnessStatus.CURRENT, coverage=1, confidence=1,
            evidence=_evidence("fns_bankinform_direct", value), source_url=self.source_url,
        )


class ArbitrationResolver:
    """Resolves Checko/KAD/bridge by canonical precedence and preserves scope."""
    def __init__(self, resolver): self.resolver = resolver
    def resolve(self, results): return self.resolver.resolve(results).get("arbitration")
