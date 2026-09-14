from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.contracts.assessment import EngineVersion
from app.contracts.decision import CheckResultStatus, Evidence
from app.contracts.regulatory import (
    LegalBasis,
    PermissionFinding,
    PermissionKind,
    PermissionStatus,
    RegulatoryCheck,
    RegulatoryOutcome,
    RequirementApplicability,
)
from app.contracts.report import DealContext


ASSESSMENT_DATE = date(2026, 9, 14)
CHECKED_AT = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
# Только вымышленные примеры. Сетевых запросов в тестах нет.
SOURCE_URL = "https://example.org/test-registry"
SCOPE = "Тестовая роль, работы, территория и объект на дату оценки"


def make_finding(status=PermissionStatus.NOT_CHECKED):
    if status == PermissionStatus.NOT_CHECKED:
        return PermissionFinding(
            subject_inn="7736207543", scope=SCOPE,
            status=status, explanation="Проверка не запущена",
        )
    confirmed = status in {
        PermissionStatus.VALID,
        PermissionStatus.INVALID,
        PermissionStatus.CONFIRMED_ABSENT,
    }
    result = (
        CheckResultStatus.FOUND if confirmed
        else CheckResultStatus.NOT_FOUND if status == PermissionStatus.NOT_FOUND
        else CheckResultStatus.UNAVAILABLE
    )
    evidence = Evidence(
        evidence_id="test-evidence",
        source_code="test-registry",
        dataset_code="test-permissions",
        result=result,
        data_date=None if result == CheckResultStatus.UNAVAILABLE else ASSESSMENT_DATE,
        checked_at=CHECKED_AT,
        reason="source_unavailable" if result == CheckResultStatus.UNAVAILABLE else None,
        record_ids=("test-document",) if confirmed else (),
    )
    return PermissionFinding(
        subject_inn="7736207543",
        scope=SCOPE,
        status=status,
        explanation="Вымышленный результат для проверки структуры",
        evidence=evidence,
        source_url=SOURCE_URL,
        record_id="test-document" if confirmed else None,
        verified_for_scope=confirmed,
        verified_on=ASSESSMENT_DATE if confirmed else None,
    )


def make_check(**changes):
    values = {
        "check_id": "test-check",
        "subject_inn": "7736207543",
        "requirement_code": "test.requirement",
        "permission_kind": PermissionKind.LICENSE,
        "assessment_date": ASSESSMENT_DATE,
        "scope": SCOPE,
        "deal_context": DealContext(counterparty_role="contractor"),
        "applicability": RequirementApplicability.REQUIRED,
        "applicability_reason": "Вымышленное обоснование; не юридический вывод",
        "legal_basis": (LegalBasis(
            document_title="Вымышленный тестовый документ",
            provision="Тестовый пункт",
            source_url="https://example.org/test-rule",
        ),),
        "exceptions_reviewed": True,
        "finding": make_finding(),
        "engine_version": EngineVersion(
            engine_name="test-regulatory-engine",
            version="0.1.0",
            ruleset_date=ASSESSMENT_DATE,
        ),
    }
    values.update(changes)
    return RegulatoryCheck(**values)


@pytest.mark.parametrize("applicability", list(RequirementApplicability))
@pytest.mark.parametrize("status", list(PermissionStatus))
def test_outcome_matrix(applicability, status):
    check = make_check(applicability=applicability, finding=make_finding(status))
    expected = RegulatoryOutcome.UNKNOWN
    if applicability == RequirementApplicability.NOT_REQUIRED:
        expected = RegulatoryOutcome.NOT_APPLICABLE
    elif applicability == RequirementApplicability.REQUIRED:
        if status == PermissionStatus.VALID:
            expected = RegulatoryOutcome.SATISFIED
        elif status in {PermissionStatus.INVALID, PermissionStatus.CONFIRMED_ABSENT}:
            expected = RegulatoryOutcome.CRITICAL
    assert check.outcome == expected
    assert check.is_blocking == (expected == RegulatoryOutcome.CRITICAL)


@pytest.mark.parametrize("applicability", [
    RequirementApplicability.REQUIRED, RequirementApplicability.NOT_REQUIRED,
])
@pytest.mark.parametrize("changes", [
    {"legal_basis": ()}, {"exceptions_reviewed": False},
])
def test_known_applicability_requires_basis_and_exceptions(applicability, changes):
    with pytest.raises(ValidationError):
        make_check(applicability=applicability, **changes)


def test_unknown_applicability_does_not_require_invented_basis():
    check = make_check(
        applicability=RequirementApplicability.UNKNOWN,
        legal_basis=(),
        exceptions_reviewed=False,
        finding=make_finding(PermissionStatus.INVALID),
    )
    assert check.outcome == RegulatoryOutcome.UNKNOWN
    assert not check.is_blocking


@pytest.mark.parametrize("status", [
    PermissionStatus.VALID, PermissionStatus.INVALID, PermissionStatus.CONFIRMED_ABSENT,
])
@pytest.mark.parametrize("changes", [
    {"evidence": None},
    {"source_url": None},
    {"record_id": None},
    {"record_id": "another-document"},
    {"verified_for_scope": False},
    {"verified_on": None},
])
def test_confirmed_finding_requires_proof(status, changes):
    values = make_finding(status).model_dump(round_trip=True)
    values.update(changes)
    with pytest.raises(ValidationError):
        PermissionFinding.model_validate(values)


@pytest.mark.parametrize("source_status", [
    PermissionStatus.NOT_FOUND, PermissionStatus.UNAVAILABLE,
])
def test_empty_or_failed_search_cannot_become_confirmed_absence(source_status):
    values = make_finding(source_status).model_dump(round_trip=True)
    values.update(
        status=PermissionStatus.CONFIRMED_ABSENT,
        verified_for_scope=True,
        verified_on=ASSESSMENT_DATE,
        record_id="invented-document",
    )
    with pytest.raises(ValidationError):
        PermissionFinding.model_validate(values)


@pytest.mark.parametrize("status", [
    PermissionStatus.NOT_CHECKED, PermissionStatus.NOT_FOUND, PermissionStatus.UNAVAILABLE,
])
@pytest.mark.parametrize("changes", [
    {"verified_for_scope": True},
    {"verified_on": ASSESSMENT_DATE},
    {"record_id": "invented-document"},
])
def test_unconfirmed_finding_rejects_confirmation_fields(status, changes):
    values = make_finding(status).model_dump(round_trip=True)
    values.update(changes)
    with pytest.raises(ValidationError):
        PermissionFinding.model_validate(values)


def test_not_checked_rejects_evidence():
    with pytest.raises(ValidationError):
        PermissionFinding(
            subject_inn="7736207543",
            scope=SCOPE,
            status=PermissionStatus.NOT_CHECKED,
            explanation="Не проверено",
            evidence=make_finding(PermissionStatus.NOT_FOUND).evidence,
        )


def test_confirmation_for_another_date_is_rejected():
    with pytest.raises(ValidationError, match="Дата подтверждения"):
        make_check(
            assessment_date=date(2026, 10, 1),
            finding=make_finding(PermissionStatus.VALID),
        )


@pytest.mark.parametrize("changes", [
    {"subject_inn": "123456789012"}, {"scope": "Другой объект и другие работы"},
])
def test_confirmation_for_another_subject_or_scope_is_rejected(changes):
    with pytest.raises(ValidationError, match="другому ИНН или области"):
        make_check(finding=make_finding(PermissionStatus.VALID), **changes)


@pytest.mark.parametrize("value", ["false", "true", 0, 1])
def test_confirmation_flags_require_real_booleans(value):
    with pytest.raises(ValidationError):
        make_check(exceptions_reviewed=value)
    values = make_finding(PermissionStatus.VALID).model_dump(round_trip=True)
    values["verified_for_scope"] = value
    with pytest.raises(ValidationError):
        PermissionFinding.model_validate(values)


def test_critical_result_survives_json_round_trip():
    check = make_check(finding=make_finding(PermissionStatus.CONFIRMED_ABSENT))
    restored = RegulatoryCheck.model_validate_json(check.model_dump_json(round_trip=True))
    assert restored == check
    assert restored.is_blocking


@pytest.mark.parametrize("field,value", [
    ("outcome", "satisfied"), ("is_blocking", False), ("score", 100),
])
def test_caller_cannot_override_derived_outcome(field, value):
    with pytest.raises(ValidationError):
        make_check(**{field: value})