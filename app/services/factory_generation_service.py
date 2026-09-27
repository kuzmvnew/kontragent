"""Restart-safe source and ruleset generation processing.

A source generation schedules only that source for existing Master companies.
Ruleset generations recalculate only companies with a complete persisted
enrichment run.  Neither path performs work during page rendering.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.contracts.risk_v3 import UnsupportedSubjectOutcome
from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichmentRun, CompanySourceCoverage
from app.models.factory import FactoryGeneration, FactoryGenerationCompany
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from app.models.source import DataSet
from app.models.worker import WorkerPublicationState
from app.services.data_readiness_service import safe_error_message
from app.services.risk_v3_persistence_service import (
    calculate_company_risk_v3_from_persisted,
    get_or_create_summary_v3,
)
from app.services.replay_readiness_service import unresolved_replay_exists_clause
from app.services.source_applicability_service import (
    SourceApplicability,
    parse_source_applicability,
    source_applicability_expression,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _worker_source_id(dataset_code: str) -> str:
    return "S02" if dataset_code == "fns_tax_debt" else dataset_code


def register_ruleset_generation(
    session: Session,
    *,
    generation_type: str,
    ruleset_version: str,
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[FactoryGeneration, bool]:
    """Register one idempotent Risk/Semantic recalculation generation."""

    if generation_type not in {"risk_ruleset", "semantic_ruleset"}:
        raise ValueError("ruleset generation type is invalid")
    if not ruleset_version.strip():
        raise ValueError("ruleset_version is required")
    key = f"{generation_type}:{ruleset_version.strip()}"
    existing = session.scalar(
        select(FactoryGeneration).where(FactoryGeneration.generation_key == key)
    )
    if existing is not None:
        return existing, False
    observed_at = now or utc_now()
    generation = FactoryGeneration(
        generation_type=generation_type,
        generation_key=key,
        ruleset_version=ruleset_version.strip(),
        status="pending",
        selection_complete=False,
        cursor_company_id=0,
        metadata_json=dict(metadata or {}),
        created_at=observed_at,
        updated_at=observed_at,
    )
    session.add(generation)
    session.flush()
    return generation, True


def ensure_operational_source_generations(
    session: Session, *, now: datetime | None = None
) -> int:
    """Create a pending generation when a newly registered source is operational."""

    observed_at = now or utc_now()
    datasets = tuple(
        session.scalars(
            select(DataSet)
            .where(
                DataSet.enabled.is_(True),
                DataSet.auto_update_status == "configured",
                DataSet.operational_status == "current",
                DataSet.last_success_at.is_not(None),
                DataSet.coverage["operational_accepted"].as_boolean().is_not(False),
            )
            .order_by(DataSet.code)
        )
    )
    created = 0
    for dataset in datasets:
        key = f"source-operational:{dataset.code}"
        if session.scalar(
            select(FactoryGeneration.id).where(
                FactoryGeneration.generation_key == key
            )
        ):
            continue
        state = session.get(WorkerPublicationState, _worker_source_id(dataset.code))
        session.add(
            FactoryGeneration(
                generation_type="source",
                generation_key=key,
                source_id=dataset.code,
                dataset_id=dataset.id,
                publication_generation=state.generation if state else None,
                status="pending",
                selection_complete=False,
                cursor_company_id=0,
                metadata_json={"trigger": "became_operational"},
                created_at=observed_at,
                updated_at=observed_at,
            )
        )
        created += 1
    if created:
        session.flush()
    return created


def _reconcile_source_members(
    session: Session, generation: FactoryGeneration, *, now: datetime
) -> None:
    members = tuple(
        session.scalars(
            select(FactoryGenerationCompany)
            .where(
                FactoryGenerationCompany.generation_id == generation.id,
                FactoryGenerationCompany.status == "scheduled",
            )
            .with_for_update(skip_locked=True)
        )
    )
    for member in members:
        run = (
            session.get(CompanyEnrichmentRun, member.enrichment_run_id)
            if member.enrichment_run_id
            else None
        )
        if run is None:
            member.status = "failed"
            member.last_error = "Source generation enrichment run is missing"
            member.completed_at = now
        elif run.status == "succeeded":
            member.status = "complete"
            member.risk_assessment_id = run.risk_assessment_id
            member.summary_id = run.summary_id
            member.last_error = None
            member.completed_at = run.finished_at or now
        elif run.status in {"failed", "cancelled"}:
            member.status = "failed"
            member.last_error = run.last_error or run.last_error_code
            member.completed_at = run.finished_at or now
        member.updated_at = now


def _schedule_source_members(
    session: Session,
    generation: FactoryGeneration,
    *,
    limit: int,
    now: datetime,
) -> tuple[int, int]:
    from app.services.company_enrichment_service import (
        create_enrichment_run,
        enqueue_enrichment_work,
    )

    dataset = (
        session.get(DataSet, generation.dataset_id)
        if generation.dataset_id is not None
        else None
    )
    if dataset is None:
        raise RuntimeError("Source generation dataset is missing")
    decision = source_applicability_expression(
        DataSet.applicability,
        Company.entity_type,
        Company.inn,
    ).label("applicability")
    rows = tuple(
        session.execute(
            select(Company.id.label("company_id"), decision)
            .select_from(Company)
            .join(DataSet, DataSet.id == dataset.id)
            .where(
                Company.id > generation.cursor_company_id,
                DataSet.id == dataset.id,
            )
            .order_by(Company.id)
            .limit(limit)
        )
    )
    run_ids: list[UUID] = []
    selected_count = 0
    for company_id, applicability in rows:
        if applicability == SourceApplicability.NOT_APPLICABLE.value:
            continue
        member = FactoryGenerationCompany(
            generation_id=generation.id,
            company_id=company_id,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        session.add(member)
        selected_count += 1
        if applicability == SourceApplicability.UNKNOWN.value:
            member.status = "applicability_unknown"
            member.last_error = "Company subject applicability is unresolved"
            continue
        try:
            creation = create_enrichment_run(
                session,
                company_id=company_id,
                trigger="source_generation",
                idempotency_key=(
                    f"source-generation:{generation.id}:{company_id}"
                ),
                source_ids=(str(generation.source_id),),
                now=now,
            )
        except ValueError as error:
            if "no applicable operational sources" not in str(error):
                raise
            member.status = "not_applicable"
            member.completed_at = now
            continue
        member.status = "scheduled"
        member.enrichment_run_id = creation.run.id
        run_ids.append(creation.run.id)
    if rows:
        generation.cursor_company_id = max(int(row.company_id) for row in rows)
        generation.selected_count += selected_count
    if len(rows) < limit:
        generation.selection_complete = True
    jobs_created = enqueue_enrichment_work(session, run_ids, now=now)
    generation.scheduled_count += len(run_ids)
    return jobs_created, len(run_ids)


def _latest_complete_run_query(*, now: datetime):
    ranked = (
        select(
            CompanyEnrichmentRun.id.label("run_id"),
            CompanyEnrichmentRun.company_id.label("company_id"),
            func.row_number()
            .over(
                partition_by=CompanyEnrichmentRun.company_id,
                order_by=(
                    CompanyEnrichmentRun.created_at.desc(),
                    CompanyEnrichmentRun.id.desc(),
                ),
            )
            .label("position"),
        )
        .where(
            CompanyEnrichmentRun.status == "succeeded",
            CompanyEnrichmentRun.completed_source_count
            == CompanyEnrichmentRun.source_count,
            CompanyEnrichmentRun.failed_source_count == 0,
            ~select(CompanySourceCoverage.id)
            .where(
                CompanySourceCoverage.enrichment_run_id
                == CompanyEnrichmentRun.id,
                CompanySourceCoverage.status == "APPLICABILITY_UNKNOWN",
            )
            .exists(),
            ~unresolved_replay_exists_clause(
                CompanyEnrichmentRun.company_id,
                CompanyEnrichmentRun.id,
                now=now,
            ),
        )
        .subquery()
    )
    return ranked


def _recalculate_ruleset_members(
    session: Session,
    generation: FactoryGeneration,
    *,
    limit: int,
    now: datetime,
) -> None:
    ranked = _latest_complete_run_query(now=now)
    rows = tuple(
        session.execute(
            select(ranked.c.company_id, ranked.c.run_id)
            .where(
                ranked.c.position == 1,
                ranked.c.company_id > generation.cursor_company_id,
            )
            .order_by(ranked.c.company_id)
            .limit(limit)
        )
    )
    for company_id, run_id in rows:
        member = FactoryGenerationCompany(
            generation_id=generation.id,
            company_id=company_id,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        session.add(member)
        try:
            assessment, _ = calculate_company_risk_v3_from_persisted(
                session, company_id, calculated_at=now
            )
            if isinstance(assessment, UnsupportedSubjectOutcome):
                member.status = "not_applicable"
                member.completed_at = now
                continue
            session.flush()
            risk = session.scalar(
                select(CompanyRiskAssessmentV3).where(
                    CompanyRiskAssessmentV3.assessment_id == assessment.assessment_id
                )
            )
            if risk is None:
                raise RuntimeError("Risk assessment was not persisted")
            summary, _ = get_or_create_summary_v3(
                session, risk.assessment_id, generated_at=now
            )
            session.flush()
            summary_row = session.scalar(
                select(CompanySummaryV3).where(
                    CompanySummaryV3.summary_id == summary.summary_id
                )
            )
            if summary_row is None:
                raise RuntimeError("Summary was not persisted")
            run = session.get(CompanyEnrichmentRun, run_id, with_for_update=True)
            if run is None:
                raise RuntimeError("Complete enrichment run is missing")
            run.risk_assessment_id = risk.assessment_id
            run.summary_id = summary_row.summary_id
            run.public_ready = True
            run.updated_at = now
            member.risk_assessment_id = risk.assessment_id
            member.summary_id = summary_row.summary_id
            member.status = "complete"
            member.completed_at = now
        except Exception as error:
            member.status = "failed"
            member.last_error = safe_error_message(error)
            member.completed_at = now
    if rows:
        generation.cursor_company_id = max(int(row.company_id) for row in rows)
        generation.selected_count += len(rows)
        generation.scheduled_count += len(rows)
    if len(rows) < limit:
        generation.selection_complete = True


def _refresh_counts(
    session: Session, generation: FactoryGeneration, *, now: datetime
) -> None:
    counts = dict(
        session.execute(
            select(
                FactoryGenerationCompany.status,
                func.count(FactoryGenerationCompany.id),
            )
            .where(FactoryGenerationCompany.generation_id == generation.id)
            .group_by(FactoryGenerationCompany.status)
        ).all()
    )
    generation.completed_count = int(counts.get("complete", 0)) + int(
        counts.get("not_applicable", 0)
    )
    generation.failed_count = int(counts.get("failed", 0))
    applicability_unknown = int(counts.get("applicability_unknown", 0))
    active = int(counts.get("pending", 0)) + int(counts.get("scheduled", 0))
    if generation.selection_complete and not active:
        generation.status = (
            "failed"
            if generation.failed_count or applicability_unknown
            else "complete"
        )
        if applicability_unknown and not generation.last_error:
            generation.last_error = (
                f"{applicability_unknown} company applicability decisions are unknown"
            )
        generation.completed_at = now
    else:
        generation.status = "running"
        generation.started_at = generation.started_at or now
    generation.updated_at = now


def process_factory_generations(
    session: Session,
    *,
    selection_limit: int = 100,
    source_selection_limit: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Advance one generation slice; caller owns transaction and restart."""

    if selection_limit <= 0:
        raise ValueError("selection_limit must be positive")
    if source_selection_limit is not None and source_selection_limit < 0:
        raise ValueError("source_selection_limit cannot be negative")
    observed_at = now or utc_now()
    generation = session.scalar(
        select(FactoryGeneration)
        .where(FactoryGeneration.status.in_(("pending", "running")))
        .order_by(
            case((FactoryGeneration.generation_type == "source", 0), else_=1),
            FactoryGeneration.created_at,
            FactoryGeneration.id,
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if generation is None:
        return {
            "generation_id": None,
            "jobs_created": 0,
            "runs_scheduled": 0,
            "status": "idle",
        }
    jobs_created = 0
    runs_scheduled = 0
    blocked_by_unknown_policy = False
    if generation.generation_type == "source":
        dataset = (
            session.get(DataSet, generation.dataset_id)
            if generation.dataset_id is not None
            else None
        )
        if dataset is None or parse_source_applicability(dataset.applicability) is None:
            blocked_by_unknown_policy = True
            generation.status = "failed"
            generation.selection_complete = True
            generation.last_error = "Source dataset applicability is unknown"
            generation.completed_at = observed_at
            generation.updated_at = observed_at
        else:
            _reconcile_source_members(session, generation, now=observed_at)
            source_limit = (
                selection_limit
                if source_selection_limit is None
                else source_selection_limit
            )
            if not generation.selection_complete and source_limit:
                jobs_created, runs_scheduled = _schedule_source_members(
                    session,
                    generation,
                    limit=source_limit,
                    now=observed_at,
                )
    elif not generation.selection_complete:
        _recalculate_ruleset_members(
            session,
            generation,
            limit=selection_limit,
            now=observed_at,
        )
    if not blocked_by_unknown_policy:
        _refresh_counts(session, generation, now=observed_at)
    applicability_unknown_count = int(
        session.scalar(
            select(func.count(FactoryGenerationCompany.id)).where(
                FactoryGenerationCompany.generation_id == generation.id,
                FactoryGenerationCompany.status == "applicability_unknown",
            )
        )
        or 0
    )
    return {
        "generation_id": str(generation.id),
        "generation_type": generation.generation_type,
        "source_id": generation.source_id,
        "status": generation.status,
        "selection_complete": generation.selection_complete,
        "selected_count": generation.selected_count,
        "scheduled_count": generation.scheduled_count,
        "completed_count": generation.completed_count,
        "failed_count": generation.failed_count,
        "applicability_unknown_count": applicability_unknown_count,
        "jobs_created": jobs_created,
        "runs_scheduled": runs_scheduled,
    }


def retry_factory_generation_failures(
    session: Session, generation_id: UUID, *, now: datetime | None = None
) -> int:
    """Reset failed source members without touching successful company work."""

    observed_at = now or utc_now()
    generation = session.get(FactoryGeneration, generation_id, with_for_update=True)
    if generation is None:
        raise LookupError("factory generation not found")
    if generation.generation_type != "source":
        raise ValueError("only source generation members have enrichment retries")
    failed = tuple(
        session.scalars(
            select(FactoryGenerationCompany)
            .where(
                FactoryGenerationCompany.generation_id == generation.id,
                FactoryGenerationCompany.status == "failed",
            )
            .with_for_update()
        )
    )
    from app.services.company_enrichment_service import restart_enrichment_run

    restarted = 0
    for member in failed:
        run = (
            session.get(CompanyEnrichmentRun, member.enrichment_run_id)
            if member.enrichment_run_id
            else None
        )
        if (
            run is None
            or run.status != "failed"
            or run.restart_count >= run.max_restarts
        ):
            continue
        restarted_run = restart_enrichment_run(session, run.id, now=observed_at)
        member.status = "scheduled"
        member.enrichment_run_id = restarted_run.id
        member.last_error = None
        member.completed_at = None
        member.updated_at = observed_at
        restarted += 1
    generation.status = "running"
    generation.failed_count = 0
    generation.completed_at = None
    generation.updated_at = observed_at
    return restarted
