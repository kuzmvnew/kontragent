"""Контракты регуляторных проверок. Не юридическая база и не сборщик данных."""

from datetime import date
from enum import StrEnum

from pydantic import Field, HttpUrl, StrictBool, computed_field, model_validator

from app.contracts.assessment import EngineVersion
from app.contracts.decision import CheckResultStatus, ContractModel, Evidence
from app.contracts.report import DealContext


class PermissionKind(StrEnum):
    LICENSE = "license"
    SRO = "sro"
    CERTIFICATE = "certificate"
    DECLARATION = "declaration"
    PERMIT = "permit"


class RequirementApplicability(StrEnum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"
    UNKNOWN = "unknown"


class PermissionStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    CONFIRMED_ABSENT = "confirmed_absent"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    NOT_CHECKED = "not_checked"


class RegulatoryOutcome(StrEnum):
    SATISFIED = "satisfied"
    CRITICAL = "critical"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class LegalBasis(ContractModel):
    """Ссылка на проверенную норму; корректность URL не доказывает её смысл."""

    document_title: str = Field(min_length=1, max_length=1000)
    provision: str = Field(min_length=1, max_length=500)
    source_url: HttpUrl


class PermissionFinding(ContractModel):
    """Результат проверки разрешения для конкретной области и даты.

    verified_on — дата, на которую установлен статус, не дата запроса.
    """

    subject_inn: str = Field(pattern=r"^(?:[0-9]{10}|[0-9]{12})$")
    scope: str = Field(min_length=1, max_length=2000)
    status: PermissionStatus
    explanation: str = Field(min_length=1, max_length=4000)
    evidence: Evidence | None = None
    source_url: HttpUrl | None = None
    record_id: str | None = Field(default=None, min_length=1, max_length=200)
    verified_for_scope: StrictBool = False
    verified_on: date | None = None

    @model_validator(mode="after")
    def validate_finding(self) -> "PermissionFinding":
        confirmed = self.status in {
            PermissionStatus.VALID,
            PermissionStatus.INVALID,
            PermissionStatus.CONFIRMED_ABSENT,
        }
        if self.status == PermissionStatus.NOT_CHECKED:
            if self.evidence is not None or self.source_url is not None:
                raise ValueError("not_checked не должен содержать результат источника")
        else:
            expected = {
                PermissionStatus.VALID: CheckResultStatus.FOUND,
                PermissionStatus.INVALID: CheckResultStatus.FOUND,
                PermissionStatus.CONFIRMED_ABSENT: CheckResultStatus.FOUND,
                PermissionStatus.NOT_FOUND: CheckResultStatus.NOT_FOUND,
                PermissionStatus.UNAVAILABLE: CheckResultStatus.UNAVAILABLE,
            }[self.status]
            if self.evidence is None or self.evidence.result != expected:
                raise ValueError("Статус разрешения не соответствует Evidence")
            if self.source_url is None:
                raise ValueError("Для проверенного источника нужна source_url")

        if confirmed:
            if not self.verified_for_scope or self.verified_on is None:
                raise ValueError("Нужно подтвердить область проверки и дату")
            if self.record_id is None or self.record_id not in self.evidence.record_ids:
                raise ValueError("Подтверждение должно ссылаться на запись Evidence")
        elif (
            self.verified_for_scope
            or self.verified_on is not None
            or self.record_id is not None
        ):
            raise ValueError("Неподтверждённый результат не может иметь подтверждение")
        return self


class RegulatoryCheck(ContractModel):
    """Проверка одного требования, а не надёжности компании в целом.

    Заполняется доверенным серверным кодом после проверки нормы, исключений
    и доказательств. Сам контракт эту содержательную проверку не выполняет.
    """

    check_id: str = Field(min_length=1, max_length=200)
    subject_inn: str = Field(pattern=r"^(?:[0-9]{10}|[0-9]{12})$")
    requirement_code: str = Field(min_length=1, max_length=200)
    permission_kind: PermissionKind
    assessment_date: date
    scope: str = Field(min_length=1, max_length=2000)
    deal_context: DealContext
    applicability: RequirementApplicability
    applicability_reason: str = Field(min_length=1, max_length=4000)
    legal_basis: tuple[LegalBasis, ...] = ()
    exceptions_reviewed: StrictBool = False
    finding: PermissionFinding
    engine_version: EngineVersion

    @model_validator(mode="after")
    def validate_requirement(self) -> "RegulatoryCheck":
        if self.finding.subject_inn != self.subject_inn or self.finding.scope != self.scope:
            raise ValueError("Подтверждение относится к другому ИНН или области проверки")
        if self.applicability != RequirementApplicability.UNKNOWN:
            if not self.legal_basis or not self.exceptions_reviewed:
                raise ValueError("Для применимости нужны правовая норма и проверка исключений")
        if (
            self.finding.verified_on is not None
            and self.finding.verified_on != self.assessment_date
        ):
            raise ValueError("Дата подтверждения должна совпадать с датой оценки")
        return self

    @computed_field
    @property
    def outcome(self) -> RegulatoryOutcome:
        if self.applicability == RequirementApplicability.UNKNOWN:
            return RegulatoryOutcome.UNKNOWN
        if self.applicability == RequirementApplicability.NOT_REQUIRED:
            return RegulatoryOutcome.NOT_APPLICABLE
        if self.finding.status == PermissionStatus.VALID:
            return RegulatoryOutcome.SATISFIED
        if self.finding.status in {
            PermissionStatus.INVALID,
            PermissionStatus.CONFIRMED_ABSENT,
        }:
            return RegulatoryOutcome.CRITICAL
        return RegulatoryOutcome.UNKNOWN

    @computed_field
    @property
    def is_blocking(self) -> bool:
        return self.outcome == RegulatoryOutcome.CRITICAL