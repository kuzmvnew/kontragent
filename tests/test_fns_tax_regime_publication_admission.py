"""Pure C6 publication lifecycle and admission fingerprint invariants."""

from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.fns_tax_regime_publication_admission import (
    PublicationLifecycle,
    canonical_fingerprint,
    canonical_sha256,
    family_identity,
    validate_publication_lifecycle,
)


SOURCE_DATE = date(2026, 9, 1)
LEGAL_ID = "1" * 64
IP_ID = "2" * 64
FAMILY_ID = family_identity({"legal": LEGAL_ID, "ip": IP_ID})


def _accepted_graph():
    datasets = {}
    for code, identity in (("fns_snr", LEGAL_ID), ("fns_snrip", IP_ID)):
        datasets[code] = SimpleNamespace(
            code=code,
            coverage={"release_identity": identity, "source_data_date": SOURCE_DATE.isoformat()},
            last_success_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            last_data_date=SOURCE_DATE,
            source_as_of=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
    datasets["fns_tax_regime"] = SimpleNamespace(
        code="fns_tax_regime",
        coverage={
            "release_identity": FAMILY_ID,
            "family_bundle_identity": FAMILY_ID,
            "members": {
                "legal": {"release_identity": LEGAL_ID, "source_data_date": SOURCE_DATE.isoformat()},
                "ip": {"release_identity": IP_ID, "source_data_date": SOURCE_DATE.isoformat()},
            },
        },
        last_success_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        last_data_date=SOURCE_DATE,
        source_as_of=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    state = SimpleNamespace(
        source_id="fns_tax_regime",
        generation=1,
        active_pointer=f"file:///tmp/fns_tax_regime/bundles/{FAMILY_ID}/normalized-bundle.json",
        last_fencing_token=1,
        published_by_run_id=None,
        updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        validation_metadata={
            "checksum": "a" * 64,
            "validation": {
                "release_identity": FAMILY_ID,
                "source_data_date": SOURCE_DATE.isoformat(),
                "member_release_identities": {"legal": LEGAL_ID, "ip": IP_ID},
            },
        },
    )
    return state, datasets


def test_no_state_is_initial_only_without_any_persisted_publication_evidence():
    state, datasets = _accepted_graph()
    del state
    assert validate_publication_lifecycle(None, {}).lifecycle == PublicationLifecycle.NO_PUBLICATION
    assert validate_publication_lifecycle(None, datasets).reasons == ("lost_accepted_publication_state",)
    assert validate_publication_lifecycle(None, {}, published_fact_count=1).lifecycle == PublicationLifecycle.CORRUPT


def test_valid_accepted_old_release_proof_is_independent_of_fresh_discovery():
    state, datasets = _accepted_graph()
    verdict = validate_publication_lifecycle(state, datasets)
    assert verdict.lifecycle == PublicationLifecycle.ACCEPTED_VALID
    assert verdict.proof.release_identity == FAMILY_ID
    assert verdict.proof.member_release_identities == {"legal": LEGAL_ID, "ip": IP_ID}


@pytest.mark.parametrize("generation", [True, False, 0, -1, 1.0, "1", None])
def test_generation_must_be_strict_positive_int(generation):
    state, datasets = _accepted_graph()
    state.generation = generation
    verdict = validate_publication_lifecycle(state, datasets)
    assert verdict.lifecycle == PublicationLifecycle.CORRUPT
    assert "accepted_generation_invalid" in verdict.reasons


@pytest.mark.parametrize("checksum", [
    "not-a-sha256", "", "a" * 63, "a" * 65, "A" * 64,
    "g" * 64, " " + "a" * 64, "a" * 64 + " ", "sha256:" + "a" * 64,
    b"a" * 64, ["a" * 64], True, None,
])
def test_checksum_requires_exact_lowercase_sha256(checksum):
    state, datasets = _accepted_graph()
    state.validation_metadata["checksum"] = checksum
    verdict = validate_publication_lifecycle(state, datasets)
    assert verdict.lifecycle == PublicationLifecycle.CORRUPT
    assert "accepted_checksum_invalid" in verdict.reasons
    assert not canonical_sha256(checksum)


@pytest.mark.parametrize(("mutation", "reason"), [
    ("identity_missing", "accepted_family_identity_invalid"),
    ("identity_arbitrary", "accepted_family_identity_invalid"),
    ("identity_other_sha", "accepted_family_composition_mismatch"),
    ("member_changed", "accepted_family_composition_mismatch"),
    ("member_map_missing", "accepted_member_map_invalid"),
    ("source_date_missing", "accepted_source_date_invalid"),
    ("family_coverage_changed", "accepted_family_dataset_identity_mismatch"),
    ("child_changed", "accepted_legal_child_identity_mismatch"),
    ("pointer_changed", "accepted_pointer_invalid"),
    ("metadata_missing", "accepted_validation_metadata_invalid"),
])
def test_corrupt_accepted_graph_cannot_become_new_release(mutation, reason):
    state, datasets = _accepted_graph()
    validation = state.validation_metadata["validation"]
    if mutation == "identity_missing":
        del validation["release_identity"]
    elif mutation == "identity_arbitrary":
        validation["release_identity"] = "previous-release"
    elif mutation == "identity_other_sha":
        validation["release_identity"] = "b" * 64
    elif mutation == "member_changed":
        validation["member_release_identities"]["legal"] = "c" * 64
    elif mutation == "member_map_missing":
        del validation["member_release_identities"]
    elif mutation == "source_date_missing":
        del validation["source_data_date"]
    elif mutation == "family_coverage_changed":
        datasets["fns_tax_regime"].coverage["release_identity"] = "b" * 64
    elif mutation == "child_changed":
        datasets["fns_snr"].coverage["release_identity"] = "b" * 64
    elif mutation == "pointer_changed":
        state.active_pointer = "file:///tmp/elsewhere.json"
    elif mutation == "metadata_missing":
        state.validation_metadata = {}
    verdict = validate_publication_lifecycle(state, datasets)
    assert verdict.lifecycle == PublicationLifecycle.CORRUPT
    assert reason in verdict.reasons


def test_existing_empty_state_is_corrupt_not_a_pristine_initial_state():
    state, datasets = _accepted_graph()
    state.active_pointer = None
    state.generation = 0
    state.validation_metadata = {}
    assert validate_publication_lifecycle(state, datasets).lifecycle == PublicationLifecycle.CORRUPT


def test_admission_fingerprint_is_order_independent_and_sensitivity_is_exact():
    original = {"b": {"y": 2, "x": 1}, "a": ["legal", "ip"]}
    reordered = {"a": ["legal", "ip"], "b": {"x": 1, "y": 2}}
    assert canonical_fingerprint(original) == canonical_fingerprint(reordered)
    modified = deepcopy(original)
    modified["b"]["x"] += 1
    assert canonical_fingerprint(original) != canonical_fingerprint(modified)
