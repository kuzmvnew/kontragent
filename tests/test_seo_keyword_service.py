from app.services.seo_keyword_service import (
    generate_seed_queries,
    score_company_demand,
)


def test_generate_seed_queries_contains_high_intent_clusters():
    queries = generate_seed_queries(
        legal_name='ООО "Ромашка"',
        short_name="Ромашка",
        inn="6671234567",
        brand_names=["Romashka"],
        stage=2,
    )

    by_query = {item.query.casefold(): item for item in queries}

    assert "инн 6671234567" in by_query
    assert "проверить ромашка" in by_query
    assert "ромашка надежность" in by_query
    assert "ромашка риски" in by_query
    assert "ромашка отзывы" in by_query
    assert "ромашка контакты" in by_query
    assert "ромашка суды" in by_query
    assert "ромашка банкротство" in by_query
    assert "ромашка директор" in by_query
    assert "ромашка учредители" in by_query
    assert "ромашка выручка" in by_query
    assert "ромашка санкции" in by_query
    assert "можно ли работать с ромашка" in by_query


def test_brand_query_weight_is_lower_than_verification_weight():
    queries = generate_seed_queries(
        legal_name="Ромашка",
        inn="6671234567",
        stage=1,
    )
    by_query = {item.query.casefold(): item for item in queries}

    assert by_query["ромашка"].weight < by_query["проверить ромашка"].weight


def test_score_penalizes_navigation_risk_but_preserves_high_intent_demand():
    measurements = [
        {"cluster": "brand", "count": 10000},
        {"cluster": "verification", "count": 2000},
        {"cluster": "risk", "count": 1000},
        {"cluster": "requisites", "count": 1500},
    ]

    clean = score_company_demand(
        measurements=measurements,
        card_completeness=1.0,
    )
    noisy = score_company_demand(
        measurements=measurements,
        card_completeness=1.0,
        enterprise_navigation_risk=True,
        consumer_navigation_risk=True,
    )

    assert clean["high_intent_demand"] == 4500
    assert clean["intent_ratio"] > 0
    assert noisy["seo_priority_score"] < clean["seo_priority_score"]


def test_score_rewards_more_complete_card():
    measurements = [
        {"cluster": "verification", "count": 1000},
        {"cluster": "risk", "count": 500},
    ]

    partial = score_company_demand(
        measurements=measurements,
        card_completeness=0.4,
    )
    complete = score_company_demand(
        measurements=measurements,
        card_completeness=1.0,
    )

    assert complete["seo_priority_score"] > partial["seo_priority_score"]
