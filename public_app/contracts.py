"""Strict public projection contract shared by exporter, importer and website."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from public_app.semantic import (
    PUBLIC_NEXT_INDEX_ENABLED,
    CompiledLimitation,
    CompiledRecommendation,
    compile_aggregate_abstention_conclusion,
    compile_aggregate_clean_conclusion,
    compile_limitation,
    compile_recommendation,
    compile_source_status,
    validate_public_text,
)

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
NUMERIC_INDEX_KEY = re.compile(
    r"(?:next[ _-]*index|индекс[ _-]*next|score|rating(?:value)?|aggregate[ _-]*rating|band|рейтинг)",
    re.IGNORECASE,
)


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


class PublicLimitation(PublicModel):
    headline: str = Field(min_length=1, max_length=500)
    short_explanation: str = Field(min_length=1, max_length=1600)
    effect_on_conclusion: str = Field(min_length=1, max_length=1600)
    what_remains_unknown: str = Field(min_length=1, max_length=1600)

    @classmethod
    def from_compiled(cls, value: CompiledLimitation) -> "PublicLimitation":
        return cls(**value.model_dump())


class PublicRecommendation(PublicModel):
    action: str = Field(min_length=1, max_length=1600)
    rationale: str = Field(min_length=1, max_length=1600)
    effect: str = Field(min_length=1, max_length=1600)

    @classmethod
    def from_compiled(cls, value: CompiledRecommendation) -> "PublicRecommendation":
        return cls(**value.model_dump())


class PublicRiskFactor(PublicModel):
    meaning_id: str | None = Field(default=None, max_length=200)
    category: str = Field(default="Фактор проверки", min_length=1, max_length=240)
    severity: str = Field(default="Требует внимания", min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=1000)
    explanation: str | None = Field(default=None, max_length=2000)
    full_explanation: str | None = Field(default=None, max_length=3000)
    client_meaning: str | None = Field(default=None, max_length=2000)
    what_it_does_not_mean: str | None = Field(default=None, max_length=1600)
    recommendation_effect: str | None = Field(default=None, max_length=1600)
    current_state: str | None = Field(default=None, max_length=240)
    previous_state: str | None = Field(default=None, max_length=240)
    change: str | None = Field(default=None, max_length=500)
    trend: str | None = Field(default=None, max_length=160)
    frequency: str | None = Field(default=None, max_length=160)
    recency: str | None = Field(default=None, max_length=160)
    duration: str | None = Field(default=None, max_length=160)
    materiality: str | None = Field(default=None, max_length=500)
    counter_evidence: tuple[str, ...] = ()
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_name: str | None = Field(default=None, max_length=250)
    source_data_date: date | None = None

    @property
    def public_headline(self) -> str:
        return self.title if self.meaning_id else "Подтверждённый фактор требует внимания."

    @property
    def public_explanation(self) -> str:
        if self.meaning_id and self.explanation:
            return self.explanation
        return "Фактор показан без расширенной интерпретации; изучите подтверждённые сведения и дату источника."


class PublicRisk(PublicModel):
    state: PublicState
    title: str = Field(min_length=1, max_length=300)
    explanation: str = Field(min_length=1, max_length=3000)
    factors: tuple[PublicRiskFactor, ...] = ()
    limitations: tuple[PublicLimitation, ...] = ()
    assessment_date: date
    model_version: str | None = Field(default=None, max_length=80)
    ruleset_version: str | None = Field(default=None, max_length=120)

    @field_validator("limitations", mode="before")
    @classmethod
    def compile_legacy_limitations(cls, value):
        compiled = []
        for item in value or ():
            if isinstance(item, PublicLimitation):
                compiled.append(item)
            elif isinstance(item, dict) and {
                "headline", "short_explanation", "effect_on_conclusion", "what_remains_unknown"
            } <= set(item):
                compiled.append(item)
            else:
                code = item.get("limitation_code") if isinstance(item, dict) else item
                compiled.append(compile_limitation(str(code or "")).model_dump())
        return tuple(compiled)

    @property
    def public_status(self) -> str:
        return compile_source_status(
            self.state.value,
            source_date=self.assessment_date,
            negative_closure_proven=self.state == PublicState.NOT_FOUND,
        ).label

    @property
    def public_visual_state(self) -> str:
        return compile_source_status(
            self.state.value,
            source_date=self.assessment_date,
            negative_closure_proven=self.state == PublicState.NOT_FOUND,
        ).visual_state

    @property
    def public_title(self) -> str:
        if self.limitations:
            return "Оценка содержит ограничения"
        if self.factors:
            return "Выявлены факторы, требующие внимания"
        if self.state == PublicState.NOT_FOUND:
            return "Неблагоприятные факторы не выявлены в завершённых проверках"
        return "Оценка содержит ограничения"

    @property
    def public_explanation(self) -> str:
        if self.limitations:
            return "Часть проверок не даёт подтверждённого результата; ограничения перечислены отдельно."
        return "Вывод сформирован только по сохранённым подтверждённым фактам и завершённым проверкам."


class PublicSummary(PublicModel):
    short_conclusion: str = Field(min_length=1, max_length=2000)
    main_factors: tuple[str, ...] = ()
    limitations: tuple[PublicLimitation, ...] = ()
    recommendations: tuple[PublicRecommendation, ...] = ()
    generated_at: datetime

    @field_validator("generated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("summary generated_at must include a timezone")
        return value

    @field_validator("limitations", mode="before")
    @classmethod
    def compile_legacy_limitations(cls, value):
        compiled = []
        for item in value or ():
            if isinstance(item, PublicLimitation):
                compiled.append(item)
            elif isinstance(item, dict) and {
                "headline", "short_explanation", "effect_on_conclusion", "what_remains_unknown"
            } <= set(item):
                compiled.append(item)
            else:
                code = item.get("limitation_code") if isinstance(item, dict) else item
                compiled.append(compile_limitation(str(code or "")).model_dump())
        return tuple(compiled)

    @field_validator("recommendations", mode="before")
    @classmethod
    def compile_legacy_recommendations(cls, value):
        compiled = []
        for item in value or ():
            if isinstance(item, PublicRecommendation):
                compiled.append(item)
                continue
            if isinstance(item, dict) and {"action", "rationale", "effect"} <= set(item):
                compiled.append(item)
                continue
            code = item.get("recommendation_code") if isinstance(item, dict) else item
            result = compile_recommendation(str(code or ""))
            if result is not None:
                compiled.append(result.model_dump())
        return tuple(compiled)


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
    negative_closure_proven: bool = False

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
        if self.state == PublicState.NOT_FOUND and not self.negative_closure_proven:
            raise ValueError("NOT_FOUND requires proven negative closure")
        return self

    @property
    def public_name(self) -> str:
        return {
            "REVEXP": "Доходы и расходы по данным ФНС",
            "PAYTAX": "Уплаченные налоги и сборы по данным ФНС",
            "DEBTAM": "Налоговая задолженность по данным ФНС",
            "TAXOFFENCE": "Налоговые правонарушения по данным ФНС",
        }[self.code]

    @property
    def public_status(self) -> str:
        return compile_source_status(
            self.state.value,
            source_date=self.source_data_date,
            negative_closure_proven=self.negative_closure_proven,
        ).label

    @property
    def public_explanation(self) -> str:
        return compile_source_status(
            self.state.value,
            source_date=self.source_data_date,
            negative_closure_proven=self.negative_closure_proven,
        ).explanation

    @property
    def public_visual_state(self) -> str:
        return compile_source_status(
            self.state.value,
            source_date=self.source_data_date,
            negative_closure_proven=self.negative_closure_proven,
        ).visual_state

    @property
    def public_values(self) -> dict[str, PublicScalar]:
        """Remove forbidden numeric reliability fields at the public boundary."""

        return {key: value for key, value in self.values.items() if not NUMERIC_INDEX_KEY.search(key)}


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

    @property
    def public_conclusion(self) -> str:
        if self.risk.factors:
            return "Выявлены подтверждённые факторы, требующие внимания."
        if self.risk.state == PublicState.NOT_FOUND and not self.risk.limitations:
            return compile_aggregate_clean_conclusion()
        return compile_aggregate_abstention_conclusion()

    @property
    def public_limitations(self) -> tuple[PublicLimitation, ...]:
        return tuple(dict.fromkeys((*self.risk.limitations, *self.summary.limitations)))

    def public_payload(self) -> dict[str, Any]:
        """Return the ordinary-user API shape with no operational identifiers."""

        factors = [
            {
                "meaning_id": item.meaning_id,
                "category": item.category,
                "severity": item.severity,
                "headline": item.public_headline,
                "short_explanation": item.public_explanation,
                "full_explanation": item.full_explanation or item.public_explanation,
                "client_meaning": item.client_meaning,
                "what_it_does_not_mean": item.what_it_does_not_mean,
                "recommendation_effect": item.recommendation_effect,
                "current_state": item.current_state,
                "previous_state": item.previous_state,
                "change": item.change,
                "trend": item.trend,
                "frequency": item.frequency,
                "recency": item.recency,
                "duration": item.duration,
                "materiality": item.materiality,
                "counter_evidence": list(item.counter_evidence),
                "confidence": item.confidence,
                "source": item.source_name,
                "source_data_date": item.source_data_date.isoformat() if item.source_data_date else None,
            }
            for item in self.risk.factors
        ]
        limitations = [item.model_dump(mode="json") for item in self.public_limitations]
        recommendations = [item.model_dump(mode="json") for item in self.summary.recommendations]
        payload = {
            "publication": {
                "published_at": self.publication.published_at.isoformat(),
                "result_date": self.publication.result_date.isoformat(),
                "content_updated_at": self.publication.content_updated_at.isoformat(),
            },
            "company": self.company.model_dump(mode="json"),
            "assessment": {
                "status": self.risk.public_status,
                "title": self.risk.public_title,
                "explanation": self.risk.public_explanation,
                "assessment_date": self.risk.assessment_date.isoformat(),
                "critical_factors": [item for item in factors if item["severity"] == "Критический фактор"],
                "attention_factors": [item for item in factors if item["severity"] != "Критический фактор"],
                "positive_factors": [],
                "limitations": limitations,
            },
            "summary": {
                "short_conclusion": self.public_conclusion,
                "recommendations": recommendations,
                "limitations": limitations,
            },
            "sources": [
                {
                    "name": item.public_name,
                    "status": item.public_status,
                    "explanation": item.public_explanation,
                    "values": item.public_values,
                    "source_data_date": item.source_data_date.isoformat() if item.source_data_date else None,
                    "result_date": item.result_date.isoformat(),
                }
                for item in self.sources
            ],
        }
        validate_public_text(payload)
        return payload


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
    entities: tuple[ManifestEntity, ...] = Field(min_length=1, max_length=10_000)

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical manifest created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def unique_sorted_legal_entities(self) -> CanonicalManifest:
        inns = tuple(entity.inn for entity in self.entities)
        if len(set(inns)) != len(inns):
            raise ValueError("manifest contains duplicate INNs")
        if inns != tuple(sorted(inns)):
            raise ValueError("manifest entities must be sorted by INN")
        if self.selection_policy.get("risk_outcome_used") is not False:
            raise ValueError("canonical cohort selection must not use risk outcome")
        if not self.selection_policy.get("version"):
            raise ValueError("canonical cohort selection policy version is required")
        return self


class ChangedCompanySummary(PublicModel):
    inn: str
    previous_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    current_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("inn")
    @classmethod
    def legal_inn_only(cls, value: str) -> str:
        if not valid_legal_inn(value):
            raise ValueError("changed company must have a valid legal-entity INN")
        return value


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
    record_count: int = Field(ge=1, le=10_000)
    companies_file: str = Field(pattern=r"^companies\.jsonl\.gz$")
    changed_company_count: int = Field(default=0, ge=0, le=10_000)
    changed_companies: tuple[ChangedCompanySummary, ...] = ()

    @field_validator("created_at", "content_updated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("release timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def changed_summary_matches_count(self) -> ReleaseManifest:
        if self.changed_company_count != len(self.changed_companies):
            raise ValueError("changed company summary count mismatch")
        if len({item.inn for item in self.changed_companies}) != len(self.changed_companies):
            raise ValueError("changed company summary contains duplicate INNs")
        return self
