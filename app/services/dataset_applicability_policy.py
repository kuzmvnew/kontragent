"""Provisioning-only applicability policy for canonical datasets.

Runtime decisions remain exclusively authoritative from ``DataSet.applicability``.
This module is used by registration and controlled recovery code; it is never a
fallback for a missing persisted policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal


EntityScope = Literal["legal", "individual_entrepreneur", "both"]


@dataclass(frozen=True)
class DatasetApplicabilityPolicy:
    scope: EntityScope
    basis: str

    @property
    def entity_types(self) -> tuple[str, ...]:
        if self.scope == "both":
            return ("legal", "individual_entrepreneur")
        return (self.scope,)

    def as_persisted(self) -> dict[str, list[str]]:
        return {"entity_types": list(self.entity_types)}


def _policy(scope: EntityScope, basis: str) -> DatasetApplicabilityPolicy:
    return DatasetApplicabilityPolicy(scope=scope, basis=basis)


# Every entry is an explicit provisioning declaration grounded in the named
# source/data contract.  Code prefixes and current database contents are not
# consulted.  The two NRS entries are included for their existing registration
# paths even though they are absent from the 2026-09-28 production inventory.
CANONICAL_DATASET_POLICIES = MappingProxyType(
    {
        "excel_companies": _policy("both", "Excel master import contract"),
        "dadata_company_lookup": _policy("both", "DaData exact-INN lookup contract"),
        "fns_egrul": _policy("legal", "FNS EGRUL source contract"),
        "fns_egrip": _policy("individual_entrepreneur", "FNS EGRIP source contract"),
        "fns_msp": _policy("both", "FNS SME registry parser contract for legal entities and IP"),
        "fns_tax_regime": _policy("both", "FNS SNR/SNRIP atomic family contract"),
        "fns_snr": _policy("legal", "FNS SNR legal-entity member contract"),
        "fns_snrip": _policy("individual_entrepreneur", "FNS SNRIP member contract"),
        "fns_disqualified": _policy("legal", "FNS disqualified-person company-link contract"),
        "fns_headcount": _policy("legal", "FNS legal-entity headcount contract"),
        "fns_tax_paid": _policy("legal", "FNS legal-entity tax-payment contract"),
        "fns_tax_debt": _policy("legal", "FNS Tax Debt source passport"),
        "fns_tax_offence": _policy("legal", "FNS Tax Offence source passport"),
        "fns_revenue_expenses": _policy("legal", "FNS legal-entity revenue/expense contract"),
        "fns_sme_support": _policy("both", "FNS SME support legal/IP parser contract"),
        "fns_npd": _policy("individual_entrepreneur", "FNS NPD source passport"),
        "girbo_reports": _policy("legal", "GIR BO legal-entity reporting contract"),
        "girbo_accounting": _policy("legal", "GIR BO accounting source contract"),
        "fssp_enforcement": _policy("legal", "FSSP company enforcement registry declaration"),
        "fedresurs_messages": _policy("both", "Fedresurs legal-entity/IP registry declaration"),
        "eis_procurements": _policy("legal", "EIS company procurement registry declaration"),
        "eis_rnp": _policy("legal", "EIS RNP source passport"),
        "worker_failure_probe": _policy("both", "Worker Foundation non-subject-specific recovery probe contract"),
        "cbr_warning_list": _policy("both", "CBR warning-list exact 10/12-digit INN parser contract"),
        "cbr_finorg": _policy("legal", "CBR financial-organisation registry declaration"),
        "erknm_inspections": _policy("both", "ERKNM parser contract for ЮЛ and ИП subjects"),
        "firmoteka": _policy("both", "Firmoteka legal-entity/IP catalog contract"),
        "mintrans_ted_registry": _policy("legal", "Mintrans TED registry declaration"),
        "nostroy_sro_members_on_demand": _policy("legal", "NOSTROY company SRO membership contract"),
        "nopriz_sro_members_on_demand": _policy("legal", "NOPRIZ company SRO membership contract"),
        "nopriz_nrs_private_on_demand": _policy("legal", "NOPRIZ private manager-evidence contract"),
        "nostroy_nrs_protected": _policy("legal", "NOSTROY protected manager-evidence contract"),
        "prime_corporate_disclosure": _policy("legal", "Corporate issuer disclosure contract"),
        "rkn_personal_data_operators": _policy("legal", "Roskomnadzor public legal-entity projection contract"),
        "rkn_communications_licenses": _policy("legal", "Roskomnadzor communications license contract"),
        "rkn_broadcast_licenses": _policy("legal", "Roskomnadzor broadcast license contract"),
        "rkn_registered_media": _policy("legal", "Roskomnadzor media founder company contract"),
        "rkn_information_distributors": _policy("legal", "Roskomnadzor information-distributor contract"),
        "rkn_hosting_providers": _policy("legal", "Roskomnadzor hosting-provider company contract"),
        "checko_arbitration_cases": _policy("legal", "Stage 1.5 arbitration company contract"),
        "moscow_general_court_cases": _policy("legal", "Stage 1.5 general-court company contract"),
        "roszdrav_pharma_licenses": _policy("legal", "Roszdrav pharma-license company contract"),
        "roszdrav_narcotics_licenses": _policy("legal", "Roszdrav narcotics-license company contract"),
        "roszdrav_medical_device_maintenance_licenses": _policy("legal", "Roszdrav maintenance-license company contract"),
        "roszdrav_unified_license_search": _policy("legal", "Roszdrav exact-INN license-search contract"),
        "roszdrav_medical_devices": _policy("legal", "Roszdrav medical-device company evidence contract"),
        "roszdrav_clinical_research_orgs": _policy("legal", "Roszdrav clinical-organisation exact-INN contract"),
    }
)


PRODUCTION_DATASET_CODES_2026_09_28 = frozenset(
    {
        "excel_companies", "dadata_company_lookup", "fns_egrul", "fns_egrip",
        "fns_msp", "fns_tax_regime", "fns_snr", "fns_snrip",
        "fns_disqualified", "fns_headcount", "fns_tax_paid", "fns_tax_debt",
        "fns_tax_offence", "fns_revenue_expenses", "fns_sme_support", "fns_npd",
        "girbo_reports", "girbo_accounting", "fssp_enforcement",
        "fedresurs_messages", "eis_procurements", "eis_rnp",
        "worker_failure_probe", "cbr_warning_list", "cbr_finorg",
        "erknm_inspections", "firmoteka", "mintrans_ted_registry",
        "nostroy_sro_members_on_demand", "nopriz_sro_members_on_demand",
        "prime_corporate_disclosure", "rkn_personal_data_operators",
        "rkn_communications_licenses", "rkn_broadcast_licenses",
        "rkn_registered_media", "rkn_information_distributors",
        "rkn_hosting_providers", "checko_arbitration_cases",
        "moscow_general_court_cases", "roszdrav_pharma_licenses",
        "roszdrav_narcotics_licenses",
        "roszdrav_medical_device_maintenance_licenses",
        "roszdrav_unified_license_search", "roszdrav_medical_devices",
        "roszdrav_clinical_research_orgs",
    }
)


def dataset_applicability_policy(code: str) -> DatasetApplicabilityPolicy:
    try:
        return CANONICAL_DATASET_POLICIES[code]
    except KeyError as error:
        raise KeyError(f"dataset applicability policy is unmapped: {code}") from error


def canonical_dataset_applicability(code: str) -> dict[str, list[str]]:
    """Return a fresh JSON-compatible provisioning value for ``code``."""

    return dataset_applicability_policy(code).as_persisted()
