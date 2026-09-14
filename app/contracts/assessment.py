from datetime import date
from enum import StrEnum
from typing import Any, Iterable

from pydantic import (
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from app.contracts.decision import (
    ContractModel,
    Coverage,
    CoverageStatus,
    Evidence,
    build_coverage,
)


class SignalSeverity(StrEnum):
    """Смысловая сила сигнала внутри одного раздела."""

    POSITIVE = "positive"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class SectionStatus(StrEnum):
    """Итог раздела, но не общий индекс компании."""

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    ATTENTION = "attention"
    CRITICAL = "critical"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ConfidenceLevel(StrEnum):
    """Уверенность, обусловленная полнотой проверки."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class RecommendationAction(StrEnum):
    """Стандартизированное действие для пользователя."""

    CONTINUE = "continue"
    REQUEST_DOCUMENTS = "request_documents"
    MANUAL_REVIEW = "manual_review"
    LIMIT_ADVANCE = "limit_advance"
    REQUIRE_SECURITY = "require_security"
    STOP = "stop"


class RecommendationPriority(StrEnum):
    """Приоритет рекомендации."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    BLOCKING = "blocking"


def _normalize_references(
    values: tuple[str, ...],
    *,
    field_name: str,
) -> tuple[str, ...]:
    normalized = tuple(
        value.strip()
        for value in values
        if value.strip()
    )

    if len(normalized) != len(set(normalized)):
        raise ValueError(
            f"{field_name} не должны содержать дубли"
        )

    return normalized


def _ensure_unique_ids(
    values: Iterable[str],
    *,
    object_name: str,
) -> None:
    identifiers = tuple(values)

    if len(identifiers) != len(set(identifiers)):
        raise ValueError(
            f"{object_name} должны иметь уникальные id"
        )


class Fact(ContractModel):
    """
    Нормализованный факт.

    Факт обязательно ссылается минимум на одно Evidence и сам
    по себе не является положительным или отрицательным выводом.
    """

    fact_id: str = Field(
        min_length=1,
        max_length=200,
    )

    code: str = Field(
        min_length=1,
        max_length=200,
    )

    label: str = Field(
        min_length=1,
        max_length=500,
    )

    value: Any

    unit: str | None = Field(
        default=None,
        max_length=100,
    )

    evidence_ids: tuple[str, ...] = Field(
        min_length=1,
    )

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _normalize_references(
            values,
            field_name="evidence_ids",
        )


class Signal(ContractModel):
    """Интерпретация одного или нескольких фактов."""

    signal_id: str = Field(
        min_length=1,
        max_length=200,
    )

    code: str = Field(
        min_length=1,
        max_length=200,
    )

    severity: SignalSeverity

    title: str = Field(
        min_length=1,
        max_length=500,
    )

    explanation: str = Field(
        min_length=1,
        max_length=4000,
    )

    fact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    @field_validator(
        "fact_ids",
        "evidence_ids",
    )
    @classmethod
    def validate_references(
        cls,
        values: tuple[str, ...],
        info,
    ) -> tuple[str, ...]:
        return _normalize_references(
            values,
            field_name=info.field_name,
        )

    @model_validator(mode="after")
    def require_source_reference(
        self,
    ) -> "Signal":
        if not self.fact_ids and not self.evidence_ids:
            raise ValueError(
                "Signal должен ссылаться на Fact или Evidence"
            )

        return self


class Recommendation(ContractModel):
    """Понятное действие, предложенное пользователю."""

    recommendation_id: str = Field(
        min_length=1,
        max_length=200,
    )

    action: RecommendationAction
    priority: RecommendationPriority

    title: str = Field(
        min_length=1,
        max_length=500,
    )

    explanation: str = Field(
        min_length=1,
        max_length=4000,
    )

    signal_ids: tuple[str, ...] = Field(
        min_length=1,
    )

    @field_validator("signal_ids")
    @classmethod
    def validate_signal_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _normalize_references(
            values,
            field_name="signal_ids",
        )

    @model_validator(mode="after")
    def validate_blocking_action(
        self,
    ) -> "Recommendation":
        if (
            self.action == RecommendationAction.STOP
            and self.priority
            != RecommendationPriority.BLOCKING
        ):
            raise ValueError(
                "Рекомендация stop должна иметь priority=blocking"
            )

        if (
            self.priority
            == RecommendationPriority.BLOCKING
            and self.action != RecommendationAction.STOP
        ):
            raise ValueError(
                "priority=blocking допустим только для action=stop"
            )

        return self


class EngineVersion(ContractModel):
    """Версия правил, сформировавших вывод раздела."""

    engine_name: str = Field(
        min_length=1,
        max_length=200,
    )

    version: str = Field(
        min_length=1,
        max_length=100,
    )

    ruleset_date: date

    ruleset_hash: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )


def confidence_from_coverage(
    coverage: Coverage,
) -> ConfidenceLevel:
    """Определяет базовую уверенность только по coverage."""

    if coverage.status in {
        CoverageStatus.NOT_APPLICABLE,
        CoverageStatus.UNAVAILABLE,
    }:
        return ConfidenceLevel.NONE

    if coverage.status == CoverageStatus.COMPLETE:
        return ConfidenceLevel.HIGH

    if (
        coverage.ratio is not None
        and coverage.ratio >= 0.5
    ):
        return ConfidenceLevel.MEDIUM

    return ConfidenceLevel.LOW


class SectionAssessment(ContractModel):
    """
    Объяснимый вывод одного раздела.

    Это не общий рейтинг компании. Каждый вывод должен быть связан
    с Evidence, Facts, Signals и конкретными Recommendations.
    """

    section_code: str = Field(
        min_length=1,
        max_length=200,
    )

    applicable: bool
    status: SectionStatus

    headline: str = Field(
        min_length=1,
        max_length=500,
    )

    explanation: str = Field(
        min_length=1,
        max_length=4000,
    )

    evidence: tuple[Evidence, ...]
    facts: tuple[Fact, ...]
    signals: tuple[Signal, ...]
    recommendations: tuple[Recommendation, ...]

    coverage: Coverage
    data_date: date | None
    confidence: ConfidenceLevel
    engine_version: EngineVersion

    @computed_field
    @property
    def positive_signals(
        self,
    ) -> tuple[Signal, ...]:
        return tuple(
            signal
            for signal in self.signals
            if signal.severity
            == SignalSeverity.POSITIVE
        )

    @computed_field
    @property
    def risk_signals(
        self,
    ) -> tuple[Signal, ...]:
        return tuple(
            signal
            for signal in self.signals
            if signal.severity
            in {
                SignalSeverity.WARNING,
                SignalSeverity.CRITICAL,
            }
        )

    @model_validator(mode="after")
    def validate_assessment(
        self,
    ) -> "SectionAssessment":
        _ensure_unique_ids(
            (
                item.evidence_id
                for item in self.evidence
            ),
            object_name="Evidence",
        )

        _ensure_unique_ids(
            (
                item.fact_id
                for item in self.facts
            ),
            object_name="Fact",
        )

        _ensure_unique_ids(
            (
                item.signal_id
                for item in self.signals
            ),
            object_name="Signal",
        )

        _ensure_unique_ids(
            (
                item.recommendation_id
                for item in self.recommendations
            ),
            object_name="Recommendation",
        )

        evidence_ids = {
            item.evidence_id
            for item in self.evidence
        }

        fact_ids = {
            item.fact_id
            for item in self.facts
        }

        signal_ids = {
            item.signal_id
            for item in self.signals
        }

        for fact in self.facts:
            unknown = set(
                fact.evidence_ids
            ).difference(
                evidence_ids
            )

            if unknown:
                raise ValueError(
                    f"Fact {fact.fact_id} ссылается на "
                    f"неизвестные Evidence: {sorted(unknown)}"
                )

        for signal in self.signals:
            unknown_facts = set(
                signal.fact_ids
            ).difference(
                fact_ids
            )

            unknown_evidence = set(
                signal.evidence_ids
            ).difference(
                evidence_ids
            )

            if unknown_facts:
                raise ValueError(
                    f"Signal {signal.signal_id} ссылается на "
                    f"неизвестные Fact: {sorted(unknown_facts)}"
                )

            if unknown_evidence:
                raise ValueError(
                    f"Signal {signal.signal_id} ссылается на "
                    f"неизвестные Evidence: {sorted(unknown_evidence)}"
                )

        for recommendation in self.recommendations:
            unknown = set(
                recommendation.signal_ids
            ).difference(
                signal_ids
            )

            if unknown:
                raise ValueError(
                    "Recommendation "
                    f"{recommendation.recommendation_id} "
                    "ссылается на неизвестные Signal: "
                    f"{sorted(unknown)}"
                )

        expected_coverage = build_coverage(
            self.evidence
        )

        if self.coverage != expected_coverage:
            raise ValueError(
                "Coverage не соответствует переданным Evidence"
            )

        data_dates = tuple(
            item.data_date
            for item in self.evidence
            if item.data_date is not None
        )

        expected_data_date = (
            max(data_dates)
            if data_dates
            else None
        )

        if self.data_date != expected_data_date:
            raise ValueError(
                "data_date должна быть максимальной датой Evidence"
            )

        if not self.applicable:
            if self.status != SectionStatus.NOT_APPLICABLE:
                raise ValueError(
                    "Неприменимый раздел должен иметь "
                    "status=not_applicable"
                )

            if (
                self.coverage.status
                != CoverageStatus.NOT_APPLICABLE
            ):
                raise ValueError(
                    "Неприменимый раздел не должен иметь "
                    "применимые проверки"
                )

        if (
            self.applicable
            and self.status
            == SectionStatus.NOT_APPLICABLE
        ):
            raise ValueError(
                "Применимый раздел не может иметь "
                "status=not_applicable"
            )

        has_critical_signal = any(
            signal.severity
            == SignalSeverity.CRITICAL
            for signal in self.signals
        )

        if (
            has_critical_signal
            and self.status
            != SectionStatus.CRITICAL
        ):
            raise ValueError(
                "Critical Signal требует status=critical"
            )

        if (
            self.status == SectionStatus.CRITICAL
            and not has_critical_signal
        ):
            raise ValueError(
                "status=critical требует Critical Signal"
            )

        if self.status == SectionStatus.POSITIVE:
            has_risk_signal = any(
                signal.severity
                in {
                    SignalSeverity.WARNING,
                    SignalSeverity.CRITICAL,
                }
                for signal in self.signals
            )

            if has_risk_signal:
                raise ValueError(
                    "Positive-раздел не может содержать risk Signal"
                )

        if (
            self.confidence == ConfidenceLevel.HIGH
            and self.coverage.status
            != CoverageStatus.COMPLETE
        ):
            raise ValueError(
                "confidence=high требует полного Coverage"
            )

        if (
            self.coverage.status
            in {
                CoverageStatus.NOT_APPLICABLE,
                CoverageStatus.UNAVAILABLE,
            }
            and self.confidence
            != ConfidenceLevel.NONE
        ):
            raise ValueError(
                "Для недоступного или неприменимого Coverage "
                "confidence должен быть none"
            )

        return self


def build_section_assessment(
    *,
    section_code: str,
    applicable: bool,
    status: SectionStatus,
    headline: str,
    explanation: str,
    evidence: Iterable[Evidence],
    facts: Iterable[Fact],
    signals: Iterable[Signal],
    recommendations: Iterable[Recommendation],
    engine_version: EngineVersion,
) -> SectionAssessment:
    """Собирает раздел и рассчитывает Coverage/Confidence."""

    evidence_items = tuple(evidence)

    coverage = build_coverage(
        evidence_items
    )

    data_dates = tuple(
        item.data_date
        for item in evidence_items
        if item.data_date is not None
    )

    data_date = (
        max(data_dates)
        if data_dates
        else None
    )

    return SectionAssessment(
        section_code=section_code,
        applicable=applicable,
        status=status,
        headline=headline,
        explanation=explanation,
        evidence=evidence_items,
        facts=tuple(facts),
        signals=tuple(signals),
        recommendations=tuple(recommendations),
        coverage=coverage,
        data_date=data_date,
        confidence=confidence_from_coverage(
            coverage
        ),
        engine_version=engine_version,
    )