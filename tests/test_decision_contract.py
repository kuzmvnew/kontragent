from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.contracts.decision import (
    CheckResultStatus,
    Coverage,
    CoverageStatus,
    Evidence,
    build_coverage,
    evidence_from_check_result,
)


DATA_DATE = date(2026, 8, 1)

CHECKED_AT = datetime(
    2026,
    9,
    14,
    12,
    0,
    tzinfo=timezone.utc,
)


def make_evidence(
    *,
    evidence_id,
    result,
):
    if result in {
        CheckResultStatus.FOUND,
        CheckResultStatus.NOT_FOUND,
    }:
        data_date = DATA_DATE
        reason = None

    else:
        data_date = None
        reason = "test_reason"

    record_ids = (
        ("record-1",)
        if result == CheckResultStatus.FOUND
        else ()
    )

    return Evidence(
        evidence_id=evidence_id,
        source_code="fns",
        dataset_code="test_dataset",
        result=result,
        data_date=data_date,
        checked_at=CHECKED_AT,
        reason=reason,
        record_ids=record_ids,
    )


@pytest.mark.parametrize(
    "result",
    [
        CheckResultStatus.FOUND,
        CheckResultStatus.NOT_FOUND,
        CheckResultStatus.NOT_APPLICABLE,
        CheckResultStatus.UNAVAILABLE,
    ],
)
def test_evidence_accepts_all_valid_results(
    result,
):
    evidence = make_evidence(
        evidence_id=f"evidence-{result}",
        result=result,
    )

    assert evidence.result == result


@pytest.mark.parametrize(
    "result",
    [
        CheckResultStatus.FOUND,
        CheckResultStatus.NOT_FOUND,
    ],
)
def test_completed_check_requires_data_date(
    result,
):
    with pytest.raises(
        ValidationError,
        match="обязательна data_date",
    ):
        Evidence(
            evidence_id="evidence-1",
            source_code="fns",
            dataset_code="test_dataset",
            result=result,
            data_date=None,
            checked_at=CHECKED_AT,
        )


@pytest.mark.parametrize(
    "result",
    [
        CheckResultStatus.NOT_APPLICABLE,
        CheckResultStatus.UNAVAILABLE,
    ],
)
def test_incomplete_check_requires_reason(
    result,
):
    with pytest.raises(
        ValidationError,
        match="обязательна reason",
    ):
        Evidence(
            evidence_id="evidence-1",
            source_code="fns",
            dataset_code="test_dataset",
            result=result,
            data_date=None,
            checked_at=CHECKED_AT,
            reason=None,
        )


def test_only_found_can_contain_record_ids():
    with pytest.raises(
        ValidationError,
        match="только для found",
    ):
        Evidence(
            evidence_id="evidence-1",
            source_code="fns",
            dataset_code="test_dataset",
            result=CheckResultStatus.NOT_FOUND,
            data_date=DATA_DATE,
            checked_at=CHECKED_AT,
            record_ids=("record-1",),
        )


def test_checked_at_must_have_timezone():
    with pytest.raises(
        ValidationError,
        match="часовой пояс",
    ):
        Evidence(
            evidence_id="evidence-1",
            source_code="fns",
            dataset_code="test_dataset",
            result=CheckResultStatus.FOUND,
            data_date=DATA_DATE,
            checked_at=datetime(
                2026,
                9,
                14,
                12,
                0,
            ),
        )


def test_record_ids_must_be_unique():
    with pytest.raises(
        ValidationError,
        match="не должны содержать дубли",
    ):
        Evidence(
            evidence_id="evidence-1",
            source_code="fns",
            dataset_code="test_dataset",
            result=CheckResultStatus.FOUND,
            data_date=DATA_DATE,
            checked_at=CHECKED_AT,
            record_ids=(
                "record-1",
                "record-1",
            ),
        )


def test_evidence_is_immutable():
    evidence = make_evidence(
        evidence_id="evidence-1",
        result=CheckResultStatus.FOUND,
    )

    with pytest.raises(
        ValidationError,
    ):
        evidence.result = (
            CheckResultStatus.NOT_FOUND
        )


def test_evidence_rejects_unknown_fields():
    with pytest.raises(
        ValidationError,
        match="Extra inputs are not permitted",
    ):
        Evidence(
            evidence_id="evidence-1",
            source_code="fns",
            dataset_code="test_dataset",
            result=CheckResultStatus.FOUND,
            data_date=DATA_DATE,
            checked_at=CHECKED_AT,
            unexpected_field=True,
        )


def test_evidence_from_check_result_maps_existing_contract():
    check_result = {
        "checked": True,
        "applicable": True,
        "result": "found",
        "data_date": DATA_DATE,
        "dataset_code": "fns_tax_offence",
        "source": "fns_tax_offence",
        "reason": None,
        "fine_amount": Decimal("125.00"),
    }

    evidence = evidence_from_check_result(
        check_result,
        evidence_id="tax-offence-current",
        checked_at=CHECKED_AT,
        record_ids=(
            "OFFENCE-1",
            "OFFENCE-2",
        ),
        metadata={
            "fine_amount": Decimal("125.00"),
        },
    )

    assert (
        evidence.result
        == CheckResultStatus.FOUND
    )

    assert (
        evidence.data_date
        == DATA_DATE
    )

    assert (
        evidence.record_ids
        == (
            "OFFENCE-1",
            "OFFENCE-2",
        )
    )

    assert (
        evidence.metadata["fine_amount"]
        == Decimal("125.00")
    )


def test_evidence_from_check_result_requires_contract_fields():
    with pytest.raises(
        ValueError,
        match="отсутствуют поля",
    ):
        evidence_from_check_result(
            {
                "result": "found",
            },
            evidence_id="evidence-1",
            checked_at=CHECKED_AT,
        )


def test_evidence_from_check_result_rejects_invalid_flags():
    with pytest.raises(
        ValueError,
        match="found/not_found требуют",
    ):
        evidence_from_check_result(
            {
                "checked": False,
                "applicable": True,
                "result": "found",
                "data_date": DATA_DATE,
                "dataset_code": "fns_tax_offence",
                "source": "fns_tax_offence",
                "reason": None,
            },
            evidence_id="evidence-1",
            checked_at=CHECKED_AT,
        )


def test_build_coverage_for_complete_checks():
    coverage = build_coverage(
        [
            make_evidence(
                evidence_id="evidence-1",
                result=CheckResultStatus.FOUND,
            ),
            make_evidence(
                evidence_id="evidence-2",
                result=CheckResultStatus.NOT_FOUND,
            ),
        ]
    )

    assert (
        coverage.status
        == CoverageStatus.COMPLETE
    )

    assert (
        coverage.ratio
        == Decimal("1.0000")
    )

    assert (
        coverage.percent
        == Decimal("100.00")
    )

    assert coverage.completed_checks == 2


def test_build_coverage_for_partial_checks():
    coverage = build_coverage(
        [
            make_evidence(
                evidence_id="evidence-1",
                result=CheckResultStatus.FOUND,
            ),
            make_evidence(
                evidence_id="evidence-2",
                result=CheckResultStatus.UNAVAILABLE,
            ),
        ]
    )

    assert (
        coverage.status
        == CoverageStatus.PARTIAL
    )

    assert (
        coverage.ratio
        == Decimal("0.5000")
    )

    assert (
        coverage.percent
        == Decimal("50.00")
    )

    assert coverage.unavailable_checks == 1


def test_build_coverage_when_all_applicable_checks_unavailable():
    coverage = build_coverage(
        [
            make_evidence(
                evidence_id="evidence-1",
                result=CheckResultStatus.UNAVAILABLE,
            ),
        ]
    )

    assert (
        coverage.status
        == CoverageStatus.UNAVAILABLE
    )

    assert (
        coverage.ratio
        == Decimal("0.0000")
    )

    assert (
        coverage.percent
        == Decimal("0.00")
    )


def test_build_coverage_excludes_not_applicable_from_ratio():
    coverage = build_coverage(
        [
            make_evidence(
                evidence_id="evidence-1",
                result=CheckResultStatus.FOUND,
            ),
            make_evidence(
                evidence_id="evidence-2",
                result=CheckResultStatus.NOT_APPLICABLE,
            ),
        ]
    )

    assert coverage.total_checks == 2
    assert coverage.applicable_checks == 1
    assert coverage.not_applicable_checks == 1

    assert (
        coverage.percent
        == Decimal("100.00")
    )


def test_build_coverage_when_everything_is_not_applicable():
    coverage = build_coverage(
        [
            make_evidence(
                evidence_id="evidence-1",
                result=CheckResultStatus.NOT_APPLICABLE,
            ),
        ]
    )

    assert (
        coverage.status
        == CoverageStatus.NOT_APPLICABLE
    )

    assert coverage.ratio is None
    assert coverage.percent is None


def test_coverage_rejects_inconsistent_counts():
    with pytest.raises(
        ValidationError,
        match="total_checks должен равняться",
    ):
        Coverage(
            total_checks=3,
            applicable_checks=1,
            completed_checks=1,
            found_checks=1,
            not_found_checks=0,
            unavailable_checks=0,
            not_applicable_checks=1,
        )


def test_computed_coverage_fields_are_serialized():
    coverage = build_coverage(
        [
            make_evidence(
                evidence_id="evidence-1",
                result=CheckResultStatus.FOUND,
            ),
        ]
    )

    payload = coverage.model_dump(
        mode="json"
    )

    assert payload["status"] == "complete"
    assert payload["ratio"] == "1.0000"
    assert payload["percent"] == "100.00"