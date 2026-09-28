from __future__ import annotations

import hashlib
import re
from pathlib import Path

from scripts.accept_seo10k_foundation import CONTRACT_SHA256, RELEASE_GATES


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "SEO-10K_RELEASE_CONTRACT.md"

B_GATES = {
    "SEO10K-B01": "Runtime",
    "SEO10K-B02": "Leakage",
    "SEO10K-B03": "Sitemap processing",
    "SEO10K-B04": "Canonical/duplicates",
    "SEO10K-B05": "Discovery/indexing",
    "SEO10K-B06": "Snippet safety",
    "SEO10K-B07": "Query relevance",
    "SEO10K-B08": "Data/product quality",
    "SEO10K-B09": "Performance",
    "SEO10K-B10": "Rollback readiness",
}

C_GATES = {
    "SEO10K-C01": "Full-corpus rerun",
    "SEO10K-C02": "2K runtime",
    "SEO10K-C03": "2K indexing",
    "SEO10K-C04": "Quality exclusions",
    "SEO10K-C05": "Catalog quality",
    "SEO10K-C06": "Semantic audit",
    "SEO10K-C07": "Demand model",
    "SEO10K-C08": "Cache/rollback at scale",
    "SEO10K-C09": "Capacity",
    "SEO10K-C10": "Decision record",
}


def _gate_mapping(text: str) -> dict[str, str]:
    return {
        match.group(1): match.group(2).strip()
        for match in re.finditer(
            r"^\| (SEO10K-[ABC]\d{2}) \| ([^|]+) \|",
            text,
            flags=re.MULTILINE,
        )
    }


def test_canonical_contract_identity_structure_and_not_released_status():
    payload = CONTRACT.read_bytes()
    text = payload.decode("utf-8")
    assert len(text.splitlines()) == 590
    assert hashlib.sha256(payload).hexdigest() == CONTRACT_SHA256
    assert "**Версия:** 1.0" in text
    assert "**Дата:** 2026-09-27" in text
    assert "**Статус:** PREPARED / NOT RELEASED" in text
    required_headings = [
        "## 1. Обязательные инварианты",
        "## 2. Production architecture",
        "## 3. Выбор и порядок когорты 10 000",
        "## 4. URL contract и canonical",
        "## 5. SEO eligibility",
        "## 6. Page template и метаданные",
        "## 7. Semantic-content requirements и thin-page protection",
        "## 8. Structured data",
        "## 9. robots и meta robots",
        "## 10. Sitemap architecture",
        "## 11. Catalog pages, pagination и internal linking",
        "## 12. Duplicate, stale и disputed policy",
        "## 13. Republishing и cache invalidation",
        "## 14. Rollout 500 → 2 000 → 10 000",
        "## 15. Exact acceptance gates",
        "## 16. Measurement",
        "## 17. Required implementation artifacts",
        "## 18. Evidence boundary",
        "## HANDOFF → 00 — Управление проектом",
    ]
    assert all(heading in text for heading in required_headings)


def test_canonical_contract_preserves_invariants_demand_formula_and_all_gate_names():
    text = CONTRACT.read_text(encoding="utf-8")
    invariant_section = text.split("## 1. Обязательные инварианты", 1)[1].split(
        "## 2. Production architecture", 1
    )[0]
    assert len(re.findall(r"^\d+\. ", invariant_section, flags=re.MULTILINE)) == 12
    assert "DemandScore = 0.60 × Y + 0.40 × G" in text
    assert "Яндекс Wordstat API" in text
    assert "GenerateKeywordHistoricalMetrics" in text
    assert "12-месячный срез" in text
    assert "Keysso" in text
    assert _gate_mapping(text) == {**RELEASE_GATES, **B_GATES, **C_GATES}
