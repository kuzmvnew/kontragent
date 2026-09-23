"""Read-side freshness boundary for the S02 tax-debt publication."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select

from app.models.source import DataSet
from app.models.tax_debt import (
    FnsTaxDebtPilotState,
    FnsTaxDebtPublicationGeneration,
)
from app.sources.fns_tax_debt import PILOT_ENVIRONMENT, SOURCE_ID

FRESH = "FRESH"
STALE_DATA = "STALE_DATA"


@dataclass(frozen=True)
class TaxDebtPublicationContext:
    """Exact query generation plus the metadata that limits its validity."""

    data_as_of: date | None
    publication_generation: int
    source_as_of: datetime | None
    retrieved_at: datetime | None
    official_actual_until: date | None
    metadata_reason: str | None = None


@dataclass(frozen=True)
class TaxDebtFreshness:
    state: str
    checked_at: datetime
    reason: str | None
    missing_fields: tuple[str, ...] = ()

    @property
    def is_fresh(self) -> bool:
        return self.state == FRESH


def utc_now() -> datetime:
    return datetime.now(UTC)


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _coverage_actual_until(coverage: Mapping[str, Any] | None) -> date | None:
    return _date_value(dict(coverage or {}).get("official_actual_until"))


def resolve_tax_debt_publication(
    session,
    *,
    dataset: DataSet,
    inn: str,
) -> TaxDebtPublicationContext:
    """Resolve freshness metadata for the same generation used by the read."""

    get = getattr(session, "get", None)
    state = get(FnsTaxDebtPilotState, SOURCE_ID) if get is not None else None
    pilot_active = bool(
        state is not None
        and state.enabled
        and state.pilot_environment == PILOT_ENVIRONMENT
        and state.dataset_id == dataset.id
    )

    if not pilot_active:
        return TaxDebtPublicationContext(
            data_as_of=dataset.last_data_date,
            publication_generation=0,
            source_as_of=getattr(dataset, "source_as_of", None),
            retrieved_at=getattr(dataset, "retrieved_at", None),
            official_actual_until=_coverage_actual_until(
                getattr(dataset, "coverage", None)
            ),
        )

    cohort = frozenset(str(value) for value in (state.cohort_inns or ()))
    in_cohort = inn in cohort
    generation = int(
        state.query_generation if in_cohort else state.baseline_generation
    )
    fallback_data_as_of = (
        state.active_data_date
        if in_cohort
        else state.baseline_data_date or dataset.last_data_date
    )
    scalar = getattr(session, "scalar", None)
    if scalar is None:
        return TaxDebtPublicationContext(
            data_as_of=fallback_data_as_of,
            publication_generation=generation,
            source_as_of=None,
            retrieved_at=None,
            official_actual_until=None,
            metadata_reason="freshness_publication_metadata_missing",
        )

    publication = scalar(
        select(FnsTaxDebtPublicationGeneration).where(
            FnsTaxDebtPublicationGeneration.dataset_id == dataset.id,
            FnsTaxDebtPublicationGeneration.generation == generation,
        )
    )
    if publication is None:
        return TaxDebtPublicationContext(
            data_as_of=fallback_data_as_of,
            publication_generation=generation,
            source_as_of=None,
            retrieved_at=None,
            official_actual_until=None,
            metadata_reason="freshness_publication_metadata_missing",
        )

    return TaxDebtPublicationContext(
        data_as_of=publication.last_data_date,
        publication_generation=generation,
        source_as_of=publication.source_as_of,
        retrieved_at=publication.retrieved_at,
        official_actual_until=publication.official_actual_until,
    )


def evaluate_tax_debt_freshness(
    context: TaxDebtPublicationContext,
    *,
    checked_at: datetime,
) -> TaxDebtFreshness:
    """Fail closed unless all official S02 freshness evidence is coherent."""

    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("checked_at must include a timezone")
    checked_at = checked_at.astimezone(UTC)

    fields = {
        "official_actual_until": context.official_actual_until,
        "source_as_of": context.source_as_of,
        "data_as_of": context.data_as_of,
        "retrieved_at": context.retrieved_at,
    }
    missing = tuple(name for name, value in fields.items() if value is None)
    if context.metadata_reason or missing:
        return TaxDebtFreshness(
            state=STALE_DATA,
            checked_at=checked_at,
            reason=context.metadata_reason or "freshness_metadata_missing",
            missing_fields=missing,
        )

    source_as_of = context.source_as_of
    retrieved_at = context.retrieved_at
    data_as_of = context.data_as_of
    actual_until = context.official_actual_until
    assert source_as_of is not None
    assert retrieved_at is not None
    assert data_as_of is not None
    assert actual_until is not None

    if (
        source_as_of.tzinfo is None
        or source_as_of.utcoffset() is None
        or retrieved_at.tzinfo is None
        or retrieved_at.utcoffset() is None
        or data_as_of > source_as_of.date()
        or source_as_of > retrieved_at
        or retrieved_at > checked_at
        or data_as_of > actual_until
        or source_as_of.date() > actual_until
        or retrieved_at.date() > actual_until
    ):
        return TaxDebtFreshness(
            state=STALE_DATA,
            checked_at=checked_at,
            reason="freshness_metadata_invalid",
        )

    if checked_at.date() > actual_until:
        return TaxDebtFreshness(
            state=STALE_DATA,
            checked_at=checked_at,
            reason="official_actual_until_expired",
        )

    return TaxDebtFreshness(
        state=FRESH,
        checked_at=checked_at,
        reason=None,
    )
