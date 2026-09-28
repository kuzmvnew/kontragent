"""Versioned semantic company-view contracts.

These contracts deliberately describe business facts rather than provider
payloads.  Provider-specific data is normalized before it reaches this layer.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


COMPANY_VIEW_VERSION = "company-view-v1"
COMPANY_RESOLUTION_VERSION = "company-resolution-v1"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResolutionState(StrEnum):
    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    RESTRICTED = "RESTRICTED"


class Audience(StrEnum):
    PUBLIC = "PUBLIC"
    AUTHENTICATED = "AUTHENTICATED"
    INTERNAL = "INTERNAL"


class DataState(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_CHECKED = "NOT_CHECKED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    PARSING_ERROR = "PARSING_ERROR"
    STALE_DATA = "STALE_DATA"
    UNKNOWN = "UNKNOWN"
    PARTIAL = "PARTIAL"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


class FactRights(StrEnum):
    PUBLIC = "PUBLIC"
    AUTHENTICATED_ONLY = "AUTHENTICATED_ONLY"
    INTERNAL_ONLY = "INTERNAL_ONLY"


class EvidenceSourceClass(StrEnum):
    OFFICIAL_PRIMARY = "OFFICIAL_PRIMARY"
    OFFICIAL_API_OPEN_DATA = "OFFICIAL_API_OPEN_DATA"
    AUTHORIZED_BRIDGE = "AUTHORIZED_BRIDGE"
    DERIVED = "DERIVED"


class Freshness(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class FinancePeriodKind(StrEnum):
    YEAR = "YEAR"
    QUARTER = "QUARTER"
    DATE = "DATE"


class FinanceMetricCode(StrEnum):
    REVENUE = "REVENUE"
    EXPENSES = "EXPENSES"
    PROFIT_LOSS = "PROFIT_LOSS"
    NET_PROFIT = "NET_PROFIT"
    EQUITY = "EQUITY"
    COMPANY_VALUE = "COMPANY_VALUE"
    EMPLOYEE_COUNT = "EMPLOYEE_COUNT"


class RelatedPersonRelationType(StrEnum):
    MANAGER = "MANAGER"
    FOUNDER = "FOUNDER"
    PARTICIPANT = "PARTICIPANT"
    INDIVIDUAL_ENTREPRENEUR = "INDIVIDUAL_ENTREPRENEUR"
    OTHER_PUBLIC_RELATION = "OTHER_PUBLIC_RELATION"


class RelationStatus(StrEnum):
    CURRENT = "CURRENT"
    HISTORICAL = "HISTORICAL"


class PersonIdentifierType(StrEnum):
    INN = "INN"
    OGRNIP = "OGRNIP"


class ContactType(StrEnum):
    PHONE = "PHONE"
    EMAIL = "EMAIL"


class ContactScope(StrEnum):
    CORPORATE = "CORPORATE"
    PERSONAL = "PERSONAL"
    UNKNOWN = "UNKNOWN"


class RelatedCompany(ContractModel):
    inn: str = Field(pattern=r"^(?:\d{10}|\d{12})$")
    name: str = Field(min_length=1, max_length=1000)


class PersonIdentifier(ContractModel):
    identifier_type: PersonIdentifierType
    value: str

    @model_validator(mode="after")
    def validate_identifier(self) -> "PersonIdentifier":
        pattern = r"\d{12}" if self.identifier_type == PersonIdentifierType.INN else r"\d{15}"
        if re.fullmatch(pattern, self.value) is None:
            raise ValueError(f"invalid {self.identifier_type.value}")
        return self


class RelatedPersonRelation(ContractModel):
    relation_type: RelatedPersonRelationType
    role: str | None = Field(default=None, max_length=500)
    context: str | None = Field(default=None, max_length=1000)
    share: str | None = Field(default=None, max_length=200)
    since: date | None = None
    until: date | None = None
    status: RelationStatus

    @model_validator(mode="after")
    def historical_end_is_not_current(self) -> "RelatedPersonRelation":
        if self.until is not None and self.status != RelationStatus.HISTORICAL:
            raise ValueError("ended relationship must be historical")
        return self


class IndividualEntrepreneurRegistration(ContractModel):
    ogrnip: str = Field(pattern=r"^\d{15}$")
    status: str | None = Field(default=None, max_length=500)
    registration_date: date | None = None
    termination_date: date | None = None
    current_status: RelationStatus

    @model_validator(mode="after")
    def terminated_ip_is_historical(self) -> "IndividualEntrepreneurRegistration":
        if self.termination_date is not None and self.current_status != RelationStatus.HISTORICAL:
            raise ValueError("terminated individual entrepreneur must be historical")
        return self


class RelatedPersonValue(ContractModel):
    person_ref: str = Field(pattern=r"^person:[0-9a-f-]{36}$")
    name: str = Field(min_length=1, max_length=1000)
    relation_types: tuple[RelatedPersonRelationType, ...] = Field(min_length=1)
    relations: tuple[RelatedPersonRelation, ...] = Field(min_length=1)
    identifiers: tuple[PersonIdentifier, ...] = ()
    related_company: RelatedCompany
    current_status: RelationStatus
    individual_entrepreneur: IndividualEntrepreneurRegistration | None = None
    # Compatibility fields retained for current CompanyView consumers.
    type: str | None = Field(default=None, max_length=100)
    position: str | None = Field(default=None, max_length=500)
    share: str | None = Field(default=None, max_length=200)
    since: date | None = None

    @model_validator(mode="after")
    def relation_types_match_relations(self) -> "RelatedPersonValue":
        relation_types = tuple(dict.fromkeys(item.relation_type for item in self.relations))
        if set(relation_types) != set(self.relation_types):
            raise ValueError("relation_types must describe relations")
        if self.current_status == RelationStatus.HISTORICAL and any(
            item.status == RelationStatus.CURRENT for item in self.relations
        ):
            raise ValueError("person with a current relation cannot be historical")
        return self


class PublicContactValue(ContractModel):
    contact_type: ContactType
    contact_scope: ContactScope
    value: str = Field(min_length=3, max_length=500)
    related_person_ref: str | None = Field(
        default=None, pattern=r"^person:[0-9a-f-]{36}$"
    )
    person_name: str | None = Field(default=None, max_length=1000)
    role_context: str | None = Field(default=None, max_length=1000)
    related_company: RelatedCompany
    current_status: RelationStatus = RelationStatus.CURRENT


class CompanyResolutionV1(ContractModel):
    contract_version: Literal["company-resolution-v1"] = COMPANY_RESOLUTION_VERSION
    state: ResolutionState
    inn: str
    audience: Audience
    company_id: int | None = None
    matched_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def protect_internal_id(self) -> "CompanyResolutionV1":
        if self.audience == Audience.PUBLIC and self.company_id is not None:
            raise ValueError("public company resolution must hide company_id")
        if self.state == ResolutionState.RESOLVED and self.matched_count != 1:
            raise ValueError("resolved company must have exactly one match")
        return self


class FinancePeriod(ContractModel):
    kind: FinancePeriodKind
    year: int | None = Field(default=None, ge=1900, le=2200)
    quarter: int | None = Field(default=None, ge=1, le=4)
    on_date: date | None = None

    @model_validator(mode="after")
    def one_period_shape(self) -> "FinancePeriod":
        if self.kind == FinancePeriodKind.YEAR and (
            self.year is None or self.quarter is not None or self.on_date is not None
        ):
            raise ValueError("YEAR period requires only year")
        if self.kind == FinancePeriodKind.QUARTER and (
            self.year is None or self.quarter is None or self.on_date is not None
        ):
            raise ValueError("QUARTER period requires year and quarter")
        if self.kind == FinancePeriodKind.DATE and (
            self.on_date is None or self.year is not None or self.quarter is not None
        ):
            raise ValueError("DATE period requires only on_date")
        return self

    @property
    def identity(self) -> str:
        if self.kind == FinancePeriodKind.YEAR:
            return f"YEAR:{self.year}"
        if self.kind == FinancePeriodKind.QUARTER:
            return f"QUARTER:{self.year}:Q{self.quarter}"
        return f"DATE:{self.on_date.isoformat()}"


class FactAnchor(ContractModel):
    fact_ref: str = Field(pattern=r"^fact:[0-9a-f-]{36}$")
    item_ref: str = Field(pattern=r"^item:[0-9a-f-]{36}$")
    company_id: int | None = Field(default=None, gt=0)
    section_key: str = Field(min_length=1, max_length=80)
    field_key: str = Field(min_length=1, max_length=120)
    period_identity: str = Field(default="", max_length=80)
    item_identity: str = Field(default="", max_length=300)


class SemanticEvidence(ContractModel):
    evidence_ref: str = Field(min_length=1, max_length=240)
    source_code: str = Field(min_length=1, max_length=100)
    source_class: EvidenceSourceClass
    source_ref: str | None = Field(default=None, max_length=2000)
    value: Any
    source_data_date: date | None = None
    retrieved_at: datetime
    confidence: float = Field(ge=0, le=1)
    freshness: Freshness
    rights: FactRights
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def aware_retrieval_time(self) -> "SemanticEvidence":
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must contain a timezone")
        return self


class SemanticFact(ContractModel):
    anchor: FactAnchor
    selected_evidence: SemanticEvidence
    alternative_evidence: tuple[SemanticEvidence, ...] = ()
    state: DataState
    rights: FactRights
    created_at: datetime
    updated_at: datetime

    @property
    def fact_ref(self) -> str:
        return self.anchor.fact_ref

    @property
    def item_ref(self) -> str:
        return self.anchor.item_ref


class FinanceMetric(ContractModel):
    fact_ref: str
    period: FinancePeriod
    metric: FinanceMetricCode
    value: Decimal
    currency: str | None = Field(default=None, max_length=3)
    source: str
    source_data_date: date | None = None
    retrieved_at: datetime
    confidence: float = Field(ge=0, le=1)
    freshness: Freshness
    limitations: tuple[str, ...] = ()
    state: DataState = DataState.FOUND


class CompanyViewSectionV1(ContractModel):
    section_key: str
    state: DataState
    facts: tuple[SemanticFact, ...] = ()


class CompanyViewModelV1(ContractModel):
    contract_version: Literal["company-view-v1"] = COMPANY_VIEW_VERSION
    revision: str = Field(pattern=r"^cv1:[0-9a-f]{64}$")
    generated_at: datetime
    audience: Audience
    company_id: int | None
    inn: str
    sections: tuple[CompanyViewSectionV1, ...]
    finances: tuple[FinanceMetric, ...] = ()
    risk_ref: str | None = None
    summary_ref: str | None = None
    action_context: None = None
    links: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_view(self) -> "CompanyViewModelV1":
        keys = tuple(section.section_key for section in self.sections)
        if len(keys) != len(set(keys)):
            raise ValueError("company view section keys must be unique")
        if self.audience == Audience.PUBLIC and self.company_id is not None:
            raise ValueError("public company view must hide company_id")
        return self
