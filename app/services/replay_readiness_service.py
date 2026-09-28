"""Canonical fail-closed contract for replay work outside a run denominator.

Read paths use this module directly, so a legacy succeeded run becomes unsafe
as soon as relevant replay is committed.  Recovery uses the same query to
materialize that condition into ``CompanySourceCoverage`` later.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session, aliased

from app.contracts.data_readiness import AutoUpdateStatus, OperationalStatus
from app.models.company import Company
from app.models.company_enrichment import CompanySourceCoverage
from app.models.registry_master import MasterReplaySignal
from app.models.source import DataSet
from app.services.source_applicability_service import (
    SourceApplicability,
    source_applicability_expression,
)


ACTIONABLE_REPLAY_STATUSES = ("pending", "scheduled")
READINESS_BLOCKING_REPLAY_STATUSES = ("pending", "scheduled", "failed")
# Compatibility name for mutation/recovery callers.  Failed signals are
# readiness blockers but never become actionable backlog implicitly.
UNRESOLVED_REPLAY_STATUSES = ACTIONABLE_REPLAY_STATUSES
BLOCKING_REPLAY_OUTCOMES = (
    SourceApplicability.APPLICABLE,
    SourceApplicability.UNKNOWN,
)
TERMINAL_COVERAGE_STATUSES = ("FOUND", "NOT_FOUND", "NOT_APPLICABLE")


def operational_replay_predicates(dataset, *, now: datetime) -> tuple[object, ...]:
    """The one operational-source filter shared by replay safety queries."""

    return (
        dataset.enabled.is_(True),
        dataset.auto_update_status == AutoUpdateStatus.CONFIGURED,
        dataset.operational_status == OperationalStatus.CURRENT,
        dataset.last_success_at.is_not(None),
        dataset.coverage["operational_accepted"].as_boolean().is_not(False),
        (
            dataset.next_expected_update_at.is_(None)
            | (dataset.next_expected_update_at > now)
        ),
    )


def signal_is_resolved_by_run_clause(signal, run_id):
    """Match one replay UUID to successful terminal coverage in one run."""

    coverage = aliased(CompanySourceCoverage)
    return (
        select(coverage.id)
        .where(
            coverage.enrichment_run_id == run_id,
            coverage.company_id == signal.company_id,
            coverage.source_id == signal.target_source_id,
            coverage.status.in_(TERMINAL_COVERAGE_STATUSES),
            coverage.execution_status == "succeeded",
            coverage.master_replay_signal_ids.op("@>")(
                func.jsonb_build_array(cast(signal.id, String))
            ),
        )
        .exists()
    )


def unresolved_replay_exists_clause(
    company_id,
    run_id,
    *,
    now: datetime,
    outcomes: Sequence[SourceApplicability] = BLOCKING_REPLAY_OUTCOMES,
    statuses: Sequence[str] = READINESS_BLOCKING_REPLAY_STATUSES,
):
    """Return a correlated EXISTS for relevant replay unresolved by ``run_id``.

    A signal is safely resolved only when that exact signal UUID is represented
    by terminal, successful coverage for the same source in the current run.
    Source-only matching is deliberately insufficient.
    """

    signal = aliased(MasterReplaySignal)
    dataset = aliased(DataSet)
    company = aliased(Company)
    decision = source_applicability_expression(
        dataset.applicability,
        company.entity_type,
        company.inn,
    )
    return (
        select(signal.id)
        .join(dataset, dataset.code == signal.target_source_id)
        .join(company, company.id == signal.company_id)
        .where(
            signal.company_id == company_id,
            signal.status.in_(tuple(statuses)),
            *operational_replay_predicates(dataset, now=now),
            decision.in_(tuple(outcome.value for outcome in outcomes)),
            ~signal_is_resolved_by_run_clause(signal, run_id),
        )
        .exists()
    )


def has_unresolved_replay(
    session: Session,
    *,
    company_id: int,
    run_id: UUID,
    now: datetime,
    outcomes: Sequence[SourceApplicability] = BLOCKING_REPLAY_OUTCOMES,
    statuses: Sequence[str] = READINESS_BLOCKING_REPLAY_STATUSES,
) -> bool:
    """Read-only current-run replay safety decision."""

    return bool(
        session.scalar(
            select(
                unresolved_replay_exists_clause(
                    company_id,
                    run_id,
                    now=now,
                    outcomes=outcomes,
                    statuses=statuses,
                )
            )
        )
    )


def unresolved_replay_signal_map(
    session: Session,
    *,
    company_id: int,
    run_id: UUID | None,
    now: datetime,
    outcomes: Sequence[SourceApplicability],
    statuses: Sequence[str] = ACTIONABLE_REPLAY_STATUSES,
    lock: bool = False,
    skip_locked: bool = False,
) -> dict[str, tuple[UUID, ...]]:
    """Group unresolved signal identities using the canonical safety contract."""

    decision = source_applicability_expression(
        DataSet.applicability,
        Company.entity_type,
        Company.inn,
    )
    statement = (
        select(MasterReplaySignal)
        .join(DataSet, DataSet.code == MasterReplaySignal.target_source_id)
        .join(Company, Company.id == MasterReplaySignal.company_id)
        .where(
            MasterReplaySignal.company_id == company_id,
            MasterReplaySignal.status.in_(tuple(statuses)),
            *operational_replay_predicates(DataSet, now=now),
            decision.in_(tuple(outcome.value for outcome in outcomes)),
            ~signal_is_resolved_by_run_clause(MasterReplaySignal, run_id),
        )
        .order_by(MasterReplaySignal.created_at, MasterReplaySignal.id)
    )
    if lock:
        statement = statement.with_for_update(skip_locked=skip_locked)
    grouped: dict[str, list[UUID]] = defaultdict(list)
    for signal in session.scalars(statement):
        grouped[signal.target_source_id].append(signal.id)
    return {source_id: tuple(values) for source_id, values in grouped.items()}
