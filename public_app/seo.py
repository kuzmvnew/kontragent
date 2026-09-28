"""Fail-closed SEO compiler derived from one accepted Public Projection.

The module is deliberately pure: it performs no I/O and never mutates the
public projection.  Routes, sitemap generation and catalog pagination consume
the same compiled result, so a legacy ``publication.index_eligible`` flag can
never grant indexability by itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from public_app.contracts import (
    SCHEMA_VERSION,
    Freshness,
    PublicProjection,
    PublicState,
    valid_legal_inn,
)
from public_app.semantic import PUBLIC_NEXT_INDEX_ENABLED


SEO_COMPILER_VERSION = "seo-eligibility-v1"
SEO_PROJECTION_VERSION = "seo-projection-v1"
PUBLIC_ORIGIN = "https://nextcompany.pro"
COMPANY_SHARD_COUNT = 16


class SeoModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeoDecision(StrEnum):
    INDEX = "INDEX"
    NOINDEX_RECOVERABLE = "NOINDEX_RECOVERABLE"
    NOT_PUBLISHED = "NOT_PUBLISHED"
    GONE = "GONE"


class IdentityEvidenceState(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    DISPUTED = "DISPUTED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"


class SeoReason(StrEnum):
    PUBLIC_NOT_READY = "PUBLIC_NOT_READY"
    RELEASE_NOT_ACTIVE = "RELEASE_NOT_ACTIVE"
    ENTITY_GONE = "ENTITY_GONE"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    UNSUPPORTED_ENTITY_SCOPE = "UNSUPPORTED_ENTITY_SCOPE"
    INVALID_INN = "INVALID_INN"
    IDENTITY_INTEGRITY_FAILED = "IDENTITY_INTEGRITY_FAILED"
    IDENTITY_NOT_CURRENT = "IDENTITY_NOT_CURRENT"
    SEMANTIC_EVIDENCE_MISSING = "SEMANTIC_EVIDENCE_MISSING"
    FORBIDDEN_FIELD = "FORBIDDEN_FIELD"
    THIN_CONTENT = "THIN_CONTENT"
    CANONICAL_INTEGRITY_FAILED = "CANONICAL_INTEGRITY_FAILED"
    EXCLUDED_ENTITY = "EXCLUDED_ENTITY"
    ORPHANED_URL = "ORPHANED_URL"
    NUMERIC_INDEX_PAYLOAD = "NUMERIC_INDEX_PAYLOAD"
    UNKNOWN_INPUT = "UNKNOWN_INPUT"
    COMPILER_ERROR = "COMPILER_ERROR"


class SeoEligibilityContext(SeoModel):
    """Release-owned inputs that do not belong in the ordinary public API."""

    active_revision_id: str = Field(min_length=8, max_length=120)
    public_ready: bool
    released: bool
    legal_entity_scope: bool = True
    identity_integrity: bool = True
    identity_state: IdentityEvidenceState = IdentityEvidenceState.CURRENT
    forbidden_field_scan_passed: bool = True
    canonical_integrity: bool = True
    duplicate_entity: bool = False
    legal_exclusion: bool = False
    test_or_fixture: bool = False
    internal_link_eligible: bool = True
    entity_gone: bool = False
    schema_version: str | None = None
    unknown_inputs: tuple[str, ...] = ()


class SemanticGateResult(SeoModel):
    passed: bool
    core_identity_fact_count: int
    additional_fact_count: int
    accepted_source_count: int
    has_dated_source_fact: bool
    analytics_linked_to_visible_fact: bool
    action_linked_to_visible_fact: bool
    limiting_block_count: int
    semantic_block_count: int
    evidence_refs: tuple[str, ...]
    failures: tuple[str, ...]


class ThinPageResult(SeoModel):
    passed: bool
    non_identity_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_codes: tuple[str, ...] = ()


class SeoEligibilityResult(SeoModel):
    decision: SeoDecision
    reason_codes: tuple[SeoReason, ...]
    evidence_refs: tuple[str, ...]
    compiler_version: Literal["seo-eligibility-v1"] = SEO_COMPILER_VERSION

    @property
    def indexable(self) -> bool:
        return self.decision == SeoDecision.INDEX


class SeoMetadata(SeoModel):
    h1: str
    inn_label: str
    title: str
    description: str
    open_graph: dict[str, str]


class SeoProjection(SeoModel):
    schema_version: Literal["seo-projection-v1"] = SEO_PROJECTION_VERSION
    active_revision_id: str
    inn: str
    canonical_url: str
    robots: str
    metadata: SeoMetadata
    json_ld: dict[str, Any]
    eligibility: SeoEligibilityResult
    sitemap_shard: str = Field(pattern=r"^[0-9a-f]$")
    sitemap_eligible: bool
    catalog_eligible: bool
    non_identity_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_visible_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_updated_at: datetime
    compiler_version: Literal["seo-eligibility-v1"] = SEO_COMPILER_VERSION

    @field_validator("content_updated_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SEO content timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def coherent_membership(self) -> "SeoProjection":
        expected = self.eligibility.decision == SeoDecision.INDEX
        if self.sitemap_eligible != expected or self.catalog_eligible != expected:
            raise ValueError("SEO membership must follow the eligibility decision")
        if expected and self.robots != "index, follow":
            raise ValueError("indexable projection must use index, follow")
        if not expected and self.robots != "noindex, follow":
            raise ValueError("non-indexable projection must use noindex, follow")
        return self


class TemplateCluster(SeoModel):
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    inns: tuple[str, ...] = Field(min_length=50)


class DemandAmbiguity(StrEnum):
    UNAMBIGUOUS = "UNAMBIGUOUS"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"


class DemandEvidenceCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    NOT_RUN = "NOT_RUN"


class ReleaseWave(IntEnum):
    WAVE_500 = 500
    WAVE_2000 = 2000
    WAVE_10000 = 10000


class CatalogPagination(SeoModel):
    page: int = Field(ge=1)
    page_size: int = Field(default=24, ge=1, le=24)
    total_items: int = Field(ge=0)
    total_pages: int = Field(ge=1)
    previous_url: str | None
    next_url: str | None
    page_links: tuple[tuple[int, str], ...]


def catalog_page_url(page: int) -> str:
    if page < 1:
        raise ValueError("catalog page must be positive")
    return "/companies" if page == 1 else f"/companies/page/{page}"


def build_catalog_pagination(page: int, total_items: int, *, page_size: int = 24) -> CatalogPagination:
    if page_size != 24:
        raise ValueError("the public company catalog page size is fixed at 24")
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    if page < 1 or page > total_pages or (total_items == 0 and page > 1):
        raise ValueError("catalog page is out of range")
    candidates = {1, total_pages}
    candidates.update(range(max(1, page - 2), min(total_pages, page + 2) + 1))
    return CatalogPagination(
        page=page,
        total_items=total_items,
        total_pages=total_pages,
        previous_url=catalog_page_url(page - 1) if page > 1 else None,
        next_url=catalog_page_url(page + 1) if page < total_pages else None,
        page_links=tuple((number, catalog_page_url(number)) for number in sorted(candidates)),
    )


class DemandEvidence(SeoModel):
    """Schema boundary for a later live demand task; contains no fake data."""

    query_set_id: str = Field(min_length=1, max_length=160)
    provider_snapshot_id: str | None = Field(default=None, min_length=1, max_length=160)
    monthly_values: tuple[int, ...] | None = Field(default=None, min_length=12, max_length=12)
    median: float | None = Field(default=None, ge=0)
    average: float | None = Field(default=None, ge=0)
    ambiguity: DemandAmbiguity
    completeness: DemandEvidenceCompleteness
    algorithm_version: str = Field(min_length=1, max_length=120)
    wave: ReleaseWave

    @field_validator("monthly_values")
    @classmethod
    def non_negative_months(cls, value: tuple[int, ...] | None) -> tuple[int, ...] | None:
        if value is not None and any(item < 0 for item in value):
            raise ValueError("monthly demand values cannot be negative")
        return value

    @model_validator(mode="after")
    def completeness_matches_evidence(self) -> "DemandEvidence":
        metrics = (self.provider_snapshot_id, self.monthly_values, self.median, self.average)
        if self.completeness == DemandEvidenceCompleteness.COMPLETE and any(item is None for item in metrics):
            raise ValueError("complete demand evidence requires provider snapshot and all metrics")
        if self.completeness == DemandEvidenceCompleteness.NOT_RUN and any(item is not None for item in metrics):
            raise ValueError("NOT_RUN demand evidence must not contain fake metrics")
        return self


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def sitemap_shard(inn: str) -> str:
    """Return the first four bits of SHA-256(INN) as one lowercase hex digit."""

    if not valid_legal_inn(inn):
        raise ValueError("sitemap shard requires a valid legal-entity INN")
    return hashlib.sha256(inn.encode("ascii")).hexdigest()[0]


def _identity_facts(projection: PublicProjection) -> tuple[dict[str, str], dict[str, str]]:
    company = projection.company
    core_candidates = {
        "name": company.name,
        "inn": company.inn,
        "full_name": company.full_name,
        "ogrn": company.ogrn,
        "legal_status": company.legal_status,
    }
    additional_candidates = {
        "kpp": company.kpp,
        "address": company.address,
        "registration_date": company.registration_date.isoformat() if company.registration_date else None,
        "director_name": company.director_name,
        "director_position": company.director_position,
    }
    return (
        {key: str(value) for key, value in core_candidates.items() if value},
        {key: str(value) for key, value in additional_candidates.items() if value},
    )


_LIMITING_STATES = {
    PublicState.NOT_CHECKED,
    PublicState.SOURCE_UNAVAILABLE,
    PublicState.TIMEOUT,
    PublicState.PARSING_ERROR,
    PublicState.STALE_DATA,
    PublicState.UNKNOWN,
    PublicState.PARTIAL,
    PublicState.CONFLICTING_EVIDENCE,
}


def evaluate_semantic_gate(projection: PublicProjection) -> SemanticGateResult:
    core, additional = _identity_facts(projection)
    accepted_sources = tuple(
        source
        for source in projection.sources
        if source.state in {PublicState.FOUND, PublicState.NOT_FOUND}
        and source.freshness == Freshness.CURRENT
        and source.source_name
    )
    dated_sources = tuple(source for source in accepted_sources if source.source_data_date)
    analytics_linked = any(
        factor.meaning_id and factor.source_name and factor.source_data_date
        for factor in projection.risk.factors
    ) or (
        projection.risk.state == PublicState.NOT_FOUND
        and len(accepted_sources) >= 2
        and all(source.negative_closure_proven for source in accepted_sources)
    )
    visible_factor_text = " ".join(
        " ".join(
            filter(
                None,
                (
                    factor.public_headline,
                    factor.public_explanation,
                    factor.client_meaning,
                ),
            )
        ).casefold()
        for factor in projection.risk.factors
    )
    recommendation_text = " ".join(
        f"{item.action} {item.rationale} {item.effect}".casefold()
        for item in projection.summary.recommendations
    )
    linkage_tokens = {
        token
        for marker in projection.summary.main_factors
        for token in re.findall(r"[a-zа-я0-9]+", marker.casefold())
        if len(token) >= 6
    }
    action_linked = any(
        token in visible_factor_text and token in recommendation_text
        for token in linkage_tokens
    )
    limiting_count = sum(source.state in _LIMITING_STATES for source in projection.sources)
    block_count = len(projection.sources) + 2  # Analytics + Action
    failures: list[str] = []
    if len(core) < 4 or not {"name", "inn"}.issubset(core):
        failures.append("CORE_IDENTITY_FACTS_LT_4")
    if len(additional) < 3:
        failures.append("ADDITIONAL_ENTITY_FACTS_LT_3")
    if len(accepted_sources) < 2:
        failures.append("ACCEPTED_PUBLIC_SOURCES_LT_2")
    if not dated_sources:
        failures.append("DATED_SOURCE_FACT_MISSING")
    if not analytics_linked:
        failures.append("ANALYTICS_FACT_LINK_MISSING")
    if not action_linked:
        failures.append("ACTION_FACT_LINK_MISSING")
    if projection.publication.result_date is None:
        failures.append("RESULT_DATE_MISSING")
    if limiting_count * 2 > block_count:
        failures.append("MAJORITY_BLOCKS_UNKNOWN_OR_UNAVAILABLE")
    evidence = tuple(
        sorted(
            [f"company:{key}" for key in (*core.keys(), *additional.keys())]
            + [f"source:{item.code}:{item.source_data_date.isoformat() if item.source_data_date else 'undated'}" for item in accepted_sources]
            + [f"meaning:{item.meaning_id}" for item in projection.risk.factors if item.meaning_id]
        )
    )
    return SemanticGateResult(
        passed=not failures,
        core_identity_fact_count=len(core),
        additional_fact_count=len(additional),
        accepted_source_count=len(accepted_sources),
        has_dated_source_fact=bool(dated_sources),
        analytics_linked_to_visible_fact=analytics_linked,
        action_linked_to_visible_fact=action_linked,
        limiting_block_count=limiting_count,
        semantic_block_count=block_count,
        evidence_refs=evidence,
        failures=tuple(failures),
    )


_INDEX_KEY = re.compile(r"(?:^|_)(?:next_?)?(?:index|score|rating|band)(?:$|_)", re.I)
_INDEX_TEXT = re.compile(r"(?:numeric\s+next\s+index|индекс\s+next|рейтинг\s+next)", re.I)


def contains_numeric_index(value: Any, path: str = "$") -> bool:
    """Detect prohibited numeric reliability/index structures recursively."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            collapsed = re.sub(r"[^a-zа-я0-9]", "", normalized)
            if collapsed in {
                "score",
                "rating",
                "ratingvalue",
                "aggregaterating",
                "band",
                "nextindex",
                "индексnext",
                "рейтингnext",
            }:
                return True
            if _INDEX_KEY.search(normalized) and (
                isinstance(child, (int, float)) and not isinstance(child, bool)
                or normalized in {"band", "rating", "rating_value", "score", "next_index"}
            ):
                return True
            if contains_numeric_index(child, f"{path}.{key}"):
                return True
    elif isinstance(value, (list, tuple)):
        return any(contains_numeric_index(item, f"{path}[]") for item in value)
    elif isinstance(value, str) and _INDEX_TEXT.search(value):
        return True
    return False


def _non_identity_payload(projection: PublicProjection) -> dict[str, Any]:
    payload = projection.public_payload()
    payload.pop("company", None)
    publication = dict(payload.pop("publication", {}))
    publication.pop("published_at", None)
    publication.pop("content_updated_at", None)
    payload["result_date"] = publication.get("result_date")
    return payload


def evaluate_thin_page(projection: PublicProjection, semantic: SemanticGateResult | None = None) -> ThinPageResult:
    semantic = semantic or evaluate_semantic_gate(projection)
    payload = _non_identity_payload(projection)
    digest = _hash(payload)
    reasons: list[str] = []
    if not payload.get("sources"):
        reasons.append("IDENTITY_ONLY_TEMPLATE")
    if semantic.accepted_source_count < 2:
        reasons.append("INSUFFICIENT_SOURCES")
    if not semantic.has_dated_source_fact:
        reasons.append("NO_DATED_FACT")
    if not semantic.analytics_linked_to_visible_fact:
        reasons.append("UNLINKED_ANALYTICS")
    if not semantic.action_linked_to_visible_fact:
        reasons.append("UNLINKED_ACTION")
    if semantic.limiting_block_count * 2 > semantic.semantic_block_count:
        reasons.append("MOST_BLOCKS_UNAVAILABLE")
    source_values = [source.values for source in projection.sources if source.values]
    if not source_values and not projection.risk.factors and not projection.summary.recommendations:
        reasons.append("EMPTY_AFTER_IDENTITY_REMOVAL")
    return ThinPageResult(
        passed=not reasons,
        non_identity_content_hash=digest,
        reason_codes=tuple(reasons),
    )


def detect_template_clusters(
    projections: Iterable[PublicProjection], *, minimum_size: int = 50
) -> tuple[TemplateCluster, ...]:
    """Group deterministic identity-free semantic signatures at the 50+ gate."""

    if minimum_size < 50:
        raise ValueError("SEO template cluster threshold cannot be below 50")
    groups: dict[str, list[str]] = defaultdict(list)
    for projection in projections:
        result = evaluate_thin_page(projection)
        groups[result.non_identity_content_hash].append(projection.company.inn)
    return tuple(
        TemplateCluster(signature=signature, inns=tuple(sorted(inns)))
        for signature, inns in sorted(groups.items())
        if len(inns) >= minimum_size
    )


def compile_eligibility(
    projection: PublicProjection,
    context: SeoEligibilityContext,
) -> tuple[SeoEligibilityResult, SemanticGateResult, ThinPageResult]:
    """Compile typed SEO eligibility.  Every unrecognised state fails closed."""

    reasons: list[SeoReason] = []
    evidence: list[str] = []
    semantic = evaluate_semantic_gate(projection)
    thin = evaluate_thin_page(projection, semantic)
    try:
        schema = context.schema_version or projection.publication.schema_version
        if not context.public_ready:
            reasons.append(SeoReason.PUBLIC_NOT_READY)
        if not context.released or context.active_revision_id != projection.publication.release_id:
            reasons.append(SeoReason.RELEASE_NOT_ACTIVE)
        if context.entity_gone:
            reasons.append(SeoReason.ENTITY_GONE)
        if schema != SCHEMA_VERSION:
            reasons.append(SeoReason.UNSUPPORTED_SCHEMA)
        if not context.legal_entity_scope:
            reasons.append(SeoReason.UNSUPPORTED_ENTITY_SCOPE)
        if not valid_legal_inn(projection.company.inn):
            reasons.append(SeoReason.INVALID_INN)
        if not context.identity_integrity:
            reasons.append(SeoReason.IDENTITY_INTEGRITY_FAILED)
        if context.identity_state != IdentityEvidenceState.CURRENT:
            reasons.append(SeoReason.IDENTITY_NOT_CURRENT)
        if not context.forbidden_field_scan_passed:
            reasons.append(SeoReason.FORBIDDEN_FIELD)
        if not semantic.passed:
            reasons.append(SeoReason.SEMANTIC_EVIDENCE_MISSING)
        if not thin.passed:
            reasons.append(SeoReason.THIN_CONTENT)
        if not context.canonical_integrity:
            reasons.append(SeoReason.CANONICAL_INTEGRITY_FAILED)
        if context.duplicate_entity or context.legal_exclusion or context.test_or_fixture:
            reasons.append(SeoReason.EXCLUDED_ENTITY)
        if not context.internal_link_eligible:
            reasons.append(SeoReason.ORPHANED_URL)
        if PUBLIC_NEXT_INDEX_ENABLED or contains_numeric_index(projection.model_dump(mode="json")):
            reasons.append(SeoReason.NUMERIC_INDEX_PAYLOAD)
        if context.unknown_inputs:
            reasons.append(SeoReason.UNKNOWN_INPUT)
        evidence.extend(semantic.evidence_refs)
        evidence.extend(f"semantic:{item}" for item in semantic.failures)
        evidence.extend(f"thin:{item}" for item in thin.reason_codes)
    except Exception:
        reasons.append(SeoReason.COMPILER_ERROR)

    reasons = list(dict.fromkeys(reasons))
    if not reasons:
        decision = SeoDecision.INDEX
    elif SeoReason.ENTITY_GONE in reasons:
        decision = SeoDecision.GONE
    elif any(
        reason in reasons
        for reason in (
            SeoReason.PUBLIC_NOT_READY,
            SeoReason.RELEASE_NOT_ACTIVE,
            SeoReason.EXCLUDED_ENTITY,
            SeoReason.UNSUPPORTED_ENTITY_SCOPE,
        )
    ):
        decision = SeoDecision.NOT_PUBLISHED
    else:
        decision = SeoDecision.NOINDEX_RECOVERABLE
    return (
        SeoEligibilityResult(
            decision=decision,
            reason_codes=tuple(reasons),
            evidence_refs=tuple(sorted(set(evidence))),
        ),
        semantic,
        thin,
    )


def build_metadata(projection: PublicProjection) -> SeoMetadata:
    """Build allowlisted neutral metadata without adverse or internal fields."""

    company = projection.company
    display_name = company.name.strip() or (company.full_name or "").strip()
    if not display_name:
        raise ValueError("company name is required for metadata")
    if company.name.strip():
        title = f"{display_name} — ИНН {company.inn}: сведения о компании | NEXT"
    else:
        title = f"{display_name} — ИНН {company.inn} | NEXT"
    fragments = [f"{display_name}. ИНН {company.inn}."]
    if company.full_name and company.full_name.strip() != display_name:
        fragments.append(f"Полное наименование: {company.full_name.strip()}.")
    if company.registration_date:
        fragments.append(f"Дата регистрации: {company.registration_date.strftime('%d.%m.%Y')}.")
    if company.address:
        fragments.append(f"Юридический адрес: {company.address.strip()}.")
    description = " ".join(fragments)[:500]
    canonical = f"{PUBLIC_ORIGIN}/companies/{company.inn}"
    return SeoMetadata(
        h1=display_name,
        inn_label=f"ИНН {company.inn}",
        title=title,
        description=description,
        open_graph={
            "type": "website",
            "title": title,
            "description": description,
            "url": canonical,
            "site_name": "NEXT Company",
        },
    )


def build_json_ld(projection: PublicProjection, metadata: SeoMetadata | None = None) -> dict[str, Any]:
    """Build an SSR @graph using only identity values rendered on the page."""

    metadata = metadata or build_metadata(projection)
    company = projection.company
    canonical = f"{PUBLIC_ORIGIN}/companies/{company.inn}"
    organization: dict[str, Any] = {
        "@type": "Organization",
        "@id": f"{canonical}#organization",
        "name": company.name,
        "identifier": company.inn,
        "url": canonical,
    }
    if company.full_name and company.full_name != company.name:
        organization["legalName"] = company.full_name
    if company.address:
        organization["address"] = company.address
    if company.registration_date:
        organization["foundingDate"] = company.registration_date.isoformat()
    graph = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebPage",
                "@id": canonical,
                "url": canonical,
                "name": metadata.title,
                "description": metadata.description,
                "mainEntity": {"@id": f"{canonical}#organization"},
            },
            organization,
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Главная", "item": f"{PUBLIC_ORIGIN}/"},
                    {"@type": "ListItem", "position": 2, "name": "Компании", "item": f"{PUBLIC_ORIGIN}/companies"},
                    {"@type": "ListItem", "position": 3, "name": company.name, "item": canonical},
                ],
            },
        ],
    }
    if contains_numeric_index(graph):
        raise ValueError("numeric index is forbidden in JSON-LD")
    return graph


def _search_visible_payload(
    projection: PublicProjection,
    metadata: SeoMetadata,
    json_ld: dict[str, Any],
) -> dict[str, Any]:
    company = projection.company
    visible = {
        "company": company.model_dump(mode="json"),
        "result_date": projection.publication.result_date.isoformat(),
        "assessment": {
            "conclusion": projection.public_conclusion,
            "title": projection.risk.public_title,
            "status": projection.risk.public_status,
            "explanation": projection.risk.public_explanation,
            "assessment_date": projection.risk.assessment_date.isoformat(),
            "factors": [
                {
                    "headline": factor.public_headline,
                    "explanation": factor.public_explanation,
                    "client_meaning": factor.client_meaning,
                    "what_it_does_not_mean": factor.what_it_does_not_mean,
                    "source_name": factor.source_name,
                    "source_data_date": factor.source_data_date.isoformat() if factor.source_data_date else None,
                }
                for factor in projection.risk.factors
            ],
        },
        "limitations": [item.model_dump(mode="json") for item in projection.public_limitations],
        "recommendations": [item.model_dump(mode="json") for item in projection.summary.recommendations],
        "sources": [
            {
                "name": source.public_name,
                "status": source.public_status,
                "explanation": source.public_explanation,
                "values": source.public_values,
                "source_data_date": source.source_data_date.isoformat() if source.source_data_date else None,
                "result_date": source.result_date.isoformat(),
            }
            for source in projection.sources
        ],
    }
    return {
        "canonical": f"{PUBLIC_ORIGIN}/companies/{projection.company.inn}",
        "metadata": metadata.model_dump(mode="json"),
        "json_ld": json_ld,
        "visible": visible,
    }


def compile_seo_projection(
    projection: PublicProjection,
    *,
    context: SeoEligibilityContext | None = None,
    previous: SeoProjection | None = None,
) -> SeoProjection:
    """Derive all SEO output from one projection/revision without data mixing."""

    context = context or SeoEligibilityContext(
        active_revision_id=projection.publication.release_id,
        public_ready=True,
        released=True,
    )
    eligibility, _semantic, thin = compile_eligibility(projection, context)
    metadata = build_metadata(projection)
    json_ld = build_json_ld(projection, metadata)
    search_hash = _hash(_search_visible_payload(projection, metadata, json_ld))
    content_updated_at = projection.publication.content_updated_at
    if (
        previous is not None
        and previous.inn == projection.company.inn
        and previous.search_visible_hash == search_hash
    ):
        content_updated_at = previous.content_updated_at
    indexable = eligibility.decision == SeoDecision.INDEX
    return SeoProjection(
        active_revision_id=context.active_revision_id,
        inn=projection.company.inn,
        canonical_url=f"{PUBLIC_ORIGIN}/companies/{projection.company.inn}",
        robots="index, follow" if indexable else "noindex, follow",
        metadata=metadata,
        json_ld=json_ld,
        eligibility=eligibility,
        sitemap_shard=sitemap_shard(projection.company.inn),
        sitemap_eligible=indexable,
        catalog_eligible=indexable,
        non_identity_content_hash=thin.non_identity_content_hash,
        search_visible_hash=search_hash,
        content_updated_at=content_updated_at,
    )


def public_seo_payload(seo: SeoProjection) -> dict[str, Any]:
    """Return the route-safe subset; internal reasons/evidence never cross the boundary."""

    return {
        "canonical_url": seo.canonical_url,
        "robots": seo.robots,
        "metadata": seo.metadata.model_dump(mode="json"),
        "json_ld": seo.json_ld,
        "content_updated_at": seo.content_updated_at.isoformat(),
    }
