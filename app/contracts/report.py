"""Условия сделки и результат проверки без общего рейтинга компании."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, computed_field, field_validator, model_validator

from app.contracts.assessment import SectionAssessment
from app.contracts.decision import ContractModel


class CounterpartyRole(StrEnum):
    """Роль проверяемого контрагента, а не пользователя сервиса."""

    UNKNOWN = "unknown"
    SUPPLIER = "supplier"
    BUYER = "buyer"
    CONTRACTOR = "contractor"
    CUSTOMER = "customer"
    OTHER = "other"


class DealContext(ContractModel):
    """Введённые условия сделки. Не доказательство и не вывод о лицензии."""

    counterparty_role: CounterpartyRole = CounterpartyRole.UNKNOWN
    subject: str | None = Field(default=None, min_length=1, max_length=2000)
    activity_description: str | None = Field(
        default=None, min_length=1, max_length=2000
    )
    territory: str | None = Field(default=None, min_length=1, max_length=500)
    amount: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    advance_percent: Decimal | None = Field(
        default=None, ge=0, le=100, allow_inf_nan=False
    )
    planned_start_date: date | None = None
    planned_end_date: date | None = None

    @field_validator("amount", "advance_percent", mode="before")
    @classmethod
    def validate_decimal_input(cls, value):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (str, int, Decimal))
        ):
            raise ValueError("Используйте строку, целое число или Decimal")
        return value

    @model_validator(mode="after")
    def validate_terms(self) -> "DealContext":
        if (self.amount is None) != (self.currency is None):
            raise ValueError("amount и currency должны быть указаны вместе")
        if (
            self.planned_start_date is not None
            and self.planned_end_date is not None
            and self.planned_end_date < self.planned_start_date
        ):
            raise ValueError("Дата окончания не может быть раньше начала")
        return self


class CompanyAssessment(ContractModel):
    """Контейнер разделов одной проверки; не оценка всей компании.

    expected_section_codes задаёт сервер, а не посетитель сайта.
    Отсутствие раздела и результат unavailable внутри раздела различаются.
    Формат ИНН не подтверждает существование компании или ИП.
    """

    report_id: str = Field(min_length=1, max_length=200)
    subject_inn: str = Field(pattern=r"^(?:[0-9]{10}|[0-9]{12})$")
    generated_at: datetime
    deal_context: DealContext | None = None
    expected_section_codes: tuple[str, ...] = Field(min_length=1)
    sections: tuple[SectionAssessment, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at должен содержать часовой пояс")
        return value

    @field_validator("expected_section_codes")
    @classmethod
    def validate_expected_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        codes = tuple(value.strip() for value in values)
        if any(not code or len(code) > 200 for code in codes):
            raise ValueError("Код раздела должен содержать от 1 до 200 символов")
        if len(codes) != len(set(codes)):
            raise ValueError("Ожидаемые разделы не должны повторяться")
        return codes

    @model_validator(mode="after")
    def validate_sections(self) -> "CompanyAssessment":
        codes = tuple(section.section_code for section in self.sections)
        if len(codes) != len(set(codes)):
            raise ValueError("Результаты разделов не должны повторяться")
        unexpected = set(codes).difference(self.expected_section_codes)
        if unexpected:
            raise ValueError(f"Разделы отсутствуют в плане: {sorted(unexpected)}")
        for section in self.sections:
            for evidence in section.evidence:
                if evidence.checked_at > self.generated_at:
                    raise ValueError("Проверка источника выполнена позже отчёта")
        return self

    @computed_field
    @property
    def missing_section_codes(self) -> tuple[str, ...]:
        """Разделы, по которым результат ещё не передан."""
        received = {section.section_code for section in self.sections}
        return tuple(
            code for code in self.expected_section_codes if code not in received
        )