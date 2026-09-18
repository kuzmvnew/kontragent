"""Evidence-based capability applicability for one company and deal context.

The policy is deliberately separate from source availability.  It may remove a
truly irrelevant check from the denominator, but it must never turn a blocked
or unconfigured source into a successful negative result.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from app.contracts.source_architecture import (
    FreshnessStatus,
    NormalizedCheckResult,
    NormalizedEvidence,
    NormalizedResultStatus,
    SourceClass,
)


# Primary-OKVED rules are intentionally conservative.  A positive source fact
# always wins over an inferred N/A because secondary activities may be absent
# from the compact company read model.
CONSTRUCTION_SRO_PREFIXES = ("41", "42", "43")
DESIGN_SRO_PREFIXES = ("71.1", "71.12")
LICENSABLE_ACTIVITY_PREFIXES = (
    "11",       # production of alcoholic beverages
    "12",       # manufacture of tobacco products
    "21.20",    # pharmaceuticals
    "38.32.4",  # processing non-ferrous metal scrap
    "46.46",    # wholesale pharmaceuticals
    "47.73",    # pharmacy retail
    "80.10",    # private security
    "85.42.1",  # driving schools
    "86",       # healthcare
)
FINANCIAL_ACTIVITY_PREFIXES = ("64", "65", "66")
ROSZDRAV_ACTIVITY_PREFIXES = ("21.20", "32.50", "46.46", "47.73", "86")


def primary_okved(company: Mapping[str, Any]) -> str:
    value = company.get("okved")
    if isinstance(value, Mapping):
        value = value.get("code") or value.get("main")
    return str(value or "").strip()


def okved_matches(company: Mapping[str, Any], prefixes: tuple[str, ...]) -> bool:
    code = primary_okved(company)
    return bool(code and any(code == prefix or code.startswith(f"{prefix}.") for prefix in prefixes))


def licence_sro_applicability(company: Mapping[str, Any]) -> dict[str, Any]:
    code = primary_okved(company)
    if not code:
        return {
            "applicable": None,
            "okved": None,
            "nostroy": None,
            "nopriz": None,
            "licensed_activity": None,
        }
    construction = okved_matches(company, CONSTRUCTION_SRO_PREFIXES)
    design = okved_matches(company, DESIGN_SRO_PREFIXES)
    licensed = okved_matches(company, LICENSABLE_ACTIVITY_PREFIXES)
    return {
        "applicable": construction or design or licensed,
        "okved": code or None,
        "nostroy": construction,
        "nopriz": design,
        "licensed_activity": licensed,
    }


def _policy_result(code: str, result: NormalizedResultStatus, reason: str, value: dict[str, Any], now: datetime) -> NormalizedCheckResult:
    not_applicable = result == NormalizedResultStatus.NOT_APPLICABLE
    return NormalizedCheckResult(
        check_code=code,
        result=result,
        source_class=SourceClass.POLICY_RULE,
        source_code="capability_applicability_policy",
        original_source="Политика применимости проверок",
        exact_identifier_match=None,
        checked_at=now,
        freshness=FreshnessStatus.CURRENT,
        coverage=1 if not_applicable else 0,
        confidence=1,
        evidence=(
            NormalizedEvidence(
                fact="Применимость проверки",
                value=value,
                evidence_id=f"applicability:{code}:{value.get('inn') or 'context'}",
            ),
        ),
        limitation=None if not_applicable else reason,
    )


def apply_capability_applicability(
    resolved: Mapping[str, NormalizedCheckResult],
    *,
    company: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, NormalizedCheckResult]:
    """Apply N/A only when entity/activity/context makes that conclusion safe."""

    output = dict(resolved)
    context = context or {}
    now = now or datetime.now(timezone.utc)
    inn = str(company.get("inn") or "")

    licence_policy = licence_sro_applicability(company)
    current_licence = output.get("licences_sro")
    if licence_policy["applicable"] is False and not (
        current_licence and current_licence.result == NormalizedResultStatus.FOUND
    ):
        output["licences_sro"] = _policy_result(
            "licences_sro",
            NormalizedResultStatus.NOT_APPLICABLE,
            "",
            {"inn": inn, **licence_policy, "basis": "primary_okved_no_regulated_activity_signal"},
            now,
        )
    elif licence_policy["applicable"] is True and current_licence is None:
        output["licences_sro"] = _policy_result(
            "licences_sro",
            NormalizedResultStatus.UNAVAILABLE,
            "Для заявленного вида деятельности требуется профильная проверка лицензии или СРО.",
            {"inn": inn, **licence_policy, "basis": "regulated_primary_okved"},
            now,
        )

    # Bankinform is a bank-facing query: the current public form asks for the
    # taxpayer INN and the BIK of the bank executing the query.  Without that
    # deal/account context, absence cannot be checked and must not be shown as
    # an outage or as a negative result.
    bik = "".join(character for character in str(context.get("bank_bik") or context.get("bik") or "") if character.isdigit())
    current_bankinform = output.get("bankinform")
    if not bik and not (
        current_bankinform and current_bankinform.result == NormalizedResultStatus.FOUND
    ):
        output["bankinform"] = _policy_result(
            "bankinform",
            NormalizedResultStatus.NOT_APPLICABLE,
            "",
            {"inn": inn, "bank_bik_supplied": False, "basis": "bank_or_account_context_absent"},
            now,
        )

    # RNP access is deferred.  Missing procurement context is not sufficient
    # evidence for a universal N/A: the company may have participated in public
    # procurement outside the compact card data.
    if "procurement_rnp" not in output:
        output["procurement_rnp"] = _policy_result(
            "procurement_rnp",
            NormalizedResultStatus.UNAVAILABLE,
            "Проверка РНП отложена до подключения официального доступа; контекст закупки сам по себе не доказывает отсутствие записи.",
            {
                "inn": inn,
                "procurement_context": bool(context.get("public_procurement")),
                "basis": "deferred_external_access",
            },
            now,
        )

    return output
