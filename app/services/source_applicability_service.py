"""Canonical fail-closed company/source applicability contract.

The persisted ``DataSet.applicability`` value is the runtime authority. Source
codes are deliberately absent: adding a dataset must not add an implicit policy.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import String, and_, case, cast, func, literal, or_
from sqlalchemy.dialects.postgresql import JSONB


class SourceApplicability(StrEnum):
    APPLICABLE = "APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class CompanySubject(StrEnum):
    LEGAL = "legal"
    INDIVIDUAL_ENTREPRENEUR = "individual_entrepreneur"
    UNKNOWN = "unknown"


ALLOWED_ENTITY_TYPES = frozenset(
    {CompanySubject.LEGAL.value, CompanySubject.INDIVIDUAL_ENTREPRENEUR.value}
)
_VALID_ENTITY_TYPE_SEQUENCES = (
    (CompanySubject.LEGAL.value,),
    (CompanySubject.INDIVIDUAL_ENTREPRENEUR.value,),
    (CompanySubject.LEGAL.value, CompanySubject.INDIVIDUAL_ENTREPRENEUR.value),
    (CompanySubject.INDIVIDUAL_ENTREPRENEUR.value, CompanySubject.LEGAL.value),
)


def parse_source_applicability(value: Any) -> frozenset[str] | None:
    """Validate persisted metadata, returning ``None`` for UNKNOWN policy."""

    if not isinstance(value, dict):
        return None
    entity_types = value.get("entity_types")
    if not isinstance(entity_types, list) or not entity_types:
        return None
    if any(not isinstance(item, str) for item in entity_types):
        return None
    if len(entity_types) != len(set(entity_types)):
        return None
    resolved = frozenset(entity_types)
    if not resolved.issubset(ALLOWED_ENTITY_TYPES):
        return None
    return resolved


def resolve_company_subject(*, entity_type: Any, inn: Any) -> CompanySubject:
    """Resolve a subject only when explicit type and identifier agree."""

    normalized_type = str(entity_type or "").strip().lower()
    normalized_inn = str(inn or "").strip()
    if normalized_type == CompanySubject.LEGAL and len(normalized_inn) == 10:
        return CompanySubject.LEGAL
    if (
        normalized_type == CompanySubject.INDIVIDUAL_ENTREPRENEUR
        and len(normalized_inn) == 12
    ):
        return CompanySubject.INDIVIDUAL_ENTREPRENEUR
    return CompanySubject.UNKNOWN


def company_scope(company) -> str:
    """Compatibility adapter exposing the canonical company subject value."""

    return resolve_company_subject(
        entity_type=company.entity_type,
        inn=company.inn,
    ).value


def resolve_source_applicability(company, dataset) -> SourceApplicability:
    """Resolve applicability from validated persisted dataset metadata."""

    policy = parse_source_applicability(dataset.applicability)
    subject = resolve_company_subject(
        entity_type=company.entity_type,
        inn=company.inn,
    )
    if policy is None or subject is CompanySubject.UNKNOWN:
        return SourceApplicability.UNKNOWN
    if subject.value in policy:
        return SourceApplicability.APPLICABLE
    return SourceApplicability.NOT_APPLICABLE


def company_subject_expression(company_entity_type_column, company_inn_column):
    """PostgreSQL expression equivalent to :func:`resolve_company_subject`."""

    normalized_type = func.lower(
        func.btrim(func.coalesce(cast(company_entity_type_column, String), ""))
    )
    inn_length = func.length(func.btrim(cast(company_inn_column, String)))
    return case(
        (
            and_(
                normalized_type == CompanySubject.LEGAL.value,
                inn_length == 10,
            ),
            CompanySubject.LEGAL.value,
        ),
        (
            and_(
                normalized_type == CompanySubject.INDIVIDUAL_ENTREPRENEUR.value,
                inn_length == 12,
            ),
            CompanySubject.INDIVIDUAL_ENTREPRENEUR.value,
        ),
        else_=CompanySubject.UNKNOWN.value,
    )


def source_applicability_expression(
    dataset_applicability_column,
    company_entity_type_column,
    company_inn_column,
):
    """Return the canonical tri-state applicability as a SQL expression."""

    entity_types = dataset_applicability_column.op("->")("entity_types")
    valid_sequences = tuple(
        entity_types == literal(list(sequence), type_=JSONB)
        for sequence in _VALID_ENTITY_TYPE_SEQUENCES
    )
    legal_only, ip_only, legal_ip, ip_legal = valid_sequences
    valid_policy = or_(*valid_sequences)
    subject = company_subject_expression(company_entity_type_column, company_inn_column)
    applies = or_(
        and_(subject == CompanySubject.LEGAL.value, or_(legal_only, legal_ip, ip_legal)),
        and_(
            subject == CompanySubject.INDIVIDUAL_ENTREPRENEUR.value,
            or_(ip_only, legal_ip, ip_legal),
        ),
    )
    return case(
        (
            valid_policy,
            case(
                (subject == CompanySubject.UNKNOWN.value, SourceApplicability.UNKNOWN.value),
                (applies, SourceApplicability.APPLICABLE.value),
                else_=SourceApplicability.NOT_APPLICABLE.value,
            ),
        ),
        else_=SourceApplicability.UNKNOWN.value,
    )


def source_applicability_clause(
    dataset_applicability_column,
    company_entity_type_column,
    company_inn_column,
):
    """Select only explicitly applicable work; UNKNOWN is never actionable."""

    return source_applicability_expression(
        dataset_applicability_column,
        company_entity_type_column,
        company_inn_column,
    ) == SourceApplicability.APPLICABLE.value
