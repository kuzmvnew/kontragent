from app.models.company_fact import COMPANY_FACT_TYPES
from app.services.company_fact_service import canonical_value_hash


def test_company_fact_types_cover_stage_1_5_scope():
    assert {
        "website", "phone", "email", "registered_address", "factual_address", "postal_address",
        "mass_address", "mass_director", "mass_founder", "public_bank_details", "related_company",
    } == set(COMPANY_FACT_TYPES)


def test_fact_hash_is_canonical_and_value_sensitive():
    assert canonical_value_hash({"bic": "044525225", "bank": "A"}) == canonical_value_hash({"bank": "A", "bic": "044525225"})
    assert canonical_value_hash({"address": "A"}) != canonical_value_hash({"address": "B"})


def test_normalized_fact_status_codes_fit_storage_contract():
    assert len("observed_unverified_current") <= 30
    assert len("source_asserted") <= 30
