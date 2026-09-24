"""Executable source contract for S02 — FNS Tax Debt."""

from __future__ import annotations

from dataclasses import asdict, dataclass


SOURCE_ID = "S02"
DATASET_CODE = "fns_tax_debt"
HANDLER_VERSION = "fns-tax-debt-v1"
BASELINE_HANDLER_VERSION = "fns-tax-debt-baseline-v1"
CONTROLLED_LIVE_HANDLER_VERSION = "fns-tax-debt-controlled-live-v1"
PARSER_VERSION = "fns-debtam-xml-v1"
NORMALIZATION_VERSION = "tax-debt-normalization-v1"
FACT_CODE = "tax.debt.amount_as_of_date"
OFFICIAL_SOURCE_PAGE = "https://www.nalog.gov.ru/opendata/7707329152-debtam/"
OFFICIAL_FILE_BASE = "https://file.nalog.ru/opendata/7707329152-debtam/"
PILOT_ENVIRONMENT = "s02-controlled-live-pilot"
PILOT_COHORT_LIMIT = 100
BASELINE_COHORT_LIMIT = 40
CONTROLLED_LIVE_PILOT_ENABLED = False
MASS_INGESTION_ENABLED = False


@dataclass(frozen=True)
class SourceContract:
    source_id: str
    dataset_code: str
    owner: str
    official_source: str
    access_method: str
    source_format: str
    identifiers: tuple[str, ...]
    freshness: str
    provenance_fields: tuple[str, ...]
    fact_code: str
    parser_version: str
    normalization_version: str
    controlled_live_pilot_enabled: bool
    mass_ingestion_enabled: bool

    def as_dict(self) -> dict:
        return asdict(self)


FNS_TAX_DEBT_SOURCE_CONTRACT = SourceContract(
    source_id=SOURCE_ID,
    dataset_code=DATASET_CODE,
    owner="ФНС России",
    official_source=OFFICIAL_SOURCE_PAGE,
    access_method="official bulk snapshot download; ZIP is retained before parsing",
    source_format="ZIP containing XML conforming to the published debtam structure",
    identifiers=("10-digit legal-entity INN",),
    freshness=(
        "source-published dated snapshot; source_as_of and retrieved_at are required; "
        "no real-time balance is claimed"
    ),
    provenance_fields=(
        "source_id",
        "official_source",
        "artifact_sha256",
        "artifact_reference",
        "worker_run_id",
        "source_document_id",
        "source_member",
        "record_hash",
        "source_as_of",
        "retrieved_at",
        "parser_version",
        "normalization_version",
        "matching_method",
    ),
    fact_code=FACT_CODE,
    parser_version=PARSER_VERSION,
    normalization_version=NORMALIZATION_VERSION,
    controlled_live_pilot_enabled=CONTROLLED_LIVE_PILOT_ENABLED,
    mass_ingestion_enabled=MASS_INGESTION_ENABLED,
)
