"""Approved public projection consumed by the Company Card v2 adapter.

Only public, display-safe values belong here.  Database models, internal Risk
or Summary objects, provider results, and raw source payloads are deliberately
outside this contract.
"""

from __future__ import annotations

from datetime import date as Date

from pydantic import Field, model_validator

from app.contracts.decision import ContractModel
from app.contracts.public_card_types import CardActionKind, PublicUIState


class PublicCompanyHeaderProjection(ContractModel):
    name: str = Field(min_length=1, max_length=1000)
    inn: str = Field(pattern=r"^(?:\d{10}|\d{12})$")
    kpp: str | None = Field(default=None, pattern=r"^\d{9}$")
    ogrn: str | None = Field(default=None, pattern=r"^(?:\d{13}|\d{15})$")
    legal_status: str | None = Field(default=None, min_length=1, max_length=240)
    as_of: Date | None = None


class PublicSummaryProjection(ContractModel):
    state: PublicUIState
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=2000)
    limitations: tuple[str, ...] = ()


class PublicEvidenceProjection(ContractModel):
    source_name: str = Field(min_length=1, max_length=160)
    date: Date | None = None
    description: str = Field(min_length=1, max_length=1000)
    status: PublicUIState


class PublicRiskProjection(ContractModel):
    state: PublicUIState
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=2000)
    evidence: tuple[PublicEvidenceProjection, ...] = ()


class PublicCoverageItemProjection(ContractModel):
    capability: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=240)
    state: PublicUIState
    limitation: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def require_not_applicable_reason(self) -> "PublicCoverageItemProjection":
        if self.state == PublicUIState.NOT_APPLICABLE and self.limitation is None:
            raise ValueError("NOT_APPLICABLE coverage requires a public reason")
        return self


class PublicCardActionProjection(ContractModel):
    action: CardActionKind
    label: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    disabled_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_availability(self) -> "PublicCardActionProjection":
        if self.enabled and self.disabled_reason is not None:
            raise ValueError("Enabled actions cannot have a disabled reason")
        if not self.enabled and self.disabled_reason is None:
            raise ValueError("Disabled actions require a public reason")
        return self


class PublicCardProjection(ContractModel):
    """Single public-only input accepted by CardV2ViewModelService."""

    company_header: PublicCompanyHeaderProjection
    summary: PublicSummaryProjection
    risk: PublicRiskProjection
    coverage_items: tuple[PublicCoverageItemProjection, ...]
    evidence: tuple[PublicEvidenceProjection, ...]
    actions: tuple[PublicCardActionProjection, ...]

    @model_validator(mode="after")
    def require_unique_actions(self) -> "PublicCardProjection":
        kinds = tuple(item.action for item in self.actions)
        if len(kinds) != len(set(kinds)):
            raise ValueError("Public card actions must be unique")
        return self
