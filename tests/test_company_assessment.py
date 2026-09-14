from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.contracts.assessment import (
    EngineVersion,
    SectionStatus,
    build_section_assessment,
)
from app.contracts.decision import CheckResultStatus, Evidence
from app.contracts.report import (
    CompanyAssessment,
    CounterpartyRole,
    DealContext,
)


CHECKED_AT = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def make_section(code="tax_offence", *, unavailable=False):
    evidence = Evidence(
        evidence_id=f"{code}-current",
        source_code="fns",
        dataset_code=code,
        result=(
            CheckResultStatus.UNAVAILABLE
            if unavailable else CheckResultStatus.NOT_FOUND
        ),
        data_date=None if unavailable else date(2026, 8, 1),
        checked_at=CHECKED_AT,
        reason="dataset_not_loaded" if unavailable else None,
    )
    return build_section_assessment(
        section_code=code,
        applicable=True,
        status=SectionStatus.UNKNOWN if unavailable else SectionStatus.NEUTRAL,
        headline="Тестовый раздел",
        explanation="Только тест структуры, не оценка реальной компании.",
        evidence=(evidence,),
        facts=(),
        signals=(),
        recommendations=(),
        engine_version=EngineVersion(
            engine_name="test-engine",
            version="1.0.0",
            ruleset_date=date(2026, 9, 14),
        ),
    )


def make_report(**changes):
    values = {
        "report_id": "report-test-1",
        "subject_inn": "7736207543",
        "generated_at": CHECKED_AT + timedelta(minutes=1),
        "expected_section_codes": ("tax_offence", "tax_debt"),
        "sections": (make_section(),),
    }
    values.update(changes)
    return CompanyAssessment(**values)


def test_empty_context_preserves_unknowns():
    context = DealContext()
    assert context.counterparty_role == CounterpartyRole.UNKNOWN
    assert context.amount is None
    assert context.currency is None
    assert context.advance_percent is None
    assert context.planned_start_date is None


def test_context_accepts_explicit_terms():
    context = DealContext(
        counterparty_role="contractor",
        subject="  Строительные работы  ",
        activity_description="Монтаж оборудования",
        territory="Свердловская область",
        amount="1250000.10",
        currency="RUB",
        advance_percent="30",
        planned_start_date=date(2026, 10, 1),
        planned_end_date=date(2026, 11, 1),
    )
    assert context.subject == "Строительные работы"
    assert context.amount == Decimal("1250000.10")
    assert context.advance_percent == Decimal("30")
    assert context.counterparty_role == CounterpartyRole.CONTRACTOR


@pytest.mark.parametrize("amount", ["0", 0, Decimal("0")])
def test_zero_amount_is_not_unknown(amount):
    assert DealContext(amount=amount, currency="RUB").amount == Decimal("0")


@pytest.mark.parametrize("value", ["0", "100"])
def test_advance_boundaries(value):
    assert DealContext(advance_percent=value).advance_percent == Decimal(value)


@pytest.mark.parametrize("values", [
    {"amount": "100"},
    {"currency": "RUB"},
    {"amount": "-1", "currency": "RUB"},
    {"amount": "NaN", "currency": "RUB"},
    {"amount": "Infinity", "currency": "RUB"},
    {"amount": 0.1, "currency": "RUB"},
    {"amount": True, "currency": "RUB"},
    {"amount": "100", "currency": "руб"},
    {"advance_percent": "-1"},
    {"advance_percent": "101"},
    {"advance_percent": "NaN"},
    {"advance_percent": False},
    {"subject": "   "},
    {"counterparty_role": "invented"},
    {"license_required": True},
    {
        "planned_start_date": date(2026, 11, 1),
        "planned_end_date": date(2026, 10, 1),
    },
])
def test_invalid_context_is_rejected(values):
    with pytest.raises(ValidationError):
        DealContext(**values)


def test_same_day_deal_is_allowed():
    value = date(2026, 10, 1)
    assert DealContext(
        planned_start_date=value, planned_end_date=value
    ).planned_end_date == value


def test_report_identifies_missing_sections():
    report = make_report()
    assert report.missing_section_codes == ("tax_debt",)
    assert report.deal_context is None
    assert report.sections[0].coverage.percent == Decimal("100.00")


def test_empty_report_does_not_hide_missing_sections():
    report = make_report(sections=())
    assert report.missing_section_codes == ("tax_offence", "tax_debt")


def test_unavailable_section_is_present_but_not_successful():
    report = make_report(
        sections=(make_section(), make_section("tax_debt", unavailable=True))
    )
    assert report.missing_section_codes == ()
    assert report.sections[1].status == SectionStatus.UNKNOWN
    assert report.sections[1].coverage.unavailable_checks == 1


def test_expected_codes_are_trimmed():
    report = make_report(expected_section_codes=(" tax_offence ", "tax_debt"))
    assert report.expected_section_codes == ("tax_offence", "tax_debt")


@pytest.mark.parametrize("codes", [
    (), ("",), ("   ",), ("x" * 201,),
    ("tax_offence", " tax_offence "),
])
def test_invalid_expected_codes_are_rejected(codes):
    with pytest.raises(ValidationError):
        make_report(expected_section_codes=codes)


def test_duplicate_sections_are_rejected():
    with pytest.raises(ValidationError, match="не должны повторяться"):
        make_report(sections=(make_section(), make_section()))


def test_unplanned_section_is_rejected():
    with pytest.raises(ValidationError, match="отсутствуют в плане"):
        make_report(sections=(make_section("licenses"),))


@pytest.mark.parametrize("inn", [
    "", "123", "12345678901", "abcdefghij", "１２３４５６７８９０", 7736207543,
])
def test_invalid_inn_format_is_rejected(inn):
    with pytest.raises(ValidationError):
        make_report(subject_inn=inn)


@pytest.mark.parametrize("inn", ["7736207543", "123456789012"])
def test_company_and_ip_inn_formats_are_supported(inn):
    # Здесь проверяется только формат, не контрольные цифры и не реестр.
    assert make_report(subject_inn=inn).subject_inn == inn


def test_generated_at_requires_timezone():
    with pytest.raises(ValidationError, match="часовой пояс"):
        make_report(generated_at=datetime(2026, 9, 14, 12, 1))


def test_evidence_cannot_be_checked_after_report():
    with pytest.raises(ValidationError, match="позже отчёта"):
        make_report(generated_at=CHECKED_AT - timedelta(seconds=1))


def test_different_timezones_are_compared_as_instants():
    local_time = CHECKED_AT.astimezone(timezone(timedelta(hours=5)))
    assert make_report(generated_at=local_time).generated_at == CHECKED_AT


def test_report_round_trip_preserves_context_and_nested_sections():
    report = make_report(deal_context=DealContext(amount="100.10", currency="RUB"))
    restored = CompanyAssessment.model_validate_json(
        report.model_dump_json(round_trip=True)
    )
    assert restored == report
    assert restored.missing_section_codes == ("tax_debt",)
    assert restored.deal_context.amount == Decimal("100.10")


def test_report_exports_missing_sections_without_a_global_score():
    payload = make_report().model_dump(mode="json")
    assert payload["missing_section_codes"] == ["tax_debt"]
    assert "score" not in payload
    assert "status" not in payload
    with pytest.raises(ValidationError):
        make_report(score=100)