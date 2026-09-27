from __future__ import annotations

import json
from datetime import timedelta

import pytest

from public_app.contracts import Freshness, PublicProjection, PublicState, valid_legal_inn
from public_app.seo import (
    DemandAmbiguity,
    DemandEvidence,
    DemandEvidenceCompleteness,
    IdentityEvidenceState,
    SeoDecision,
    SeoEligibilityContext,
    SeoReason,
    ReleaseWave,
    build_catalog_pagination,
    build_json_ld,
    build_metadata,
    compile_seo_projection,
    contains_numeric_index,
    detect_template_clusters,
    evaluate_semantic_gate,
    evaluate_thin_page,
    public_seo_payload,
    sitemap_shard,
)
from tests.public_test_support import projection


def context(item: PublicProjection, **changes) -> SeoEligibilityContext:
    values = {
        "active_revision_id": item.publication.release_id,
        "public_ready": True,
        "released": True,
    }
    values.update(changes)
    return SeoEligibilityContext(**values)


def test_default_projection_passes_independent_compiler_not_legacy_flag():
    item = projection(index_eligible=False)
    seo = compile_seo_projection(item, context=context(item))
    assert seo.eligibility.decision == SeoDecision.INDEX
    assert seo.sitemap_eligible is True
    assert seo.catalog_eligible is True


def test_public_ready_alone_fails_closed_without_semantic_evidence():
    item = projection()
    value = item.model_dump(mode="json")
    value["summary"]["recommendations"] = []
    item = PublicProjection.model_validate(value)
    seo = compile_seo_projection(item, context=context(item, public_ready=True))
    assert seo.eligibility.decision == SeoDecision.NOINDEX_RECOVERABLE
    assert SeoReason.SEMANTIC_EVIDENCE_MISSING in seo.eligibility.reason_codes
    assert SeoReason.THIN_CONTENT in seo.eligibility.reason_codes


def test_unlinked_generated_action_text_cannot_satisfy_semantic_gate():
    item = projection()
    value = item.model_dump(mode="json")
    value["summary"]["main_factors"] = ["Несвязанный шаблонный текст"]
    item = PublicProjection.model_validate(value)
    semantic = evaluate_semantic_gate(item)
    assert semantic.action_linked_to_visible_fact is False
    assert "ACTION_FACT_LINK_MISSING" in semantic.failures


def test_unknown_schema_and_unknown_input_fail_closed():
    item = projection()
    seo = compile_seo_projection(
        item,
        context=context(item, schema_version="future-unknown-v99", unknown_inputs=("future_state",)),
    )
    assert seo.eligibility.decision == SeoDecision.NOINDEX_RECOVERABLE
    assert SeoReason.UNSUPPORTED_SCHEMA in seo.eligibility.reason_codes
    assert SeoReason.UNKNOWN_INPUT in seo.eligibility.reason_codes


@pytest.mark.parametrize(
    "state",
    [
        IdentityEvidenceState.STALE,
        IdentityEvidenceState.DISPUTED,
        IdentityEvidenceState.SOURCE_UNAVAILABLE,
        IdentityEvidenceState.UNKNOWN,
    ],
)
def test_non_current_core_identity_is_noindex_recoverable(state):
    item = projection()
    seo = compile_seo_projection(item, context=context(item, identity_state=state))
    assert seo.eligibility.decision == SeoDecision.NOINDEX_RECOVERABLE
    assert seo.sitemap_eligible is False
    assert seo.catalog_eligible is False


def test_unreleased_and_non_legal_scope_are_not_published():
    item = projection()
    for changes in ({"released": False}, {"legal_entity_scope": False}):
        seo = compile_seo_projection(item, context=context(item, **changes))
        assert seo.eligibility.decision == SeoDecision.NOT_PUBLISHED
    assert valid_legal_inn("500100732259") is False


def test_numeric_next_index_is_recursively_banned():
    item = projection()
    value = item.model_dump(mode="json")
    value["sources"][0]["values"]["next_index"] = 81
    item = PublicProjection.model_validate(value)
    seo = compile_seo_projection(item, context=context(item))
    assert contains_numeric_index(item.model_dump(mode="json")) is True
    assert SeoReason.NUMERIC_INDEX_PAYLOAD in seo.eligibility.reason_codes
    assert seo.eligibility.decision == SeoDecision.NOINDEX_RECOVERABLE
    assert "next_index" not in json.dumps(item.public_payload())
    assert contains_numeric_index({"ratingValue": 4.8}) is True
    assert contains_numeric_index({"aggregateRating": {"value": 4.8}}) is True


def test_semantic_and_thin_gates_are_deterministic():
    item = projection()
    semantic = evaluate_semantic_gate(item)
    thin = evaluate_thin_page(item, semantic)
    assert semantic.passed is True
    assert semantic.core_identity_fact_count >= 4
    assert semantic.additional_fact_count >= 3
    assert semantic.accepted_source_count >= 2
    assert semantic.analytics_linked_to_visible_fact is True
    assert semantic.action_linked_to_visible_fact is True
    assert thin.passed is True
    assert len(thin.non_identity_content_hash) == 64


def test_one_stale_non_core_module_can_remain_visible_but_metadata_stays_neutral():
    item = projection()
    value = item.model_dump(mode="json")
    value["sources"][0].update(
        state="STALE_DATA",
        freshness="STALE",
        limitation="Срок актуальности данных истёк.",
    )
    item = PublicProjection.model_validate(value)
    seo = compile_seo_projection(item, context=context(item))
    assert seo.eligibility.decision == SeoDecision.INDEX
    serialized = json.dumps(seo.metadata.model_dump(), ensure_ascii=False).casefold()
    for forbidden in ("задолж", "правонаруш", "risk", "partial", "score", "rating"):
        assert forbidden not in serialized


def test_metadata_jsonld_are_allowlisted_and_match_visible_identity():
    item = projection()
    metadata = build_metadata(item)
    graph = build_json_ld(item, metadata)
    assert metadata.h1 == item.company.name
    assert metadata.inn_label == f"ИНН {item.company.inn}"
    assert metadata.title == f"{item.company.name} — ИНН {item.company.inn}: сведения о компании | NEXT"
    assert graph["@context"] == "https://schema.org"
    assert {node["@type"] for node in graph["@graph"]} == {"WebPage", "Organization", "BreadcrumbList"}
    serialized = json.dumps(graph, ensure_ascii=False)
    for forbidden in ("Review", "AggregateRating", "ratingValue", "ClaimReview", "score"):
        assert forbidden not in serialized


def test_search_visible_hash_ignores_technical_republish_but_changes_with_visible_fact():
    first = projection(release_id="public-v1-first")
    first_seo = compile_seo_projection(first, context=context(first))
    publication = first.publication.model_copy(
        update={
            "release_id": "public-v1-second",
            "published_at": first.publication.published_at + timedelta(days=1),
            "content_updated_at": first.publication.content_updated_at + timedelta(days=1),
        }
    )
    republished = first.model_copy(update={"publication": publication})
    same = compile_seo_projection(republished, context=context(republished), previous=first_seo)
    assert same.search_visible_hash == first_seo.search_visible_hash
    assert same.content_updated_at == first_seo.content_updated_at

    hidden_factor = republished.risk.factors[0].model_copy(update={"confidence": 0.5})
    hidden_risk = republished.risk.model_copy(update={"factors": (hidden_factor,)})
    hidden_only = republished.model_copy(update={"risk": hidden_risk})
    hidden_seo = compile_seo_projection(hidden_only, context=context(hidden_only), previous=first_seo)
    assert hidden_seo.search_visible_hash == first_seo.search_visible_hash
    assert hidden_seo.content_updated_at == first_seo.content_updated_at

    value = republished.model_dump(mode="json")
    value["sources"][0]["values"]["Показатель, ₽"] = "200.00"
    changed = PublicProjection.model_validate(value)
    changed_seo = compile_seo_projection(changed, context=context(changed), previous=first_seo)
    assert changed_seo.search_visible_hash != first_seo.search_visible_hash
    assert changed_seo.content_updated_at == changed.publication.content_updated_at


def test_internal_reasons_are_not_in_public_seo_payload():
    item = projection()
    seo = compile_seo_projection(item, context=context(item, identity_state=IdentityEvidenceState.DISPUTED))
    payload = public_seo_payload(seo)
    text = json.dumps(payload)
    assert "reason_codes" not in text
    assert "evidence_refs" not in text
    assert "IDENTITY_NOT_CURRENT" not in text


def test_sitemap_shard_is_first_sha256_nibble_and_stable():
    item = projection()
    observed = sitemap_shard(item.company.inn)
    assert observed == __import__("hashlib").sha256(item.company.inn.encode("ascii")).hexdigest()[0]
    assert observed == sitemap_shard(item.company.inn)
    shards = {sitemap_shard(projection(sequence=100_000_000 + offset).company.inn) for offset in range(2000)}
    assert shards == set("0123456789abcdef")


def test_catalog_pagination_has_first_nearby_previous_and_next_links():
    pagination = build_catalog_pagination(3, 121)
    assert pagination.page_size == 24
    assert pagination.total_pages == 6
    assert pagination.previous_url == "/companies/page/2"
    assert pagination.next_url == "/companies/page/4"
    assert (1, "/companies") in pagination.page_links
    assert (6, "/companies/page/6") in pagination.page_links
    with pytest.raises(ValueError):
        build_catalog_pagination(7, 121)


def test_template_cluster_detection_flags_50_identical_non_identity_pages():
    items = [projection(sequence=200_000_000 + index) for index in range(50)]
    clusters = detect_template_clusters(items)
    assert len(clusters) == 1
    assert len(clusters[0].inns) == 50


def test_not_run_demand_boundary_cannot_carry_fake_metrics():
    value = DemandEvidence(
        query_set_id="query-set-v1",
        ambiguity=DemandAmbiguity.UNRESOLVED,
        completeness=DemandEvidenceCompleteness.NOT_RUN,
        algorithm_version="demand-boundary-v1",
        wave=ReleaseWave.WAVE_500,
    )
    assert value.monthly_values is None
    with pytest.raises(ValueError, match="must not contain fake metrics"):
        DemandEvidence(
            query_set_id="query-set-v1",
            provider_snapshot_id="fixture-is-not-live",
            monthly_values=(0,) * 12,
            median=0,
            average=0,
            ambiguity=DemandAmbiguity.UNRESOLVED,
            completeness=DemandEvidenceCompleteness.NOT_RUN,
            algorithm_version="demand-boundary-v1",
            wave=ReleaseWave.WAVE_500,
        )
