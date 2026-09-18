import json
from datetime import datetime, timezone

from app.contracts.source_architecture import NormalizedResultStatus, SourceClass
from scripts.accept_product_recovery_v3 import build_fssp_comparison, historical_fssp_result


def _official_row():
    return {
        "classification": "MISMATCH", "firmoteka_amount": "1617123987.05",
        "firmoteka_count": "74", "firmoteka_date": "2026-09-16",
        "firmoteka_result": "found", "inn": "0274101890", "name": "ООО ФИРМА",
        "official_amount_coverage": "85/96 visible result rows",
        "official_amount_visible_rows": "1993729468.56",
        "official_checked_at": "2026-09-17", "official_count": "96",
        "official_result": "found", "official_url": "https://fssp.gov.ru/iss/ip/",
        "validation_note": "preserved official result",
    }


def test_preserved_official_fssp_result_has_explicit_historical_evidence_class():
    result = historical_fssp_result(
        _official_row(), now=datetime(2026, 9, 18, tzinfo=timezone.utc),
    )
    assert result.result == NormalizedResultStatus.FOUND
    assert result.source_class == SourceClass.OFFICIAL_DIRECT
    assert result.source_code == "fssp_direct_historical_acceptance"
    assert result.evidence[0].metadata["evidence_class"] == "EXTERNAL_HISTORICAL_EVIDENCE"
    assert result.evidence[0].value["count"] == 96
    assert "85/96" in result.limitation


def test_fssp_comparison_reconciles_count_semantics_but_not_amounts(tmp_path):
    normalized = tmp_path / "normalized"
    normalized.mkdir()
    (normalized / "0274101890.json").write_text(json.dumps({
        "enforcements": {
            "count": 74, "completed_count": 22, "closed_count": 1,
            "total_due": 1_973_646_613.86,
        },
    }), encoding="utf-8")
    artifact, classifications = build_fssp_comparison(
        tmp_path, [_official_row()],
        generated_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
    )
    comparison = artifact["comparisons"][0]
    assert comparison["difference"] == {
        "raw_count_delta": 22, "current_plus_completed": 96,
        "count_reconciled": True, "amounts_comparable": False,
    }
    assert classifications["0274101890"] == "COUNT_SEMANTICS_RECONCILED_AMOUNT_UNRESOLVED"
    assert artifact["policy"] == {
        "positive_bridge_allowed": True, "negative_bridge_allowed": False,
    }
