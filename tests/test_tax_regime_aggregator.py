from app.aggregators import company_aggregator
from app.services.tax_regime_service import (
    get_regime_name,
)


def test_tax_regime_names():
    assert (
        get_regime_name("usn")
        == "Упрощённая система налогообложения (УСН)"
    )

    assert (
        get_regime_name("psn")
        == "Патентная система налогообложения (ПСН)"
    )

    assert (
        get_regime_name("npd")
        == "Налог на профессиональный доход (НПД)"
    )

    assert get_regime_name("unknown") is None


def test_structured_domain_data_contains_tax_regime(
    monkeypatch,
):
    expected_profile = {
        "company_id": 123,
        "entity_type": "legal",
        "data_date": "2026-08-01",
        "regime_codes": [
            "usn",
        ],
        "regimes": [
            {
                "code": "usn",
                "name": (
                    "Упрощённая система "
                    "налогообложения (УСН)"
                ),
            }
        ],
        "dataset_id": 21,
        "dataset_code": "fns_snr",
    }

    monkeypatch.setattr(
        company_aggregator,
        "get_msp_profile_for_company",
        lambda company_id: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_regime_profile_for_company",
        lambda company_id: expected_profile,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_revenue_expense_check_for_company",
        lambda company_id: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_debt_check_for_company",
        lambda company_id, include_items=True: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_latest_tax_debt_for_company",
        lambda company_id, include_items=True: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_debt_history",
        lambda company_id, limit=24: [],
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_offence_check_for_company",
        lambda company_id: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_offence_history",
        lambda company_id, limit=20: [],
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_latest_tax_payment_for_company",
        lambda company_id: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_payment_history",
        lambda company_id, limit=10: [],
    )

    result = (
        company_aggregator.load_structured_domain_data(
            company_id=123
        )
    )

    assert (
        result["tax_regime_profile"]
        == expected_profile
    )

    assert (
        result["tax_regime_profile"][
            "dataset_code"
        ]
        == "fns_snr"
    )
