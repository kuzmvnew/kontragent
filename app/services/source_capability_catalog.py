"""Canonical product capabilities built from accepted runtime source paths."""

from __future__ import annotations

from app.contracts.risk import RiskProfile
from app.contracts.source_architecture import (
    ApplicabilityClass,
    CapabilityStatus,
    SourceCapability,
    SourceClass,
    SourcePath,
)

P = RiskProfile
PRECEDENCE = (
    SourceClass.OFFICIAL_DIRECT,
    SourceClass.OFFICIAL_DOWNLOADED_DATASET,
    SourceClass.AUTHORIZED_BRIDGE,
    SourceClass.DISCOVERY_ONLY,
)
ALL = (P.GENERAL_LE, P.IP, P.NEW_COMPANY, P.FINANCIAL_ORG, P.NON_PROFIT)


def _path(code: str, kind: SourceClass, priority: int) -> SourcePath:
    return SourcePath(source_code=code, source_class=kind, priority=priority)


def _cap(
    capability_id: str, domain: str, human_name: str, coverage_weight: float,
    paths: tuple[SourcePath, ...], *, risk_weight: float = 0,
    profiles: tuple[RiskProfile, ...] = ALL, mandatory: tuple[RiskProfile, ...] = ALL,
    bridge_allowed: bool = False, negative_bridge_allowed: bool = False,
    runner: str | None = None, fallback: str | None = None,
    status: CapabilityStatus = CapabilityStatus.ACTIVE,
    applicability: ApplicabilityClass = ApplicabilityClass.MANDATORY_ALWAYS,
    applicability_basis: str = "Applies to every supported company profile.",
) -> SourceCapability:
    return SourceCapability(
        capability_id=capability_id, domain=domain, human_name=human_name,
        profiles=profiles, mandatory_for_profiles=mandatory,
        applicability_class=applicability,
        applicability_basis=applicability_basis,
        risk_weight=risk_weight, coverage_weight=coverage_weight,
        freshness_policy={"max_age_days": 45}, accepted_source_paths=paths,
        source_precedence=PRECEDENCE, bridge_allowed=bridge_allowed,
        negative_bridge_allowed=negative_bridge_allowed, runner=runner,
        fallback_runner=fallback, status=status,
    )


CAPABILITIES: tuple[SourceCapability, ...] = (
    _cap("registration", "registration", "Регистрационный статус", 15, (
        _path("fns_egrul_egrip", SourceClass.OFFICIAL_DOWNLOADED_DATASET, 10),
        _path("firmoteka_registration", SourceClass.AUTHORIZED_BRIDGE, 20),
    ), risk_weight=12, bridge_allowed=True, negative_bridge_allowed=True, runner="master_registry", fallback="firmoteka"),
    _cap("bankruptcy", "bankruptcy", "Банкротство и ликвидация", 13, (
        _path("efrsb_direct", SourceClass.OFFICIAL_DIRECT, 10),
        _path("egrul_events", SourceClass.OFFICIAL_DOWNLOADED_DATASET, 20),
        _path("firmoteka_bankruptcy", SourceClass.AUTHORIZED_BRIDGE, 30),
    ), risk_weight=18, bridge_allowed=True, runner="efrsb_direct", fallback="firmoteka"),
    _cap("fssp", "enforcement", "Исполнительные производства ФССП", 10, (
        _path("fssp_direct", SourceClass.OFFICIAL_DIRECT, 10),
        _path("firmoteka_fssp", SourceClass.AUTHORIZED_BRIDGE, 20),
    ), risk_weight=12, bridge_allowed=True, negative_bridge_allowed=False, runner="fssp_direct", fallback="firmoteka"),
    _cap("tax_debt", "tax", "Налоговая задолженность", 10, (
        _path("fns_tax_debt", SourceClass.OFFICIAL_DOWNLOADED_DATASET, 10),
        _path("firmoteka_tax", SourceClass.AUTHORIZED_BRIDGE, 20),
    ), risk_weight=10, bridge_allowed=True, runner="fns_tax_debt"),
    _cap("tax_offence", "tax", "Налоговые правонарушения", 4, (
        _path("fns_tax_offence", SourceClass.OFFICIAL_DOWNLOADED_DATASET, 10),
    ), risk_weight=4, runner="fns_tax_offence"),
    _cap("finance", "finance", "Финансовая отчётность", 10, (
        _path("fns_revenue_expenses", SourceClass.OFFICIAL_DOWNLOADED_DATASET, 10),
        _path("firmoteka_finance", SourceClass.AUTHORIZED_BRIDGE, 20),
    ), risk_weight=12, bridge_allowed=True, runner="fns_revenue_expenses"),
    _cap("arbitration", "courts", "Арбитражные дела", 8, (
        # Checko is an authorised data bridge to court records, not the
        # official court publisher.  KAD remains the direct official path.
        _path("checko_arbitration", SourceClass.AUTHORIZED_BRIDGE, 10),
        _path("kad_public", SourceClass.OFFICIAL_DIRECT, 20),
        _path("firmoteka_courts", SourceClass.AUTHORIZED_BRIDGE, 30),
    ), risk_weight=7, bridge_allowed=True, runner="arbitration_resolver"),
    _cap("general_courts", "courts", "Суды общей юрисдикции", 6, (
        _path("official_general_courts", SourceClass.OFFICIAL_DIRECT, 10),
        _path("firmoteka_courts", SourceClass.AUTHORIZED_BRIDGE, 20),
    ), risk_weight=3, bridge_allowed=True, runner="general_court_resolver"),
    _cap("cbr_zsk", "compliance", "Высокая группа риска Банка России", 5, (
        _path("cbr_zsk_direct", SourceClass.OFFICIAL_DIRECT, 10),
    ), risk_weight=5, runner="cbr_zsk_direct"),
    _cap("cbr_warning", "compliance", "Предупредительный список Банка России", 4, (
        _path("cbr_warning_list", SourceClass.OFFICIAL_DOWNLOADED_DATASET, 10),
    ), risk_weight=3, runner="cbr_warning_list"),
    _cap("bankinform", "compliance", "Приостановления операций ФНС", 4, (
        _path("fns_bankinform_direct", SourceClass.OFFICIAL_DIRECT, 10),
    ), risk_weight=2, mandatory=(), runner="fns_bankinform_direct",
        applicability=ApplicabilityClass.OPTIONAL_CONTEXT,
        applicability_basis="Requires a bank/account verification context and the querying bank BIK."),
    _cap("management", "management", "Руководители и дисквалификация", 4, (
        _path("fns_disqualified", SourceClass.OFFICIAL_DOWNLOADED_DATASET, 10),
        _path("firmoteka_management", SourceClass.AUTHORIZED_BRIDGE, 20),
    ), risk_weight=5, bridge_allowed=True, runner="fns_disqualified"),
    _cap("licences_sro", "licences", "Лицензии и СРО", 4, (
        _path("official_licence_registries", SourceClass.OFFICIAL_DOWNLOADED_DATASET, 10),
        _path("firmoteka_licences_sro", SourceClass.AUTHORIZED_BRIDGE, 20),
    ), risk_weight=3, bridge_allowed=True, runner="licence_resolver",
        applicability=ApplicabilityClass.MANDATORY_IF_APPLICABLE,
        applicability_basis="Required for regulated activities identified by OKVED; SRO route depends on construction/design scope."),
    _cap("procurement_rnp", "procurement", "РНП", 2, (
        _path("eis_rnp", SourceClass.OFFICIAL_DIRECT, 10),
        _path("firmoteka_procurement", SourceClass.AUTHORIZED_BRIDGE, 20),
    ), risk_weight=2, bridge_allowed=True, mandatory=(), runner="eis_rnp", status=CapabilityStatus.ACCESS_PENDING,
        applicability=ApplicabilityClass.DEFERRED_EXTERNAL_ACCESS,
        applicability_basis="Relevant to procurement due diligence, but official machine access is not yet connected."),
    _cap("regulatory_inspections", "inspections", "Контрольные мероприятия", 1, (
        _path("erknm_inspections", SourceClass.OFFICIAL_DOWNLOADED_DATASET, 10),
    ), risk_weight=2, runner="erknm",
        applicability=ApplicabilityClass.MANDATORY_ALWAYS,
        applicability_basis="Official inspection history can exist for any registered business; absence is limited to loaded periods."),
)


class SourceCapabilityCatalog:
    def __init__(self, capabilities: tuple[SourceCapability, ...] = CAPABILITIES):
        self._items = {item.capability_id: item for item in capabilities}

    def get(self, capability_id: str) -> SourceCapability:
        return self._items[capability_id]

    def for_profile(self, profile: RiskProfile) -> tuple[SourceCapability, ...]:
        return tuple(item for item in self._items.values() if profile in item.profiles)

    def all(self) -> tuple[SourceCapability, ...]:
        return tuple(self._items.values())


CATALOG = SourceCapabilityCatalog()
