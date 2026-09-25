"""Strict public projection contract shared by exporter, importer and website."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "public-projection-v1"
REQUIRED_SOURCE_CODES = ("REVEXP", "PAYTAX", "DEBTAM", "TAXOFFENCE")
FORBIDDEN_KEY_PARTS = {
    "raw",
    "raw_payload",
    "artifact",
    "artifact_reference",
    "worker_job",
    "worker_run",
    "internal_evidence",
    "internal_metadata",
    "secret",
    "token",
    "api_key",
    "private",
    "cookie",
    "authorization",
    "credentials",
}
PRIVATE_PATH = re.compile(r"(?:^|\s)(?:/Users/|/home/|/private/|file://|[A-Za-z]:\\)")


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicState(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_CHECKED = "NOT_CHECKED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    PARSING_ERROR = "PARSING_ERROR"
    STALE_DATA = "STALE_DATA"
    UNKNOWN = "UNKNOWN"
    PARTIAL = "PARTIAL"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


class Freshness(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


LIMITING_STATE_ORDER = {
    PublicState.CONFLICTING_EVIDENCE: 100,
    PublicState.PARSING_ERROR: 95,
    PublicState.TIMEOUT: 90,
    PublicState.SOURCE_UNAVAILABLE: 85,
    PublicState.STALE_DATA: 80,
    PublicState.PARTIAL: 75,
    PublicState.UNKNOWN: 70,
    PublicState.NOT_CHECKED: 65,
    PublicState.NOT_APPLICABLE: 20,
    PublicState.FOUND: 10,
    PublicState.NOT_FOUND: 10,
}


def strongest_state(*states: PublicState) -> PublicState:
    """Return the most limiting state; limitations always outrank observations."""

    if not states:
        return PublicState.UNKNOWN
    return max(states, key=lambda item: LIMITING_STATE_ORDER[item])


def valid_legal_inn(value: str) -> bool:
    if not re.fullmatch(r"\d{10}", value):
        return False
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    return sum(int(digit) * weight for digit, weight in zip(value[:9], weights)) % 11 % 10 == int(value[9])


def scan_forbidden(value: Any, path: str = "$") -> None:
    """Fail closed on forbidden keys and private filesystem path values."""

    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if any(part in normalized for part in FORBIDDEN_KEY_PARTS):
                raise ValueError(f"forbidden public field at {path}.{key}")
            scan_forbidden(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            scan_forbidden(child, f"{path}[{index}]")
    elif isinstance(value, str) and PRIVATE_PATH.search(value):
        raise ValueError(f"private filesystem path at {path}")


class PublicationInfo(PublicModel):
    schema_version: str = Field(pattern=r"^public-projection-v1$")
    release_id: str = Field(min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9._-]+$")
    published_at: datetime
    result_date: date
    content_updated_at: datetime
    index_eligible: bool

    @field_validator("published_at", "content_updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publication timestamps must include a timezone")
        return value


class CompanyInfo(PublicModel):
    name: str = Field(min_length=1, max_length=500)
    full_name: str | None = Field(default=None, max_length=1200)
    legal_status: str | None = Field(default=None, max_length=240)
    inn: str
    kpp: str | None = Field(default=None, pattern=r"^\d{9}$")
    ogrn: str | None = Field(default=None, pattern=r"^\d{13}$")
    address: str | None = Field(default=None, max_length=2000)
    registration_date: date | None = None
    director_name: str | None = Field(default=None, max_length=600)
    director_position: str | None = Field(default=None, max_length=300)

    @field_validator("inn")
    @classmethod
    def legal_inn_only(cls, value: str) -> str:
        if not valid_legal_inn(value):
            raise ValueError("only a valid 10-digit legal-entity INN is public")
        return value


class PublicRiskFactor(PublicModel):
    title: str = Field(min_length=1, max_length=1000)
    explanation: str | None = Field(default=None, max_length=2000)
    source_name: str | None = Field(default=None, max_length=250)
    source_data_date: date | None = None


class PublicRisk(PublicModel):
    state: PublicState
    title: str = Field(min_length=1, max_length=300)
    explanation: str = Field(min_length=1, max_length=3000)
    factors: tuple[PublicRiskFactor, ...] = ()
    limitations: tuple[str, ...] = ()
    assessment_date: date
    model_version: str | None = Field(default=None, max_length=80)
    ruleset_version: str | None = Field(default=None, max_length=120)


class PublicSummary(PublicModel):
    short_conclusion: str = Field(min_length=1, max_length=2000)
    main_factors: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    generated_at: datetime

    @field_validator("generated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("summary generated_at must include a timezone")
        return value


PublicScalar = str | int | float | bool | None


class PublicSourceBlock(PublicModel):
    code: str
    state: PublicState
    values: dict[str, PublicScalar] = Field(default_factory=dict)
    source_name: str = Field(min_length=1, max_length=250)
    source_data_date: date | None = None
    result_date: date
    freshness: Freshness
    limitation: str | None = Field(default=None, max_length=2000)

    @field_validator("code")
    @classmethod
    def known_source_code(cls, value: str) -> str:
        if value not in REQUIRED_SOURCE_CODES:
            raise ValueError("unsupported public source code")
        return value

    @model_validator(mode="after")
    def enforce_limiting_semantics(self) -> PublicSourceBlock:
        if self.freshness == Freshness.STALE and self.state != PublicState.STALE_DATA:
            raise ValueError("stale evidence must use STALE_DATA")
        if self.state in {
            PublicState.NOT_CHECKED,
            PublicState.SOURCE_UNAVAILABLE,
            PublicState.TIMEOUT,
            PublicState.PARSING_ERROR,
            PublicState.STALE_DATA,
            PublicState.UNKNOWN,
            PublicState.PARTIAL,
            PublicState.CONFLICTING_EVIDENCE,
        } and not self.limitation:
            raise ValueError("limiting source states require an explanation")
        return self


class PublicProjection(PublicModel):
    publication: PublicationInfo
    company: CompanyInfo
    risk: PublicRisk
    summary: PublicSummary
    sources: tuple[PublicSourceBlock, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_projection(self) -> PublicProjection:
        codes = tuple(source.code for source in self.sources)
        if set(codes) != set(REQUIRED_SOURCE_CODES) or len(codes) != len(set(codes)):
            raise ValueError("projection must contain each required source exactly once")
        scan_forbidden(self.model_dump(mode="json"))
        return self


class ManifestEntity(PublicModel):
    inn: str
    entity_type: str = Field(pattern=r"^legal$")
    master_dataset: Literal["fns_egrul"]
    source: Literal["fns"]
    usable_source_coverage: tuple[str, ...] = ()

    @field_validator("inn")
    @classmethod
    def validate_inn(cls, value: str) -> str:
        if not valid_legal_inn(value):
            raise ValueError("manifest accepts valid 10-digit legal-entity INNs only")
        return value


class CanonicalManifest(PublicModel):
    schema_version: Literal["canonical-public-cohort-v1"]
    manifest_version: Literal[2]
    release_name: str = Field(min_length=1, max_length=120)
    created_at: datetime
    source_main_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_database: Literal["nextcompany_operational"]
    production_eligibility: Literal["VERIFIED_OPERATIONAL"]
    selection_policy: dict[str, Any]
    supersedes_release_id: str | None = Field(default=None, max_length=120)
    entities: tuple[ManifestEntity, ...] = Field(min_length=40, max_length=40)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical manifest created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def exactly_40_unique_legal_entities(self) -> CanonicalManifest:
        inns = tuple(entity.inn for entity in self.entities)
        if len(inns) != 40:
            raise ValueError("first public release requires exactly 40 entities")
        if len(set(inns)) != 40:
            raise ValueError("manifest contains duplicate INNs")
        if inns != tuple(sorted(inns)):
            raise ValueError("manifest entities must be sorted by INN")
        if self.selection_policy.get("risk_outcome_used") is not False:
            raise ValueError("canonical cohort selection must not use risk outcome")
        if not self.selection_policy.get("version"):
            raise ValueError("canonical cohort selection policy version is required")
        return self


class ReleaseManifest(PublicModel):
    schema_version: str = Field(pattern=r"^public-projection-v1$")
    release_id: str = Field(min_length=8, max_length=120, pattern=r"^[a-zA-Z0-9._-]+$")
    source_main_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    cohort_manifest_path: str = Field(pattern=r"^docs/releases/[a-zA-Z0-9._-]+\.json$")
    cohort_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cohort_source_main_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    previous_release_id: str | None = Field(default=None, max_length=120)
    created_at: datetime
    result_date: date
    content_updated_at: datetime
    record_count: int = Field(ge=40, le=40)
    companies_file: str = Field(pattern=r"^companies\.jsonl\.gz$")

    @field_validator("created_at", "content_updated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("release timestamps must include a timezone")
        return value
