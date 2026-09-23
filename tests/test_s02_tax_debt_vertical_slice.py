from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.contracts.risk_v3 import (
    Execution,
    Freshness,
    Observation,
    OverallRiskResult,
    ResolutionState,
)
from app.contracts.s02_tax_debt import S02FactState
from app.services.s02_tax_debt_vertical_slice_service import (
    build_s02_tax_debt_fact,
    calculate_s02_vertical_slice,
    s02_tax_debt_fact_to_risk_candidate,
)
from tests.test_risk_v3 import COMPANY_ID, complete_legal_baseline, replace


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
SOURCE_AS_OF = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
DATA_AS_OF = date(2026, 9, 1)


def _fact(state: S02FactState, *, amount=Decimal("125.00")):
    values = {
        "company_id": COMPANY_ID,
        "state": state,
        "checked_at": NOW,
        "evidence_refs": (f"s02-evidence:{state.value}",),
        "source_as_of": SOURCE_AS_OF,
        "retrieved_at": RETRIEVED_AT,
        "official_actual_until": date(2026, 9, 30),
    }
    if state == S02FactState.FOUND:
        values.update(
            amount=amount,
            amount_as_of_date=DATA_AS_OF,
            total_arrears=amount - Decimal("25.00") if amount else Decimal("0"),
            total_penalties=Decimal("20.00") if amount else Decimal("0"),
            total_fines=Decimal("5.00") if amount else Decimal("0"),
            source_reference="file:///internal/raw.zip#record=secret",
            source_document_id="DEBT-1",
            provenance={
                "source_id": "S02",
                "artifact_sha256": "a" * 64,
                "artifact_reference": "file:///internal/raw.zip",
                "worker_run_id": "internal-worker-run",
                "source_document_id": "DEBT-1",
                "source_member": "data.xml",
                "record_hash": "b" * 64,
                "source_as_of": SOURCE_AS_OF.isoformat(),
                "retrieved_at": RETRIEVED_AT.isoformat(),
                "parser_version": "fns-debtam-xml-v1",
                "normalization_version": "tax-debt-normalization-v1",
                "matching_method": "inn_exact",
            },
        )
    elif state == S02FactState.NOT_FOUND:
        values.update(
            amount_as_of_date=DATA_AS_OF,
            limitations=("absence_is_limited_to_current_snapshot",),
        )
    elif state == S02FactState.STALE_DATA:
        values.update(
            amount_as_of_date=DATA_AS_OF,
            freshness_reason="official_actual_until_expired",
            limitations=("stale_data",),
        )
    else:
        values.update(
            freshness_reason="dataset_not_available",
            limitations=("dataset_not_available",),
        )
    return build_s02_tax_debt_fact(**values)


def _candidate(state: S02FactState, *, amount=Decimal("125.00")):
    return s02_tax_debt_fact_to_risk_candidate(
        _fact(state, amount=amount),
        inn="7700000000",
    )


def _slice(state: S02FactState, *, amount=Decimal("125.00")):
    values = replace(
        complete_legal_baseline(),
        "tax_debt",
        _candidate(state, amount=amount),
    )
    return calculate_s02_vertical_slice(
        values,
        company_id=COMPANY_ID,
        calculated_at=NOW,
    )


def test_s02_fact_creation_carries_amount_date_source_freshness_and_provenance():
    fact = _fact(S02FactState.FOUND)

    assert fact.fact_code == "tax.debt.amount_as_of_date"
    assert fact.amount == Decimal("125.00")
    assert fact.amount_as_of_date == DATA_AS_OF
    assert fact.source.source_id == "S02"
    assert fact.freshness.status == Freshness.CURRENT
    assert fact.provenance.artifact_sha256 == "a" * 64
    assert fact.provenance.matching_method == "inn_exact"
    assert "not_real_time_balance" in fact.limitations


def test_s02_fact_is_the_existing_risk_input_without_engine_changes():
    candidate = _candidate(S02FactState.FOUND)

    assert candidate.capability_code == "tax_debt"
    assert candidate.fact_identity == "tax.current_debt"
    assert candidate.observation == Observation.FOUND
    assert candidate.execution == Execution.CHECKED
    assert candidate.fact_payload["adverse"] is True
    assert candidate.fact_payload["total_debt"] == "125.00"
    assert candidate.scope_details["s02_fact"]["state"] == "FOUND"


def test_s02_risk_to_summary_keeps_traceable_confirmed_fact():
    fact, risk, summary, _ = _slice(S02FactState.FOUND)

    tax_check = next(
        item for item in risk.resolved_checks if item.capability_code == "tax_debt"
    )
    assert tax_check.resolution_state == ResolutionState.RESOLVED
    assert any(item.factor_code == "TAX_DEBT_PRESENT" for item in risk.factors)
    assert any(
        item.fact_code == fact.fact_code
        and item.origin_check_ref == tax_check.check_ref
        for item in summary.confirmed_positive_facts
    )
    assert any(
        ref.endswith("TAX_DEBT_PRESENT") for ref in summary.key_reason_refs
    )


def test_s02_public_projection_contains_card_data_and_hides_raw_coordinates():
    fact, risk, summary, projection = _slice(S02FactState.FOUND)
    payload = projection.model_dump(mode="json")

    assert projection.summary.summary_id == summary.summary_id
    assert projection.risk.assessment_id == risk.assessment_id
    assert projection.evidence.fact_ref == fact.fact_ref
    assert projection.evidence.amount == Decimal("125.00")
    assert projection.evidence.total_arrears == Decimal("100.00")
    assert projection.coverage.resolution_state == ResolutionState.RESOLVED
    assert projection.freshness.status == Freshness.CURRENT
    assert "not_real_time_balance" in projection.limitations
    rendered = str(payload)
    assert "file:///internal" not in rendered
    assert "internal-worker-run" not in rendered
    assert "data.xml" not in rendered
    assert "a" * 64 not in rendered


@pytest.mark.parametrize(
    ("state", "observation", "execution", "freshness", "resolved"),
    (
        (
            S02FactState.FOUND,
            Observation.FOUND,
            Execution.CHECKED,
            Freshness.CURRENT,
            ResolutionState.RESOLVED,
        ),
        (
            S02FactState.NOT_FOUND,
            Observation.NOT_FOUND,
            Execution.CHECKED,
            Freshness.CURRENT,
            ResolutionState.RESOLVED,
        ),
        (
            S02FactState.STALE_DATA,
            Observation.UNKNOWN,
            Execution.CHECKED,
            Freshness.STALE,
            ResolutionState.UNRESOLVED,
        ),
        (
            S02FactState.SOURCE_UNAVAILABLE,
            Observation.UNKNOWN,
            Execution.SOURCE_UNAVAILABLE,
            Freshness.UNKNOWN,
            ResolutionState.UNRESOLVED,
        ),
    ),
)
def test_s02_states_are_preserved_through_risk_and_projection(
    state,
    observation,
    execution,
    freshness,
    resolved,
):
    _, risk, _, projection = _slice(state)
    check = next(
        item for item in risk.resolved_checks if item.capability_code == "tax_debt"
    )

    assert projection.evidence.state == state
    assert check.observation == observation
    assert check.execution == execution
    assert check.freshness == freshness
    assert check.resolution_state == resolved
    assert projection.coverage.resolution_state == resolved


@pytest.mark.parametrize(
    "state",
    (
        S02FactState.NOT_FOUND,
        S02FactState.STALE_DATA,
        S02FactState.SOURCE_UNAVAILABLE,
    ),
)
def test_no_data_never_creates_false_tax_debt_risk(state):
    _, risk, summary, projection = _slice(state)

    assert all(item.factor_code != "TAX_DEBT_PRESENT" for item in risk.factors)
    assert all(
        item.fact_code != "tax.debt.amount_as_of_date"
        for item in summary.confirmed_positive_facts
    )
    assert projection.risk.factors == ()
    if state == S02FactState.NOT_FOUND:
        assert (
            risk.overall_result
            == OverallRiskResult.NO_ADVERSE_FACTORS_AFTER_MANDATORY_GATE
        )
    else:
        assert risk.overall_result == OverallRiskResult.INCOMPLETE_NO_POSITIVE_CONCLUSION


def test_zero_amount_source_record_is_negative_closure_not_risk():
    fact, risk, _, projection = _slice(S02FactState.FOUND, amount=Decimal("0"))
    check = next(
        item for item in risk.resolved_checks if item.capability_code == "tax_debt"
    )

    assert fact.state == S02FactState.FOUND
    assert fact.has_debt is False
    assert check.observation == Observation.NOT_FOUND
    assert check.negative_closure_proven is True
    assert projection.risk.factors == ()
