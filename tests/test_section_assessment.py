from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.contracts.assessment import (
    ConfidenceLevel,
    EngineVersion,
    Fact,
    Recommendation,
    RecommendationAction,
    RecommendationPriority,
    SectionAssessment,
    SectionStatus,
    Signal,
    SignalSeverity,
    build_section_assessment,
)
from app.contracts.decision import (
    CheckResultStatus,
    Coverage,
    Evidence,
    build_coverage,
)


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
    evidence_id="tax-offence-current",
    result=CheckResultStatus.FOUND,
    data_date=date(2026, 8, 1),
):
    reason = (
        "dataset_not_loaded"
        if result == CheckResultStatus.UNAVAILABLE
        else (
            "legal_entities_only"
            if result
            == CheckResultStatus.NOT_APPLICABLE
            else None
        )
    )

    if result in {
        CheckResultStatus.UNAVAILABLE,
        CheckResultStatus.NOT_APPLICABLE,
    }:
        data_date = None

    return Evidence(
        evidence_id=evidence_id,
        source_code="fns_tax_offence",
        dataset_code="fns_tax_offence",
        result=result,
        data_date=data_date,
        checked_at=CHECKED_AT,
        reason=reason,
        record_ids=(
            ("OFFENCE-1",)
            if result == CheckResultStatus.FOUND
            else ()
        ),
    )


def make_fact(
    *,
    fact_id="tax-offence-fine",
    evidence_ids=("tax-offence-current",),
):
    return Fact(
        fact_id=fact_id,
        code="tax_offence.total_fine",
        label="Сумма штрафов",
        value=Decimal("125.00"),
        unit="RUB",
        evidence_ids=evidence_ids,
    )


def make_signal(
    *,
    signal_id="tax-offence-found",
    severity=SignalSeverity.WARNING,
    fact_ids=("tax-offence-fine",),
    evidence_ids=(),
):
    return Signal(
        signal_id=signal_id,
        code="tax_offence.found",
        severity=severity,
        title="Найдены сведения о правонарушении",
        explanation=(
            "В актуальном наборе ФНС найдены сведения."
        ),
        fact_ids=fact_ids,
        evidence_ids=evidence_ids,
    )


def make_recommendation(
    *,
    recommendation_id="review-tax-offence",
    action=RecommendationAction.MANUAL_REVIEW,
    priority=RecommendationPriority.HIGH,
    signal_ids=("tax-offence-found",),
):
    return Recommendation(
        recommendation_id=recommendation_id,
        action=action,
        priority=priority,
        title="Проверьте обстоятельства",
        explanation=(
            "Запросите у контрагента пояснения и документы."
        ),
        signal_ids=signal_ids,
    )


def make_engine_version():
    return EngineVersion(
        engine_name="tax-section-engine",
        version="1.0.0",
        ruleset_date=date(2026, 9, 14),
        ruleset_hash="test-ruleset-hash",
    )


def make_warning_assessment():
    return build_section_assessment(
        section_code="tax_offence",
        applicable=True,
        status=SectionStatus.ATTENTION,
        headline="Найдены сведения ФНС",
        explanation=(
            "Сведения требуют дополнительной проверки."
        ),
        evidence=[
            make_evidence()
        ],
        facts=[
            make_fact()
        ],
        signals=[
            make_signal()
        ],
        recommendations=[
            make_recommendation()
        ],
        engine_version=make_engine_version(),
    )


def test_fact_requires_evidence_reference():
    with pytest.raises(
        ValidationError
    ):
        make_fact(
            evidence_ids=()
        )


def test_fact_rejects_duplicate_evidence_references():
    with pytest.raises(
        ValidationError,
        match="не должны содержать дубли",
    ):
        make_fact(
            evidence_ids=(
                "tax-offence-current",
                "tax-offence-current",
            )
        )


def test_signal_requires_fact_or_evidence_reference():
    with pytest.raises(
        ValidationError,
        match="должен ссылаться",
    ):
        make_signal(
            fact_ids=(),
            evidence_ids=(),
        )


@pytest.mark.parametrize(
    (
        "action",
        "priority",
    ),
    [
        (
            RecommendationAction.STOP,
            RecommendationPriority.HIGH,
        ),
        (
            RecommendationAction.MANUAL_REVIEW,
            RecommendationPriority.BLOCKING,
        ),
    ],
)
def test_blocking_recommendation_contract(
    action,
    priority,
):
    with pytest.raises(
        ValidationError
    ):
        make_recommendation(
            action=action,
            priority=priority,
        )


def test_build_section_assessment():
    assessment = make_warning_assessment()

    assert assessment.section_code == "tax_offence"
    assert assessment.status == SectionStatus.ATTENTION
    assert assessment.data_date == date(2026, 8, 1)

    assert (
        assessment.coverage.percent
        == Decimal("100.00")
    )

    assert (
        assessment.confidence
        == ConfidenceLevel.HIGH
    )

    assert len(assessment.risk_signals) == 1
    assert assessment.positive_signals == ()


def test_partial_coverage_reduces_confidence():
    assessment = build_section_assessment(
        section_code="tax",
        applicable=True,
        status=SectionStatus.UNKNOWN,
        headline="Проверка выполнена частично",
        explanation=(
            "Один из источников временно недоступен."
        ),
        evidence=[
            make_evidence(),
            make_evidence(
                evidence_id="tax-debt-current",
                result=CheckResultStatus.UNAVAILABLE,
            ),
        ],
        facts=[
            make_fact()
        ],
        signals=[
            make_signal(
                severity=SignalSeverity.UNKNOWN,
            )
        ],
        recommendations=[
            make_recommendation()
        ],
        engine_version=make_engine_version(),
    )

    assert (
        assessment.coverage.percent
        == Decimal("50.00")
    )

    assert (
        assessment.confidence
        == ConfidenceLevel.MEDIUM
    )


def test_not_applicable_section():
    evidence = make_evidence(
        result=CheckResultStatus.NOT_APPLICABLE,
    )

    assessment = build_section_assessment(
        section_code="tax_offence",
        applicable=False,
        status=SectionStatus.NOT_APPLICABLE,
        headline="Проверка не применяется",
        explanation=(
            "Набор не применяется к этому типу сущности."
        ),
        evidence=[
            evidence
        ],
        facts=[],
        signals=[
            Signal(
                signal_id="not-applicable",
                code="source.not_applicable",
                severity=SignalSeverity.INFO,
                title="Источник не применяется",
                explanation=(
                    "Это не является положительным результатом."
                ),
                evidence_ids=(
                    evidence.evidence_id,
                ),
            )
        ],
        recommendations=[],
        engine_version=make_engine_version(),
    )

    assert assessment.data_date is None

    assert (
        assessment.confidence
        == ConfidenceLevel.NONE
    )

    assert assessment.coverage.percent is None


def test_fact_cannot_reference_unknown_evidence():
    with pytest.raises(
        ValidationError,
        match="неизвестные Evidence",
    ):
        build_section_assessment(
            section_code="tax_offence",
            applicable=True,
            status=SectionStatus.ATTENTION,
            headline="Найдены сведения",
            explanation="Требуется проверка.",
            evidence=[
                make_evidence()
            ],
            facts=[
                make_fact(
                    evidence_ids=(
                        "unknown-evidence",
                    ),
                )
            ],
            signals=[],
            recommendations=[],
            engine_version=make_engine_version(),
        )


def test_signal_cannot_reference_unknown_fact():
    with pytest.raises(
        ValidationError,
        match="неизвестные Fact",
    ):
        build_section_assessment(
            section_code="tax_offence",
            applicable=True,
            status=SectionStatus.ATTENTION,
            headline="Найдены сведения",
            explanation="Требуется проверка.",
            evidence=[
                make_evidence()
            ],
            facts=[
                make_fact()
            ],
            signals=[
                make_signal(
                    fact_ids=(
                        "unknown-fact",
                    ),
                )
            ],
            recommendations=[],
            engine_version=make_engine_version(),
        )


def test_recommendation_cannot_reference_unknown_signal():
    with pytest.raises(
        ValidationError,
        match="неизвестные Signal",
    ):
        build_section_assessment(
            section_code="tax_offence",
            applicable=True,
            status=SectionStatus.ATTENTION,
            headline="Найдены сведения",
            explanation="Требуется проверка.",
            evidence=[
                make_evidence()
            ],
            facts=[
                make_fact()
            ],
            signals=[
                make_signal()
            ],
            recommendations=[
                make_recommendation(
                    signal_ids=(
                        "unknown-signal",
                    ),
                )
            ],
            engine_version=make_engine_version(),
        )


def test_assessment_rejects_duplicate_ids():
    with pytest.raises(
        ValidationError,
        match="уникальные id",
    ):
        build_section_assessment(
            section_code="tax_offence",
            applicable=True,
            status=SectionStatus.ATTENTION,
            headline="Найдены сведения",
            explanation="Требуется проверка.",
            evidence=[
                make_evidence(),
                make_evidence(),
            ],
            facts=[],
            signals=[],
            recommendations=[],
            engine_version=make_engine_version(),
        )


def test_critical_signal_requires_critical_section():
    with pytest.raises(
        ValidationError,
        match="Critical Signal требует",
    ):
        build_section_assessment(
            section_code="license",
            applicable=True,
            status=SectionStatus.ATTENTION,
            headline="Разрешение не подтверждено",
            explanation="Требуется проверка разрешения.",
            evidence=[
                make_evidence()
            ],
            facts=[
                make_fact()
            ],
            signals=[
                make_signal(
                    severity=SignalSeverity.CRITICAL,
                )
            ],
            recommendations=[
                make_recommendation(
                    action=RecommendationAction.STOP,
                    priority=RecommendationPriority.BLOCKING,
                )
            ],
            engine_version=make_engine_version(),
        )


def test_critical_section_requires_critical_signal():
    with pytest.raises(
        ValidationError,
        match="status=critical требует",
    ):
        build_section_assessment(
            section_code="license",
            applicable=True,
            status=SectionStatus.CRITICAL,
            headline="Критический вывод",
            explanation="Требуется проверка разрешения.",
            evidence=[
                make_evidence()
            ],
            facts=[
                make_fact()
            ],
            signals=[
                make_signal(
                    severity=SignalSeverity.WARNING,
                )
            ],
            recommendations=[
                make_recommendation()
            ],
            engine_version=make_engine_version(),
        )


def test_positive_section_rejects_risk_signal():
    with pytest.raises(
        ValidationError,
        match="Positive-раздел",
    ):
        build_section_assessment(
            section_code="tax",
            applicable=True,
            status=SectionStatus.POSITIVE,
            headline="Положительный результат",
            explanation="Проверка выполнена.",
            evidence=[
                make_evidence()
            ],
            facts=[
                make_fact()
            ],
            signals=[
                make_signal(
                    severity=SignalSeverity.WARNING,
                )
            ],
            recommendations=[],
            engine_version=make_engine_version(),
        )


def test_high_confidence_requires_complete_coverage():
    evidence = [
        make_evidence(),
        make_evidence(
            evidence_id="tax-debt-current",
            result=CheckResultStatus.UNAVAILABLE,
        ),
    ]

    with pytest.raises(
        ValidationError,
        match="confidence=high",
    ):
        SectionAssessment(
            section_code="tax",
            applicable=True,
            status=SectionStatus.UNKNOWN,
            headline="Проверка выполнена частично",
            explanation="Один источник недоступен.",
            evidence=tuple(evidence),
            facts=(),
            signals=(),
            recommendations=(),
            coverage=build_coverage(
                evidence
            ),
            data_date=date(2026, 8, 1),
            confidence=ConfidenceLevel.HIGH,
            engine_version=make_engine_version(),
        )


def test_builder_uses_latest_evidence_date():
    assessment = build_section_assessment(
        section_code="tax",
        applicable=True,
        status=SectionStatus.NEUTRAL,
        headline="Проверка завершена",
        explanation="Использованы два периода данных.",
        evidence=[
            make_evidence(
                evidence_id="older",
                data_date=date(2026, 7, 1),
            ),
            make_evidence(
                evidence_id="newer",
                data_date=date(2026, 8, 1),
            ),
        ],
        facts=[],
        signals=[],
        recommendations=[],
        engine_version=make_engine_version(),
    )

    assert (
        assessment.data_date
        == date(2026, 8, 1)
    )


def test_computed_signal_groups_are_serialized():
    assessment = make_warning_assessment()

    payload = assessment.model_dump(
        mode="json"
    )

    assert payload["positive_signals"] == []
    assert len(payload["risk_signals"]) == 1

    assert (
        payload["risk_signals"][0]["severity"]
        == "warning"
    )