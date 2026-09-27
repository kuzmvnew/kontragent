"""Deterministic Russian-language compiler for the ordinary public product.

The compiler is intentionally closed: a code without an approved template is
not echoed to the user.  Operational references stay on the input side for
validation and traceability and are never copied into public text.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.contracts.risk_v3 import (
    Applicability,
    AssessmentStatus,
    Execution,
    Freshness as RiskFreshness,
    Observation,
    OverallRiskResult,
    ResolutionState,
    RiskAssessmentV3,
    ScopeCompleteness,
    TemporalKind,
)


PUBLIC_NEXT_INDEX_ENABLED = False
SEMANTIC_TEXT_RULESET_VERSION = "public-semantic-ru-v1"


class SemanticCompilerError(ValueError):
    """The input cannot support a safe public statement."""


class PublicCategory(StrEnum):
    REGISTRATION = "Регистрационный статус"
    BANKRUPTCY = "Банкротство"
    ENFORCEMENT = "Исполнительные производства"
    TAX = "Налоги"
    FINANCE = "Финансовые показатели"
    COURTS = "Судебные дела"
    REGULATORY = "Регуляторные сведения"
    MANAGEMENT = "Руководство"
    LICENCE = "Лицензии и допуски"
    COVERAGE = "Полнота проверки"
    OTHER = "Прочие сведения"


class PublicSeverity(StrEnum):
    INFORMATION = "Сведения"
    ATTENTION = "Требует внимания"
    SIGNIFICANT = "Значимый фактор"
    CRITICAL = "Критический фактор"


class CompilerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MeaningInput(CompilerModel):
    meaning_id: str = Field(min_length=1, max_length=200)
    factor_code: str = Field(min_length=1, max_length=120)
    factor_refs: tuple[str, ...] = ()
    fact_refs: tuple[str, ...] = ()
    metric_refs: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = Field(min_length=1)
    source_dates: tuple[date, ...] = ()
    current_state: str = Field(min_length=1, max_length=120)
    previous_state: str | None = Field(default=None, max_length=240)
    change: str | None = Field(default=None, max_length=500)
    trend: str | None = Field(default=None, max_length=160)
    frequency: str | None = Field(default=None, max_length=160)
    recency: str | None = Field(default=None, max_length=160)
    duration: str | None = Field(default=None, max_length=160)
    materiality: str | None = Field(default=None, max_length=500)
    counter_evidence: tuple[str, ...] = ()
    confidence: float = Field(default=1.0, ge=0, le=1)
    recommendation_code: str | None = Field(default=None, max_length=120)
    ruleset_version: str = Field(min_length=1, max_length=120)
    denominator_required: bool = False
    denominator_present: bool = False
    causality_supported: bool = False

    @model_validator(mode="after")
    def validate_claim_support(self) -> "MeaningInput":
        if not (self.factor_refs or self.fact_refs or self.metric_refs or self.event_refs):
            raise ValueError("public meaning requires a traceable origin")
        if self.denominator_required and not self.denominator_present:
            raise ValueError("material public meaning requires a denominator")
        return self


class CompiledMeaning(CompilerModel):
    meaning_id: str
    category: str
    severity: str
    headline: str
    short_explanation: str
    full_explanation: str
    client_meaning: str
    what_it_does_not_mean: str
    recommendation_effect: str | None = None
    current_state: str
    previous_state: str | None = None
    change: str | None = None
    trend: str | None = None
    frequency: str | None = None
    recency: str | None = None
    duration: str | None = None
    materiality: str | None = None
    counter_evidence: tuple[str, ...] = ()
    confidence: float
    source_dates: tuple[date, ...] = ()


class CompiledLimitation(CompilerModel):
    headline: str
    short_explanation: str
    effect_on_conclusion: str
    what_remains_unknown: str


class CompiledRecommendation(CompilerModel):
    action: str
    rationale: str
    effect: str


class CompiledSourceStatus(CompilerModel):
    label: str
    explanation: str
    visual_state: str = Field(pattern=r"^(positive|neutral|attention|negative)$")


class AggregateCleanProof(CompilerModel):
    """Internal trace for the permission to publish a clean aggregate result."""

    proven: bool
    reason_codes: tuple[str, ...]
    resolved_check_refs: tuple[str, ...] = ()
    coverage_calculated_at: str | None = None
    mandatory_gate_evaluated_at: str | None = None
    ruleset_version: str | None = None
    policy_versions: tuple[str, ...] = ()


def aggregate_clean_conclusion_proof(
    risk: Mapping[str, Any],
) -> AggregateCleanProof:
    """Prove a clean aggregate conclusion from one canonical Risk v3 payload.

    Persisted rows contain the canonical assessment in ``result_payload``.  A
    direct RiskAssessmentV3-shaped mapping is accepted for deterministic use by
    the compiler itself.  Missing, empty, legacy, or malformed evidence never
    receives compatibility defaults.
    """

    if not isinstance(risk, Mapping):
        return AggregateCleanProof(
            proven=False,
            reason_codes=("RISK_V3_CONTRACT_MISSING",),
        )
    persisted_payload = risk.get("result_payload")
    payload = persisted_payload if isinstance(persisted_payload, Mapping) else risk
    try:
        assessment = RiskAssessmentV3.model_validate(payload)
    except (ValidationError, TypeError, ValueError):
        return AggregateCleanProof(
            proven=False,
            reason_codes=("RISK_V3_CONTRACT_INVALID",),
        )

    coverage = assessment.coverage_snapshot
    gate = assessment.mandatory_gate
    trace = {
        "resolved_check_refs": tuple(
            sorted(item.check_ref for item in assessment.resolved_checks)
        ),
        "coverage_calculated_at": coverage.calculated_at.isoformat(),
        "mandatory_gate_evaluated_at": gate.evaluated_at.isoformat(),
        "ruleset_version": assessment.ruleset_version,
        "policy_versions": (
            assessment.coverage_policy_version,
            assessment.applicability_policy_version,
            assessment.source_resolution_policy_version,
            assessment.freshness_policy_version,
            gate.policy_version,
        ),
    }
    reasons: list[str] = []

    if assessment.status != AssessmentStatus.CALCULATED:
        reasons.append("ASSESSMENT_NOT_CALCULATED")
    if assessment.factors:
        reasons.append("FACTORS_PRESENT")
    if assessment.overall_result != OverallRiskResult.NO_ADVERSE_FACTORS_AFTER_MANDATORY_GATE:
        reasons.append("OVERALL_RESULT_NOT_CLEAN")
    if any(item.blocks_positive_conclusion for item in assessment.limitations):
        reasons.append("BLOCKING_LIMITATION")

    applicable = set(coverage.applicable_codes)
    resolved = set(coverage.resolved_codes)
    mandatory = set(coverage.mandatory_applicable_codes)
    mandatory_resolved = set(coverage.mandatory_resolved_codes)
    if coverage.denominator <= 0 or coverage.numerator != coverage.denominator:
        reasons.append("COVERAGE_NOT_COMPLETE")
    if not applicable or resolved != applicable:
        reasons.append("APPLICABLE_CHECKS_NOT_RESOLVED")
    if (
        coverage.unresolved_codes
        or coverage.partial_codes
        or coverage.applicability_unknown_codes
    ):
        reasons.append("COVERAGE_HAS_UNRESOLVED_AXES")
    if (
        coverage.mandatory_denominator <= 0
        or coverage.mandatory_numerator != coverage.mandatory_denominator
        or not mandatory
        or mandatory_resolved != mandatory
    ):
        reasons.append("MANDATORY_COVERAGE_NOT_COMPLETE")
    if coverage.coverage_policy_version != assessment.coverage_policy_version:
        reasons.append("COVERAGE_POLICY_MISMATCH")

    if (
        gate.allowed is not True
        or gate.blocking_checks
        or set(gate.mandatory_applicable_codes) != mandatory
        or set(gate.resolved_codes) != mandatory
    ):
        reasons.append("MANDATORY_GATE_NOT_PROVEN")
    if gate.policy_version != assessment.applicability_policy_version:
        reasons.append("MANDATORY_GATE_POLICY_MISMATCH")

    check_codes = [item.capability_code for item in assessment.resolved_checks]
    if len(check_codes) != len(set(check_codes)):
        reasons.append("DUPLICATE_CHECK_CAPABILITY")
    check_applicable = {
        item.capability_code
        for item in assessment.resolved_checks
        if item.applicability == Applicability.APPLICABLE
    }
    if check_applicable != applicable:
        reasons.append("COVERAGE_CHECK_SET_MISMATCH")
    candidate_refs = {item.candidate_ref for item in assessment.evidence_snapshot}
    evidence_refs = {
        ref for item in assessment.evidence_snapshot for ref in item.evidence_refs
    }
    source_refs = {item.source_code for item in assessment.evidence_snapshot}
    if any(
        item.company_id != assessment.company_id
        for item in assessment.evidence_snapshot
    ):
        reasons.append("EVIDENCE_SUBJECT_MISMATCH")

    for check in assessment.resolved_checks:
        if check.company_id != assessment.company_id:
            reasons.append("CHECK_SUBJECT_MISMATCH")
        if check.applicability == Applicability.APPLICABILITY_UNKNOWN:
            reasons.append("APPLICABILITY_NOT_PROVEN")
            continue
        if check.applicability == Applicability.NOT_APPLICABLE:
            if (
                check.resolution_state != ResolutionState.RESOLVED
                or check.applicability_decision is None
            ):
                reasons.append("NOT_APPLICABLE_PROVENANCE_MISSING")
            continue
        if check.capability_code not in applicable:
            continue
        if (
            check.resolution_state != ResolutionState.RESOLVED
            or check.execution != Execution.CHECKED
            or check.observation not in {Observation.FOUND, Observation.NOT_FOUND}
            or check.scope != ScopeCompleteness.COMPLETE
            or not check.selected_evidence_refs
            or not check.source_refs
            or not check.candidate_refs
        ):
            reasons.append("RELIED_CHECK_NOT_RESOLVED")
        if (
            not set(check.candidate_refs) <= candidate_refs
            or not set(check.selected_evidence_refs) <= evidence_refs
            or not set(check.source_refs) <= source_refs
        ):
            reasons.append("RELIED_EVIDENCE_NOT_TRACEABLE")
        if (
            check.temporal_kind == TemporalKind.CURRENT_STATE
            and check.freshness != RiskFreshness.CURRENT
        ):
            reasons.append("RELIED_CHECK_NOT_CURRENT")
        if (
            check.observation == Observation.NOT_FOUND
            and not check.negative_closure_proven
        ):
            reasons.append("NEGATIVE_CLOSURE_NOT_PROVEN")
        if check.resolution_policy_version != assessment.source_resolution_policy_version:
            reasons.append("RESOLUTION_POLICY_MISMATCH")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return AggregateCleanProof(
        proven=not unique_reasons,
        reason_codes=unique_reasons,
        **trace,
    )


class _FactorTemplate(CompilerModel):
    category: PublicCategory
    severity: PublicSeverity
    headline: str
    short_explanation: str
    full_explanation: str
    client_meaning: str
    what_it_does_not_mean: str
    recommendation_effect: str | None = None


def _factor(
    category: PublicCategory,
    severity: PublicSeverity,
    headline: str,
    short: str,
    meaning: str,
    does_not_mean: str,
    effect: str | None = None,
) -> _FactorTemplate:
    return _FactorTemplate(
        category=category,
        severity=severity,
        headline=headline,
        short_explanation=short,
        full_explanation=short,
        client_meaning=meaning,
        what_it_does_not_mean=does_not_mean,
        recommendation_effect=effect,
    )


FACTOR_TEMPLATES: dict[str, _FactorTemplate] = {
    "REGISTRATION_ADVERSE_STATUS": _factor(
        PublicCategory.REGISTRATION, PublicSeverity.CRITICAL,
        "Регистрационный статус требует отдельной проверки.",
        "Официальные сведения содержат статус, который может ограничивать обычную деятельность юридического лица.",
        "До сделки важно подтвердить актуальный статус и полномочия компании.",
        "Сам по себе статус не доказывает недобросовестность участников или руководителей.",
        "Проверка статуса снижает риск заключения сделки с лицом, чьи полномочия ограничены.",
    ),
    "BANKRUPTCY_ADVERSE_EVENT": _factor(
        PublicCategory.BANKRUPTCY, PublicSeverity.CRITICAL,
        "Найдено существенное событие, связанное с банкротством.",
        "В подтверждённых источниках есть событие о процедуре банкротства или её существенной стадии.",
        "Событие необходимо учесть при оценке возможности исполнения обязательств.",
        "Историческое событие не всегда означает, что процедура продолжается сейчас.",
        "Уточнение стадии процедуры помогает выбрать допустимый формат сделки и оплаты.",
    ),
    "FSSP_ACTIVE_ENFORCEMENT": _factor(
        PublicCategory.ENFORCEMENT, PublicSeverity.SIGNIFICANT,
        "Найдены действующие исполнительные производства.",
        "Источник содержит сведения о текущих исполнительных производствах в отношении компании.",
        "Фактор следует учесть при согласовании суммы, срока и обеспечения обязательств.",
        "Наличие производства не описывает причины долга и не определяет исход исполнения.",
        "Проверка состава производств помогает соотнести обязательства с условиями сделки.",
    ),
    "TAX_DEBT_PRESENT": _factor(
        PublicCategory.TAX, PublicSeverity.SIGNIFICANT,
        "По данным ФНС указана налоговая задолженность.",
        "В официальном наборе данных для ИНН указана ненулевая сумма задолженности.",
        "Действующую задолженность следует учесть при согласовании условий оплаты и обеспечений.",
        "Запись не объясняет причину задолженности и не доказывает невозможность расчёта по сделке.",
        "Учёт задолженности помогает выбрать условия оплаты, соответствующие подтверждённому фактору.",
    ),
    "TAX_OFFENCE_PRESENT": _factor(
        PublicCategory.TAX, PublicSeverity.ATTENTION,
        "Найдены сведения о налоговом правонарушении.",
        "В официальном наборе данных есть запись о налоговом правонарушении и применённой санкции.",
        "Запись следует учитывать как подтверждённый исторический налоговый факт.",
        "Одна запись не характеризует всю текущую налоговую дисциплину компании.",
        "Уточнение периода и предмета нарушения помогает оценить его значение для сделки.",
    ),
    "FINANCE_ADVERSE_RESULT": _factor(
        PublicCategory.FINANCE, PublicSeverity.ATTENTION,
        "Финансовый результат за опубликованный период требует внимания.",
        "В официальной отчётности за указанный период отражён неблагоприятный финансовый результат.",
        "Показатель следует сопоставить с периодом, масштабом деятельности и динамикой отчётности.",
        "Отдельный показатель не доказывает неплатёжеспособность и не описывает текущее состояние без более свежих данных.",
        "Сопоставление периодов помогает оценить устойчивость показателя перед сделкой.",
    ),
    "ARBITRATION_ADVERSE_CASES": _factor(
        PublicCategory.COURTS, PublicSeverity.ATTENTION,
        "Найдены арбитражные дела, требующие изучения.",
        "В доступных данных есть арбитражные дела с релевантной для оценки ролью компании.",
        "Важно изучить предмет, роль компании, суммы и текущую стадию конкретных дел.",
        "Количество дел без контекста не доказывает финансовые трудности или недобросовестность.",
        "Изучение конкретных дел позволяет оценить их связь с планируемой сделкой.",
    ),
    "CBR_WARNING_EXACT_MATCH": _factor(
        PublicCategory.REGULATORY, PublicSeverity.SIGNIFICANT,
        "Найдено точное совпадение в предупредительном списке Банка России.",
        "Идентификатор компании совпал с записью официального предупредительного списка.",
        "До взаимодействия необходимо изучить основание и актуальный статус записи.",
        "Совпадение не заменяет правовую оценку конкретной операции.",
        "Проверка основания записи помогает определить допустимость взаимодействия.",
    ),
    "CBR_ZSK_HIGH_RISK": _factor(
        PublicCategory.REGULATORY, PublicSeverity.SIGNIFICANT,
        "Найден подтверждённый регуляторный признак повышенного риска.",
        "Официальный источник содержит актуальный признак, относящийся к компании.",
        "Признак следует учитывать при проверке платежей и деловой цели операции.",
        "Признак не является судебным решением и не доказывает совершение нарушения.",
        "Дополнительная проверка операции помогает учесть регуляторное ограничение.",
    ),
    "CURRENT_MANAGER_DISQUALIFIED": _factor(
        PublicCategory.MANAGEMENT, PublicSeverity.CRITICAL,
        "Найдено ограничение в отношении действующего руководителя.",
        "Сведения о текущем руководителе совпали с официальной записью о дисквалификации.",
        "До сделки необходимо подтвердить полномочия подписанта и актуальный состав руководства.",
        "Запись относится к полномочиям лица и сама по себе не описывает все обязательства компании.",
        "Проверка полномочий снижает риск оспаривания подписанных документов.",
    ),
    "REQUIRED_LICENCE_SRO_ADVERSE": _factor(
        PublicCategory.LICENCE, PublicSeverity.SIGNIFICANT,
        "Не подтверждён требуемый разрешительный статус.",
        "Для применимой деятельности проверка выявила отсутствие или ограничение обязательной лицензии либо допуска.",
        "До заказа работ необходимо подтвердить право компании выполнять соответствующий вид деятельности.",
        "Фактор относится только к деятельности, для которой разрешение обязательно.",
        "Проверка разрешения помогает исключить выполнение работ без необходимого статуса.",
    ),
    "BANKINFORM_ACTIVE_SUSPENSION": _factor(
        PublicCategory.TAX, PublicSeverity.SIGNIFICANT,
        "Найдено действующее решение о приостановлении операций по счетам.",
        "Официальная проверка содержит актуальное решение, относящееся к компании.",
        "Ограничение следует учитывать при выборе порядка и срока оплаты.",
        "Запись не раскрывает все счета и не доказывает общую неплатёжеспособность.",
        "Уточнение статуса решения помогает выбрать исполнимый порядок расчётов.",
    ),
    "INDUSTRY_SPECIFIC_ADVERSE": _factor(
        PublicCategory.REGULATORY, PublicSeverity.SIGNIFICANT,
        "Отраслевая проверка выявила значимое ограничение.",
        "Применимая к деятельности компании официальная проверка содержит неблагоприятный факт.",
        "Ограничение необходимо сопоставить с предметом планируемой сделки.",
        "Фактор нельзя распространять на виды деятельности, к которым проверка не относится.",
        "Проверка применимости помогает определить влияние ограничения на конкретную сделку.",
    ),
}


LIMITATION_TEMPLATES: dict[str, CompiledLimitation] = {
    "AGGREGATE_CLEAN_PROOF_NOT_PROVEN": CompiledLimitation(
        headline="Проверка выполнена не полностью.",
        short_explanation="Недостаточно подтверждённых результатов, чтобы сделать общий вывод об отсутствии факторов внимания.",
        effect_on_conclusion="Отсутствие выявленных факторов не считается подтверждением их отсутствия.",
        what_remains_unknown="Остаётся неизвестно, завершены ли все применимые обязательные проверки.",
    ),
    "APPLICABILITY_UNKNOWN": CompiledLimitation(
        headline="Применимость проверки не определена.",
        short_explanation="Недостаточно подтверждённых данных, чтобы решить, относится ли эта проверка к деятельности компании.",
        effect_on_conclusion="Этот блок не используется для положительного вывода.",
        what_remains_unknown="Остаётся неизвестно, обязательна ли эта проверка для компании.",
    ),
    "STALE_CURRENT_STATE": CompiledLimitation(
        headline="Данные устарели.",
        short_explanation="Последний официальный набор описывает прошлое состояние и не подтверждает текущее.",
        effect_on_conclusion="Исторические сведения не используются как подтверждение текущего благополучного состояния.",
        what_remains_unknown="Текущее состояние после даты источника не подтверждено.",
    ),
    "SOURCE_UNAVAILABLE": CompiledLimitation(
        headline="Источник временно недоступен.",
        short_explanation="Проверку не удалось завершить из-за недоступности источника.",
        effect_on_conclusion="Отсутствие результата не считается отсутствием факта.",
        what_remains_unknown="Результат этой проверки остаётся неизвестным.",
    ),
    "NOT_CHECKED": CompiledLimitation(
        headline="Проверка ещё не выполнена.",
        short_explanation="Для этого блока пока нет завершённого результата.",
        effect_on_conclusion="Блок не используется для положительного вывода.",
        what_remains_unknown="Наличие или отсутствие соответствующего факта не подтверждено.",
    ),
    "TIMEOUT": CompiledLimitation(
        headline="Проверка не завершилась вовремя.",
        short_explanation="Источник не вернул полный результат в установленное время.",
        effect_on_conclusion="Неполный результат не используется как подтверждение отсутствия факта.",
        what_remains_unknown="Результат проверки остаётся неизвестным.",
    ),
    "PARSING_ERROR": CompiledLimitation(
        headline="Данные источника не удалось обработать.",
        short_explanation="Формат ответа источника не позволил получить подтверждённый результат.",
        effect_on_conclusion="Необработанные данные не используются в выводе.",
        what_remains_unknown="Результат проверки остаётся неизвестным.",
    ),
    "NEGATIVE_CLOSURE_NOT_PROVEN": CompiledLimitation(
        headline="Отсутствие сведений не подтверждено.",
        short_explanation="Охват источника не позволяет доказать, что отсутствие совпадения означает отсутствие факта.",
        effect_on_conclusion="Этот результат не используется как положительное подтверждение.",
        what_remains_unknown="Наличие факта вне доступного охвата источника остаётся неизвестным.",
    ),
    "CONFLICTING_EVIDENCE": CompiledLimitation(
        headline="Источники содержат противоречивые сведения.",
        short_explanation="Сохранённые результаты нельзя свести к одному подтверждённому состоянию.",
        effect_on_conclusion="Противоречивый блок исключён из однозначного вывода.",
        what_remains_unknown="Актуальное состояние требует повторной проверки.",
    ),
    "PARTIAL_SCOPE": CompiledLimitation(
        headline="Проверка охватывает не все необходимые данные.",
        short_explanation="Полученный результат относится только к части ожидаемого набора сведений.",
        effect_on_conclusion="Частичный охват не подтверждает отсутствие факта во всём проверяемом объёме.",
        what_remains_unknown="Сведения за пределами доступного охвата не проверены.",
    ),
    "SCOPE_UNKNOWN": CompiledLimitation(
        headline="Охват проверки не определён.",
        short_explanation="Нельзя подтвердить, что источник охватывает весь необходимый объём сведений.",
        effect_on_conclusion="Результат не используется для положительного вывода.",
        what_remains_unknown="Полнота проверенных сведений остаётся неизвестной.",
    ),
    "FRESHNESS_UNKNOWN": CompiledLimitation(
        headline="Актуальность данных не подтверждена.",
        short_explanation="У источника нет достаточной даты, чтобы подтвердить текущее состояние.",
        effect_on_conclusion="Результат без подтверждённой актуальности не используется как текущий положительный факт.",
        what_remains_unknown="Неизвестно, изменилось ли состояние после публикации данных.",
    ),
    "OBSERVATION_UNKNOWN": CompiledLimitation(
        headline="Результат проверки не определён.",
        short_explanation="Полученных сведений недостаточно для подтверждения наличия или отсутствия факта.",
        effect_on_conclusion="Неопределённый результат исключён из положительного вывода.",
        what_remains_unknown="Наличие или отсутствие факта не подтверждено.",
    ),
    "EMPTY_MANDATORY_SET": CompiledLimitation(
        headline="Обязательный состав проверок не определён.",
        short_explanation="Методика не смогла сформировать подтверждённый набор обязательных проверок.",
        effect_on_conclusion="Положительный вывод не формируется.",
        what_remains_unknown="Неизвестно, выполнены ли все необходимые проверки.",
    ),
}

GENERIC_LIMITATION = CompiledLimitation(
    headline="Проверка имеет ограничение.",
    short_explanation="Недостаточно подтверждённых данных для однозначного результата по этому блоку.",
    effect_on_conclusion="Блок не используется для положительного вывода.",
    what_remains_unknown="Наличие или отсутствие соответствующего факта не подтверждено.",
)


RECOMMENDATION_TEMPLATES: dict[str, CompiledRecommendation] = {
    "VERIFY_REGISTRATION_STATUS": CompiledRecommendation(
        action="До сделки подтвердите актуальный регистрационный статус и полномочия подписанта.",
        rationale="Официальные сведения содержат ограничивающий регистрационный статус.",
        effect="Это помогает снизить риск заключения неисполнимой или оспоримой сделки.",
    ),
    "REVIEW_BANKRUPTCY_EVENT": CompiledRecommendation(
        action="Уточните текущую стадию процедуры и ограничения на совершение сделки.",
        rationale="Найдено подтверждённое событие, связанное с банкротством.",
        effect="Это позволяет согласовать допустимые условия сделки и оплаты.",
    ),
    "REVIEW_ENFORCEMENT": CompiledRecommendation(
        action="Учтите действующие исполнительные производства при согласовании суммы, срока и обеспечения обязательств.",
        rationale="Источник содержит текущие сведения об исполнительных производствах.",
        effect="Условия сделки будут учитывать подтверждённую нагрузку на компанию.",
    ),
    "REQUEST_TAX_DEBT_CLEARANCE": CompiledRecommendation(
        action="Учтите действующую налоговую задолженность при согласовании условий оплаты.",
        rationale="По данным ФНС для компании указана ненулевая задолженность.",
        effect="Это помогает выбрать порядок расчётов, соответствующий подтверждённому фактору.",
    ),
    "REVIEW_TAX_OFFENCE": CompiledRecommendation(
        action="Сопоставьте период и предмет налогового нарушения с условиями планируемой сделки.",
        rationale="В официальном наборе есть подтверждённая запись о нарушении.",
        effect="Контекст записи позволит точнее оценить её значение для сделки.",
    ),
    "REVIEW_FINANCIALS": CompiledRecommendation(
        action="Сопоставьте финансовый результат с более свежими периодами и масштабом деятельности.",
        rationale="Опубликованный показатель за отдельный период требует контекста.",
        effect="Сравнение периодов помогает не переносить исторический показатель на текущее состояние без оснований.",
    ),
    "REVIEW_ARBITRATION_CASES": CompiledRecommendation(
        action="Изучите роль компании, предмет, суммы и текущую стадию релевантных арбитражных дел.",
        rationale="Найдены дела, значение которых зависит от конкретного контекста.",
        effect="Это позволяет оценить связь судебных споров с планируемой сделкой.",
    ),
    "REVIEW_CBR_WARNING": CompiledRecommendation(
        action="До взаимодействия изучите основание и актуальный статус записи Банка России.",
        rationale="Найдено точное совпадение с официальным предупредительным списком.",
        effect="Проверка поможет определить допустимость планируемой операции.",
    ),
    "REVIEW_CBR_ZSK": CompiledRecommendation(
        action="Проверьте деловую цель и платёжный контекст планируемой операции.",
        rationale="Найден актуальный регуляторный признак повышенного риска.",
        effect="Дополнительная проверка помогает учесть регуляторное ограничение.",
    ),
    "VERIFY_CURRENT_MANAGEMENT": CompiledRecommendation(
        action="Подтвердите актуальный состав руководства и полномочия подписанта.",
        rationale="В отношении действующего руководителя найдено ограничение.",
        effect="Это снижает риск оспаривания подписанных документов.",
    ),
    "VERIFY_LICENCE_SRO": CompiledRecommendation(
        action="До заказа работ подтвердите действующую лицензию или допуск для требуемого вида деятельности.",
        rationale="Применимая проверка не подтвердила обязательный разрешительный статус.",
        effect="Это помогает исключить выполнение работ без необходимого разрешения.",
    ),
    "VERIFY_ACCOUNT_SUSPENSION": CompiledRecommendation(
        action="Уточните актуальный статус решения и выберите исполнимый порядок расчётов.",
        rationale="Найдено решение о приостановлении операций по счетам.",
        effect="Это снижает риск задержки или невозможности платежа выбранным способом.",
    ),
    "REVIEW_INDUSTRY_CHECK": CompiledRecommendation(
        action="Сопоставьте отраслевое ограничение с предметом планируемой сделки.",
        rationale="Официальная применимая проверка выявила неблагоприятный факт.",
        effect="Это позволяет определить, влияет ли ограничение на конкретные работы или услуги.",
    ),
}


_STATE_LABELS = {
    "FOUND": ("Сведения найдены", "Источник содержит относящиеся к компании сведения.", "attention"),
    "NOT_FOUND": ("Признак не выявлен", "По данным источника на указанную дату признак не выявлен.", "positive"),
    "NOT_APPLICABLE": ("Проверка не применяется", "Подтверждено, что эта проверка не относится к компании.", "neutral"),
    "NOT_CHECKED": ("Проверка ещё не выполнена", "Для этого блока пока нет завершённого результата.", "neutral"),
    "SOURCE_UNAVAILABLE": ("Источник временно недоступен", "Результат проверки остаётся неизвестным.", "attention"),
    "TIMEOUT": ("Проверка не завершилась вовремя", "Источник не вернул полный результат в установленное время.", "attention"),
    "PARSING_ERROR": ("Данные не удалось обработать", "Формат ответа не позволил получить подтверждённый результат.", "attention"),
    "UNKNOWN": ("Недостаточно данных", "Полученных сведений недостаточно для вывода.", "neutral"),
    "PARTIAL": ("Проверка выполнена частично", "Результат относится только к доступной части сведений.", "attention"),
    "CONFLICTING_EVIDENCE": ("Сведения противоречат друг другу", "Актуальное состояние требует повторной проверки.", "attention"),
}


def _date_ru(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def compile_source_status(
    state: str,
    *,
    source_date: date | None,
    negative_closure_proven: bool,
    historical_value: str | None = None,
) -> CompiledSourceStatus:
    normalized = str(state).upper()
    if normalized == "STALE_DATA":
        when = f" на {_date_ru(source_date)}" if source_date else ""
        historical = f" Историческое значение: {historical_value}." if historical_value else ""
        return CompiledSourceStatus(
            label="Данные устарели",
            explanation=(
                f"Опубликованный набор описывает состояние{when}.{historical} "
                "Текущее состояние не подтверждено."
            ).replace("..", "."),
            visual_state="attention",
        )
    if normalized == "NOT_FOUND" and not negative_closure_proven:
        return CompiledSourceStatus(
            label="Недостаточно данных",
            explanation="Охват источника не позволяет подтвердить отсутствие признака.",
            visual_state="neutral",
        )
    label, explanation, visual = _STATE_LABELS.get(normalized, _STATE_LABELS["UNKNOWN"])
    if normalized == "NOT_FOUND" and source_date:
        explanation = f"По данным источника на {_date_ru(source_date)} признак не выявлен."
    return CompiledSourceStatus(label=label, explanation=explanation, visual_state=visual)


def compile_meaning(value: MeaningInput) -> CompiledMeaning | None:
    template = FACTOR_TEMPLATES.get(value.factor_code)
    if template is None:
        return None
    return CompiledMeaning(
        meaning_id=value.meaning_id,
        category=template.category.value,
        severity=template.severity.value,
        headline=template.headline,
        short_explanation=template.short_explanation,
        full_explanation=template.full_explanation,
        client_meaning=template.client_meaning,
        what_it_does_not_mean=template.what_it_does_not_mean,
        recommendation_effect=template.recommendation_effect,
        current_state=value.current_state,
        previous_state=value.previous_state,
        change=value.change,
        trend=value.trend,
        frequency=value.frequency,
        recency=value.recency,
        duration=value.duration,
        materiality=value.materiality,
        counter_evidence=value.counter_evidence,
        confidence=value.confidence,
        source_dates=value.source_dates,
    )


def compile_limitation(code: str | None) -> CompiledLimitation:
    return LIMITATION_TEMPLATES.get(str(code or "").upper(), GENERIC_LIMITATION)


def compile_aggregate_abstention_conclusion() -> str:
    return "Недостаточно данных для общего положительного вывода. Ограничения проверки указаны ниже."


def compile_aggregate_clean_conclusion() -> str:
    return "Неблагоприятные факторы не выявлены в рамках выполненных актуальных проверок."


def compile_recommendation(code: str | None) -> CompiledRecommendation | None:
    normalized = str(code or "").upper()
    direct = RECOMMENDATION_TEMPLATES.get(normalized)
    if direct is not None:
        return direct
    if normalized.startswith("RESOLVE_") and normalized.removeprefix("RESOLVE_") in LIMITATION_TEMPLATES:
        limitation = LIMITATION_TEMPLATES[normalized.removeprefix("RESOLVE_")]
        return CompiledRecommendation(
            action="Завершите или обновите указанную проверку до принятия решения.",
            rationale=limitation.short_explanation,
            effect="Это позволит заменить неизвестное состояние подтверждённым результатом.",
        )
    return None


_BANNED_TEXT = (
    "{'", '"origin_ref"', "fact_ref", "origin_check_ref", "limitation_code",
    "recommendation_code", "parameters", "evidence_refs", "APPLICABILITY_UNKNOWN",
    "STALE_DATA", "SOURCE_UNAVAILABLE", "NOT_CHECKED", "REVEXP", "PAYTAX",
    "DEBTAM", "TAXOFFENCE", "компания ненадёжна", "компания подозрительна",
    "всё хорошо", "безопасная компания", "можно доверять", "NEXT не нашёл",
    "мы не нашли", "много судов", "тяжёлое финансовое состояние",
)
_BANNED_KEYS = {
    "origin_ref", "fact_ref", "origin_check_ref", "limitation_code",
    "recommendation_code", "parameters", "evidence_refs", "ruleset_version",
    "model_version", "source_code", "factor_code",
}
_INTERNAL_VERSION = re.compile(r"(?:risk|summary|rules?)-v\d", re.IGNORECASE)


def validate_public_text(value: Any, path: str = "$") -> None:
    """Reject internal representations or unsupported blanket conclusions."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _BANNED_KEYS:
                raise SemanticCompilerError(f"internal field at {path}.{key}")
            validate_public_text(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            validate_public_text(child, f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    folded = value.casefold()
    for banned in _BANNED_TEXT:
        if banned.casefold() in folded:
            raise SemanticCompilerError(f"forbidden public text at {path}")
    if _INTERNAL_VERSION.search(value):
        raise SemanticCompilerError(f"internal version at {path}")
