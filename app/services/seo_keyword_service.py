from dataclasses import dataclass


@dataclass(frozen=True)
class SeoSeedQuery:
    query: str
    cluster: str
    weight: float
    stage: int


CLUSTER_WEIGHTS = {
    "identity": 1.0,
    "verification": 2.2,
    "reliability": 2.0,
    "risk": 2.1,
    "reputation": 1.7,
    "requisites": 1.8,
    "contacts": 1.2,
    "management": 1.6,
    "owners": 1.6,
    "relations": 1.5,
    "finance": 1.5,
    "debts": 2.0,
    "courts": 2.0,
    "bankruptcy": 2.1,
    "tax": 1.7,
    "licenses": 1.5,
    "procurement": 1.6,
    "rnp": 2.1,
    "status": 1.7,
    "staff": 1.1,
    "sanctions": 2.0,
    "cooperation": 2.2,
    "brand": 0.15,
}


STAGE_1_MODIFIERS = {
    "verification": (
        "проверить {name}",
        "проверка компании {name}",
        "проверка контрагента {name}",
    ),
    "reliability": (
        "{name} надежность",
        "надежная ли компания {name}",
    ),
    "risk": (
        "{name} риски",
        "риски компании {name}",
    ),
    "reputation": (
        "{name} отзывы",
        "ооо {name} отзывы",
    ),
    "requisites": (
        "{name} инн",
        "инн {name}",
        "{name} реквизиты",
    ),
    "contacts": (
        "{name} контакты",
        "{name} телефон",
    ),
    "courts": (
        "{name} суды",
        "{name} арбитраж",
    ),
    "debts": (
        "{name} долги",
        "{name} задолженность",
    ),
    "bankruptcy": (
        "{name} банкротство",
    ),
}


STAGE_2_MODIFIERS = {
    "management": (
        "{name} директор",
        "генеральный директор {name}",
        "руководитель {name}",
    ),
    "owners": (
        "{name} учредители",
        "{name} владельцы",
        "кому принадлежит {name}",
    ),
    "relations": (
        "связанные компании {name}",
        "аффилированные компании {name}",
    ),
    "finance": (
        "{name} выручка",
        "{name} прибыль",
        "{name} оборот",
        "финансовое состояние {name}",
    ),
    "tax": (
        "{name} налоги",
        "налоговая задолженность {name}",
    ),
    "licenses": (
        "{name} лицензии",
        "лицензия {name}",
    ),
    "procurement": (
        "{name} госзакупки",
        "{name} госконтракты",
        "{name} 44 фз",
        "{name} 223 фз",
    ),
    "rnp": (
        "{name} рнп",
        "{name} реестр недобросовестных поставщиков",
    ),
    "status": (
        "{name} действующая компания",
        "{name} ликвидация",
        "статус {name}",
    ),
    "staff": (
        "{name} сотрудники",
        "численность {name}",
    ),
    "sanctions": (
        "{name} санкции",
        "{name} санкционный список",
        "{name} комплаенс",
    ),
    "cooperation": (
        "можно ли работать с {name}",
        "стоит ли работать с {name}",
        "проверить перед договором {name}",
    ),
}


def _normalize_name(value: str) -> str:
    return " ".join((value or "").strip().split())


def _unique(values):
    seen = set()
    result = []
    for value in values:
        normalized = _normalize_name(value)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def build_aliases(
    *,
    legal_name: str,
    short_name: str | None = None,
    brand_names: list[str] | None = None,
    former_names: list[str] | None = None,
) -> list[str]:
    aliases = [legal_name]
    if short_name:
        aliases.append(short_name)
    aliases.extend(brand_names or [])
    aliases.extend(former_names or [])
    return _unique(aliases)


def generate_seed_queries(
    *,
    legal_name: str,
    inn: str,
    short_name: str | None = None,
    brand_names: list[str] | None = None,
    former_names: list[str] | None = None,
    stage: int = 1,
) -> list[SeoSeedQuery]:
    aliases = build_aliases(
        legal_name=legal_name,
        short_name=short_name,
        brand_names=brand_names,
        former_names=former_names,
    )
    if not aliases:
        return []

    queries = []

    # The exact INN is a high-intent identifier and must always be measured.
    clean_inn = "".join(ch for ch in str(inn) if ch.isdigit())
    if clean_inn:
        queries.append(
            SeoSeedQuery(
                query=clean_inn,
                cluster="identity",
                weight=CLUSTER_WEIGHTS["identity"],
                stage=1,
            )
        )
        queries.append(
            SeoSeedQuery(
                query=f"инн {clean_inn}",
                cluster="identity",
                weight=CLUSTER_WEIGHTS["identity"],
                stage=1,
            )
        )

    modifier_sets = [STAGE_1_MODIFIERS]
    if stage >= 2:
        modifier_sets.append(STAGE_2_MODIFIERS)

    # Use all legal aliases, but keep raw brand-only demand low-weighted.
    for alias in aliases:
        queries.append(
            SeoSeedQuery(
                query=alias,
                cluster="brand",
                weight=CLUSTER_WEIGHTS["brand"],
                stage=1,
            )
        )

        for modifier_set in modifier_sets:
            for cluster, templates in modifier_set.items():
                for template in templates:
                    queries.append(
                        SeoSeedQuery(
                            query=template.format(name=alias),
                            cluster=cluster,
                            weight=CLUSTER_WEIGHTS[cluster],
                            stage=(
                                1
                                if modifier_set is STAGE_1_MODIFIERS
                                else 2
                            ),
                        )
                    )

    deduped = []
    seen = set()
    for item in queries:
        key = item.query.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def score_company_demand(
    *,
    measurements: list[dict],
    card_completeness: float,
    enterprise_navigation_risk: bool = False,
    consumer_navigation_risk: bool = False,
) -> dict:
    """Calculate a transparent provisional SEO priority score.

    measurements rows: {cluster, count, weight?}. Counts are Wordstat
    frequencies for exact measured seeds. The formula is intentionally simple
    and explainable; it can be calibrated after Search Console/Webmaster data
    arrives.
    """

    cluster_scores = {}
    raw_total = 0.0
    high_intent_total = 0.0

    for row in measurements:
        cluster = str(row.get("cluster") or "").strip()
        try:
            count = max(0, int(row.get("count") or 0))
        except (TypeError, ValueError):
            count = 0
        try:
            weight = float(
                row.get("weight")
                if row.get("weight") is not None
                else CLUSTER_WEIGHTS.get(cluster, 1.0)
            )
        except (TypeError, ValueError):
            weight = 1.0

        contribution = count * weight
        cluster_scores[cluster] = (
            cluster_scores.get(cluster, 0.0) + contribution
        )
        raw_total += count
        if cluster != "brand":
            high_intent_total += count

    brand_count = sum(
        max(0, int(row.get("count") or 0))
        for row in measurements
        if row.get("cluster") == "brand"
    )
    denominator = max(1, brand_count + high_intent_total)
    intent_ratio = high_intent_total / denominator

    weighted_total = sum(cluster_scores.values())
    navigation_penalty = 1.0
    if enterprise_navigation_risk:
        navigation_penalty *= 0.75
    if consumer_navigation_risk:
        navigation_penalty *= 0.60

    completeness = min(1.0, max(0.0, float(card_completeness)))
    completeness_factor = 0.50 + (0.50 * completeness)

    score = weighted_total * navigation_penalty * completeness_factor

    return {
        "seo_priority_score": round(score, 2),
        "raw_search_demand": int(raw_total),
        "high_intent_demand": int(high_intent_total),
        "intent_ratio": round(intent_ratio, 4),
        "cluster_scores": {
            key: round(value, 2)
            for key, value in sorted(cluster_scores.items())
        },
        "navigation_penalty": navigation_penalty,
        "card_completeness": completeness,
    }
