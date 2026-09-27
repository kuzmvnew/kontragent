"""Canonical validation of persisted SEO projections.

This boundary is intentionally shared by company pages, sitemaps and the
catalog.  SQL may reduce the candidate set, but only this validator may turn a
stored row into discovery membership.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from public_app.contracts import PublicProjection
from public_app.seo import (
    PUBLIC_ORIGIN,
    SEO_COMPILER_VERSION,
    SeoDecision,
    SeoEligibilityContext,
    SeoProjection,
    compile_seo_projection,
    sitemap_shard,
)


class StoredSeoProjectionState(StrEnum):
    VALID_INDEX = "VALID_INDEX"
    VALID_NOINDEX = "VALID_NOINDEX"
    INVALID = "INVALID"


@dataclass(frozen=True)
class StoredSeoProjectionValidation:
    state: StoredSeoProjectionState
    projection: SeoProjection | None

    @property
    def valid(self) -> bool:
        return self.state != StoredSeoProjectionState.INVALID


_INVALID = StoredSeoProjectionValidation(
    state=StoredSeoProjectionState.INVALID,
    projection=None,
)
_AUTHORIZED_COHORTS = {500, 2000, 10000}


def _strict_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _cohort(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return None
    return candidate if candidate in _AUTHORIZED_COHORTS else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        return None
    return candidate


def validate_stored_seo_projection(
    *,
    public_projection: PublicProjection,
    release_id: str,
    inn: str,
    seo_projection: Any,
    seo_decision: Any,
    seo_compiler_version: Any,
    search_visible_hash: Any,
    non_identity_content_hash: Any,
    sitemap_shard_value: Any,
    seo_content_updated_at: Any,
    release_status: Any,
    seo_contract_version: Any,
    seo_release_cohort: Any,
    seo_released: Any,
) -> StoredSeoProjectionValidation:
    """Validate every persisted SEO boundary and return one typed decision.

    ``VALID_INDEX`` is impossible without explicit release authorization.
    Fully valid canonical noindex projections remain renderable as
    ``VALID_NOINDEX``.  Anything missing, malformed, contradictory or stale is
    ``INVALID`` and therefore cannot enter discovery surfaces.
    """

    released = _strict_bool(seo_released)
    cohort = _cohort(seo_release_cohort)
    if released is None or (seo_release_cohort is not None and cohort is None):
        return _INVALID
    if released and cohort is None:
        return _INVALID
    if (
        public_projection.publication.release_id != release_id
        or public_projection.company.inn != inn
        or release_status != "active"
        or seo_contract_version != SEO_COMPILER_VERSION
        or seo_compiler_version != SEO_COMPILER_VERSION
    ):
        return _INVALID

    try:
        candidate = SeoProjection.model_validate(seo_projection)
        expected = compile_seo_projection(
            public_projection,
            context=SeoEligibilityContext(
                active_revision_id=release_id,
                public_ready=True,
                released=True,
            ),
        )
        content_updated_at = _timestamp(seo_content_updated_at)
        canonical_url = f"{PUBLIC_ORIGIN}/companies/{inn}"
        mirrors_match = (
            candidate.active_revision_id == release_id
            and candidate.inn == inn
            and candidate.canonical_url == canonical_url
            and candidate.eligibility.decision.value == seo_decision
            and candidate.compiler_version == seo_compiler_version
            and candidate.search_visible_hash == search_visible_hash
            and candidate.non_identity_content_hash == non_identity_content_hash
            and candidate.sitemap_shard == sitemap_shard_value
            and candidate.sitemap_shard == sitemap_shard(inn)
            and content_updated_at is not None
            and candidate.content_updated_at == content_updated_at
        )
        deterministic_content_matches = (
            candidate.metadata == expected.metadata
            and candidate.json_ld == expected.json_ld
            and candidate.search_visible_hash == expected.search_visible_hash
            and candidate.non_identity_content_hash == expected.non_identity_content_hash
        )
    except Exception:
        # Persistence is an untrusted boundary. Any unknown validation failure
        # is fail-closed rather than a page/discovery exception.
        return _INVALID
    if not mirrors_match or not deterministic_content_matches:
        return _INVALID

    if candidate.eligibility.decision == SeoDecision.INDEX:
        if not released or cohort is None:
            return _INVALID
        return StoredSeoProjectionValidation(
            state=StoredSeoProjectionState.VALID_INDEX,
            projection=candidate,
        )
    return StoredSeoProjectionValidation(
        state=StoredSeoProjectionState.VALID_NOINDEX,
        projection=candidate,
    )
