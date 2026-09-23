"""Public, presentation-only contracts for Company Card v2.

These models are the boundary between an approved public projection and UI
components.  They deliberately contain neither internal source provenance nor
domain-engine inputs.
"""

from __future__ import annotations

from datetime import date as Date
from enum import StrEnum

from pydantic import Field, model_validator

from app.contracts.decision import ContractModel


class PublicUIState(StrEnum):
    """Closed state vocabulary understood by Card v2 components."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    PARTIAL = "PARTIAL"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    ERROR = "ERROR"


class CardActionKind(StrEnum):
    GET_REPORT = "GET_REPORT"
    WATCH = "WATCH"
    SAVE = "SAVE"


class PublicEvidenceReference(ContractModel):
    """Minimal public evidence; raw provenance is intentionally not exposed."""

    source_name: str = Field(min_length=1, max_length=160)
    date: Date | None = None
    description: str = Field(min_length=1, max_length=1000)
    status: PublicUIState


class CoverageItem(ContractModel):
    """One capability's check state, never a company-risk assessment."""

    capability: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=240)
    state: PublicUIState
    limitation: str | None = Field(default=None, min_length=1, max_length=1000)


class CardAction(ContractModel):
    """A semantic action ready for the Card v2 component to render."""

    action: CardActionKind
    label: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    disabled_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_availability(self) -> "CardAction":
        if self.enabled and self.disabled_reason is not None:
            raise ValueError("Enabled actions cannot have a disabled reason")
        if not self.enabled and self.disabled_reason is None:
            raise ValueError("Disabled actions require a public reason")
        return self


class CompanyHeaderViewModel(ContractModel):
    name: str = Field(min_length=1, max_length=1000)
    inn: str = Field(pattern=r"^(?:\d{10}|\d{12})$")
    kpp: str | None = Field(default=None, pattern=r"^\d{9}$")
    ogrn: str | None = Field(default=None, pattern=r"^(?:\d{13}|\d{15})$")
    legal_status: str | None = Field(default=None, min_length=1, max_length=240)
    as_of: Date | None = None


class SummaryViewModel(ContractModel):
    state: PublicUIState
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=2000)
    limitations: tuple[str, ...] = ()


class RiskViewModel(ContractModel):
    state: PublicUIState
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=2000)
    evidence: tuple[PublicEvidenceReference, ...] = ()


class CoverageViewModel(ContractModel):
    title: str = "Полнота проверки"
    items: tuple[CoverageItem, ...]


class EvidenceViewModel(ContractModel):
    title: str = "Источники и основания"
    items: tuple[PublicEvidenceReference, ...]


class ActionViewModel(ContractModel):
    actions: tuple[CardAction, ...]

    @model_validator(mode="after")
    def require_unique_actions(self) -> "ActionViewModel":
        kinds = tuple(item.action for item in self.actions)
        if len(kinds) != len(set(kinds)):
            raise ValueError("Card actions must be unique")
        return self


class CardV2ViewModel(ContractModel):
    """Complete payload consumed by Card v2 UI components."""

    company_header: CompanyHeaderViewModel
    summary: SummaryViewModel
    risk: RiskViewModel
    coverage: CoverageViewModel
    evidence: EvidenceViewModel
    actions: ActionViewModel
