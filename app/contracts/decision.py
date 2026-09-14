from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Any, Iterable, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


class CheckResultStatus(StrEnum):
    """Допустимые состояния проверки одного источника."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


class CoverageStatus(StrEnum):
    """Итоговое состояние полноты группы проверок."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class ContractModel(BaseModel):
    """Базовые строгие настройки всех decision-контрактов."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class Evidence(ContractModel):
    """
    Доказательство того, как был проверен один dataset.

    Evidence ничего не говорит о риске самостоятельно.
    Оно фиксирует источник, дату и фактический результат проверки.
    """

    evidence_id: str = Field(
        min_length=1,
        max_length=200,
    )

    source_code: str = Field(
        min_length=1,
        max_length=100,
    )

    dataset_code: str = Field(
        min_length=1,
        max_length=100,
    )

    result: CheckResultStatus

    data_date: date | None = None

    checked_at: datetime = Field(
        default_factory=(
            lambda: datetime.now(
                timezone.utc
            )
        )
    )

    reason: str | None = Field(
        default=None,
        max_length=500,
    )

    record_ids: tuple[str, ...] = ()

    metadata: dict[str, Any] = Field(
        default_factory=dict,
    )

    @field_validator(
        "evidence_id",
        "source_code",
        "dataset_code",
    )
    @classmethod
    def validate_required_text(
        cls,
        value: str,
    ) -> str:
        if not value:
            raise ValueError(
                "Значение не может быть пустым"
            )

        return value

    @field_validator("checked_at")
    @classmethod
    def validate_checked_at(
        cls,
        value: datetime,
    ) -> datetime:
        if (
            value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ValueError(
                "checked_at должен содержать часовой пояс"
            )

        return value

    @field_validator("record_ids")
    @classmethod
    def validate_record_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized = tuple(
            value.strip()
            for value in values
            if value.strip()
        )

        if len(normalized) != len(set(normalized)):
            raise ValueError(
                "record_ids не должны содержать дубли"
            )

        return normalized

    @model_validator(mode="after")
    def validate_result_contract(
        self,
    ) -> "Evidence":
        if self.result in {
            CheckResultStatus.FOUND,
            CheckResultStatus.NOT_FOUND,
        } and self.data_date is None:
            raise ValueError(
                "Для found/not_found обязательна data_date"
            )

        if self.result in {
            CheckResultStatus.NOT_APPLICABLE,
            CheckResultStatus.UNAVAILABLE,
        } and not self.reason:
            raise ValueError(
                "Для not_applicable/unavailable обязательна reason"
            )

        if (
            self.result
            != CheckResultStatus.FOUND
            and self.record_ids
        ):
            raise ValueError(
                "record_ids допустимы только для found"
            )

        return self


class Coverage(ContractModel):
    """
    Полнота группы проверок.

    not_applicable не ухудшает coverage, потому что источник
    не обязан применяться к каждой сущности.
    """

    total_checks: int = Field(ge=0)
    applicable_checks: int = Field(ge=0)
    completed_checks: int = Field(ge=0)
    found_checks: int = Field(ge=0)
    not_found_checks: int = Field(ge=0)
    unavailable_checks: int = Field(ge=0)
    not_applicable_checks: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(
        self,
    ) -> "Coverage":
        if self.total_checks != (
            self.applicable_checks
            + self.not_applicable_checks
        ):
            raise ValueError(
                "total_checks должен равняться applicable_checks "
                "+ not_applicable_checks"
            )

        if self.applicable_checks != (
            self.completed_checks
            + self.unavailable_checks
        ):
            raise ValueError(
                "applicable_checks должен равняться completed_checks "
                "+ unavailable_checks"
            )

        if self.completed_checks != (
            self.found_checks
            + self.not_found_checks
        ):
            raise ValueError(
                "completed_checks должен равняться found_checks "
                "+ not_found_checks"
            )

        return self

    @computed_field
    @property
    def status(
        self,
    ) -> CoverageStatus:
        if self.applicable_checks == 0:
            return CoverageStatus.NOT_APPLICABLE

        if self.completed_checks == 0:
            return CoverageStatus.UNAVAILABLE

        if self.completed_checks < self.applicable_checks:
            return CoverageStatus.PARTIAL

        return CoverageStatus.COMPLETE

    @computed_field
    @property
    def ratio(
        self,
    ) -> Decimal | None:
        if self.applicable_checks == 0:
            return None

        return (
            Decimal(self.completed_checks)
            / Decimal(self.applicable_checks)
        ).quantize(
            Decimal("0.0001"),
            rounding=ROUND_HALF_UP,
        )

    @computed_field
    @property
    def percent(
        self,
    ) -> Decimal | None:
        if self.ratio is None:
            return None

        return (
            self.ratio
            * Decimal("100")
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )


def evidence_from_check_result(
    check_result: Mapping[str, Any],
    *,
    evidence_id: str,
    checked_at: datetime | None = None,
    record_ids: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> Evidence:
    """
    Преобразует существующий словарь check-result в Evidence.

    Функция является мостом между текущими налоговыми сервисами
    и будущими Fact/Signal/Conclusion объектами.
    """

    required_keys = {
        "checked",
        "applicable",
        "result",
        "data_date",
        "dataset_code",
        "source",
        "reason",
    }

    missing_keys = sorted(
        required_keys.difference(
            check_result.keys()
        )
    )

    if missing_keys:
        raise ValueError(
            "В check-result отсутствуют поля: "
            + ", ".join(missing_keys)
        )

    result = CheckResultStatus(
        check_result["result"]
    )

    checked = check_result["checked"]
    applicable = check_result["applicable"]

    if result in {
        CheckResultStatus.FOUND,
        CheckResultStatus.NOT_FOUND,
    } and (
        checked is not True
        or applicable is not True
    ):
        raise ValueError(
            "found/not_found требуют checked=True "
            "и applicable=True"
        )

    if (
        result == CheckResultStatus.NOT_APPLICABLE
        and (
            checked is not True
            or applicable is not False
        )
    ):
        raise ValueError(
            "not_applicable требует checked=True "
            "и applicable=False"
        )

    if (
        result == CheckResultStatus.UNAVAILABLE
        and checked is not False
    ):
        raise ValueError(
            "unavailable требует checked=False"
        )

    return Evidence(
        evidence_id=evidence_id,
        source_code=check_result["source"],
        dataset_code=check_result["dataset_code"],
        result=result,
        data_date=check_result["data_date"],
        checked_at=(
            checked_at
            if checked_at is not None
            else datetime.now(timezone.utc)
        ),
        reason=check_result["reason"],
        record_ids=tuple(record_ids),
        metadata=dict(
            metadata
            if metadata is not None
            else {}
        ),
    )


def build_coverage(
    evidences: Iterable[Evidence],
) -> Coverage:
    """Рассчитывает coverage без оценки надёжности компании."""

    evidence_list = tuple(evidences)

    found_checks = sum(
        evidence.result
        == CheckResultStatus.FOUND
        for evidence in evidence_list
    )

    not_found_checks = sum(
        evidence.result
        == CheckResultStatus.NOT_FOUND
        for evidence in evidence_list
    )

    unavailable_checks = sum(
        evidence.result
        == CheckResultStatus.UNAVAILABLE
        for evidence in evidence_list
    )

    not_applicable_checks = sum(
        evidence.result
        == CheckResultStatus.NOT_APPLICABLE
        for evidence in evidence_list
    )

    completed_checks = (
        found_checks
        + not_found_checks
    )

    applicable_checks = (
        completed_checks
        + unavailable_checks
    )

    return Coverage(
        total_checks=len(evidence_list),
        applicable_checks=applicable_checks,
        completed_checks=completed_checks,
        found_checks=found_checks,
        not_found_checks=not_found_checks,
        unavailable_checks=unavailable_checks,
        not_applicable_checks=not_applicable_checks,
    )