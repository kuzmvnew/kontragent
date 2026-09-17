from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


FNS_BANKINFORM_URL = "https://service.nalog.ru/bi.do"
CBR_ZSK_URL = "https://cbr.ru/counteraction_m_ter/platform_zsk/proverka-po-inn/"
FSSP_ENFORCEMENT_URL = "https://fssp.gov.ru/iss/ip/"


@dataclass(frozen=True)
class ProtectedSourceDefinition:
    code: str
    source_url: str
    allowed_results: frozenset[str]


PROTECTED_SOURCES = {
    "fns_bankinform": ProtectedSourceDefinition(
        code="fns_bankinform",
        source_url=FNS_BANKINFORM_URL,
        allowed_results=frozenset({"active_suspensions_found", "active_suspensions_not_found", "unavailable", "challenge_required"}),
    ),
    "cbr_zsk": ProtectedSourceDefinition(
        code="cbr_zsk",
        source_url=CBR_ZSK_URL,
        allowed_results=frozenset({"high_risk_information_found", "high_risk_information_not_found", "unavailable", "challenge_required"}),
    ),
    "fssp": ProtectedSourceDefinition(
        code="fssp",
        source_url=FSSP_ENFORCEMENT_URL,
        allowed_results=frozenset({
            "enforcement_found", "enforcement_not_found",
            "unavailable", "challenge_required",
        }),
    ),
}


def evidence_sha256(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def parse_fns_bankinform_result_text(text: str) -> dict:
    normalized = " ".join(str(text or "").split())
    if re.search(r"подтвердите,? что вы не робот|превысили лимит запросов", normalized, re.I):
        return {"result": "challenge_required", "records": [], "evidence_hash": evidence_sha256(normalized)}
    if re.search(r"не найден|не имеется|отсутств", normalized, re.I):
        return {"result": "active_suspensions_not_found", "records": [], "evidence_hash": evidence_sha256(normalized)}
    if re.search(r"решени[яй].*приостановлени", normalized, re.I):
        return {"result": "active_suspensions_found", "records": [], "evidence_hash": evidence_sha256(normalized)}
    return {"result": "unavailable", "records": [], "evidence_hash": evidence_sha256(normalized)}


def parse_cbr_zsk_result_text(text: str) -> dict:
    normalized = " ".join(str(text or "").split())
    if re.search(r"smartcaptcha|подтвердите,? что вы не робот", normalized, re.I):
        return {"result": "challenge_required", "evidence_hash": evidence_sha256(normalized)}
    has_risk_context = bool(re.search(r"сведени", normalized, re.I) and re.search(r"высок", normalized, re.I))
    has_explicit_absence = bool(re.search(r"не найден|не имеют|отсутств", normalized, re.I))
    if has_risk_context and has_explicit_absence:
        return {"result": "high_risk_information_not_found", "evidence_hash": evidence_sha256(normalized)}
    if re.search(r"сведени[яй].*(?:высок|высокой).*(?:найден|имеют|содерж)", normalized, re.I):
        return {"result": "high_risk_information_found", "evidence_hash": evidence_sha256(normalized)}
    return {"result": "unavailable", "evidence_hash": evidence_sha256(normalized)}
