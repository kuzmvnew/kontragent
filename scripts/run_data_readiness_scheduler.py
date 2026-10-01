"""Run the data-readiness scheduler once or as a supervised worker."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json

from sqlalchemy import func, select

from app.database.base import Base
from app.database.postgres import SessionLocal
from app.ingestion.cbr_finorg_worker import register_cbr_finorg_worker
from app.ingestion.cbr_warning_worker import register_cbr_warning_worker
from app.ingestion.erknm_worker import register_erknm_worker
from app.ingestion.fns_headcount import register_fns_headcount_worker
from app.ingestion.fns_disqualified import register_fns_disqualified_worker
from app.ingestion.fns_msp import register_fns_msp_worker
from app.ingestion.fns_sme_support import register_fns_sme_support_worker
from app.ingestion.fns_revenue_expense import register_fns_revenue_expense_worker
from app.ingestion.fns_tax_debt_pipeline import (
    register_fns_tax_debt_controlled_live_handler,
)
from app.ingestion.fns_tax_offence import register_fns_tax_offence_worker
from app.ingestion.fns_tax_payment import register_fns_tax_payment_worker
from app.ingestion.fns_tax_regime import register_fns_tax_regime_worker
from app.ingestion.fns_registry_master import register_fns_registry_workers
from app.ingestion.girbo_worker import register_girbo_worker
from app.ingestion.firmoteka_worker import register_firmoteka_worker
from app.ingestion.exact_source_workers import (
    register_eis_rnp_worker,
    register_exact_source_workers,
)
from app.ingestion.roskomnadzor_bulk_worker import register_rkn_bulk_workers
from app.ingestion.mintrans_ted_worker import register_mintrans_ted_worker
from app.ingestion.next_five_source_workers import register_next_five_workers
from app.ingestion.roszdrav_license_worker import register_roszdrav_license_workers
from app.services.cbr_warning_registry_service import (
    ensure_cbr_warning_list_dataset,
)
from app.services.cbr_finorg_registry_service import ensure_cbr_finorg_dataset
from app.services.fns_sme_support_registry_service import (
    ensure_fns_sme_support_dataset,
)
from app.services.erknm_registry_service import ensure_erknm_dataset
from app.services.source_service import ensure_default_dataset
from app.services.source_factory_registry_service import ensure_source_factory_datasets
from app.services.company_enrichment_service import run_company_enrichment_cycle
from app.services.factory_scale_service import FactoryScaleConfig
from app.services.roszdrav_registry_service import ensure_roszdrav_datasets
from app.services.data_readiness_scheduler import (
    FNS_BULK_DATASET_CODES,
    ROSZDRAV_LICENSE_DATASET_CODES,
    SCHEDULED_SOURCE_DATASET_CODES,
    configure_fns_bulk_schedules,
    configure_source_schedules,
    run_due_updates,
    sync_worker_failure_signals,
    worker_loop,
)
from app.worker.errors import HandlerNotRegisteredError, WorkerFoundationError
from app.worker.execution import (
    FACTORY_LANES,
    RetryPolicy,
    WorkerExecutor,
    claim_next_job,
    recover_stale_runs,
)
from app.models.firmoteka import FirmotekaCrawlRun
from app.models.worker import WorkerJob
from app.worker.registry import HandlerRegistry


CONTROLLED_DEFAULT_WORK_BUDGET = 20
WORKER_JOB_STATUSES = (
    "queued",
    "running",
    "retry_scheduled",
    "succeeded",
    "failed",
    "cancelled",
)


def build_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    with SessionLocal() as session:
        # Metadata registration is idempotent and never activates schedules.
        # This also makes all adapters visible in the operations console.
        ensure_source_factory_datasets(session)
        # Sources are deliberately registered in scheduler priority order.
        # Each retains an independent source id, lease, job namespace and
        # publication generation.
        register_firmoteka_worker(session, registry)
        # Registration exposes fail-closed source contracts only.  These five
        # families have no scheduler handlers until source-specific access and
        # baselines are accepted explicitly.
        register_next_five_workers(session, registry)
        register_exact_source_workers(session, registry)
        register_eis_rnp_worker(session, registry)
        register_rkn_bulk_workers(session, registry)
        register_fns_tax_offence_worker(session, registry)
        register_fns_revenue_expense_worker(session, registry)
        # S02 is registered only after its explicit durable cohort approval.
        try:
            register_fns_tax_debt_controlled_live_handler(session, registry)
        except HandlerNotRegisteredError:
            pass
        register_fns_tax_payment_worker(session, registry)
        register_cbr_finorg_worker(session, registry)
        register_cbr_warning_worker(session, registry)
        register_fns_headcount_worker(session, registry)
        register_fns_msp_worker(session, registry)
        register_fns_tax_regime_worker(session, registry)
        register_fns_sme_support_worker(session, registry)
        register_fns_disqualified_worker(session, registry)
        register_fns_registry_workers(session, registry)
        register_mintrans_ted_worker(session, registry)
        register_girbo_worker(session, registry)
        register_erknm_worker(session, registry)
        register_roszdrav_license_workers(session, registry)
        session.commit()
    return registry


def _jobs_by_status(session) -> dict[str, int]:
    counts = {
        str(status): int(count)
        for status, count in session.execute(
            select(WorkerJob.status, func.count())
            .group_by(WorkerJob.status)
            .order_by(WorkerJob.status)
        )
    }
    return {status: counts.get(status, 0) for status in WORKER_JOB_STATUSES}


def _table_family_count(session, *, names: tuple[str, ...], suffix: str) -> int:
    tables = tuple(
        table
        for table in Base.metadata.tables.values()
        if table.name in names or table.name.endswith(suffix)
    )
    return sum(
        int(session.scalar(select(func.count()).select_from(table)) or 0)
        for table in tables
    )


def _firmoteka_crawl_state(session) -> dict[str, dict[str, object]]:
    rows = tuple(
        session.scalars(
            select(FirmotekaCrawlRun).order_by(FirmotekaCrawlRun.id)
        )
    )
    return {
        str(row.id): {
            "status": row.status,
            "phase": row.phase,
            "cursor": dict(row.cursor or {}),
            "request_count": int(row.request_count),
            "success_count": int(row.success_count),
            "failure_count": int(row.failure_count),
            "retry_count": int(row.retry_count),
            "catalog_discovered": int(row.catalog_discovered),
            "company_discovered": int(row.company_discovered),
            "fetched_count": int(row.fetched_count),
            "parsed_count": int(row.parsed_count),
            "valid_count": int(row.valid_count),
            "last_checkpoint_at": (
                row.last_checkpoint_at.isoformat()
                if row.last_checkpoint_at is not None
                else None
            ),
            "completed_at": (
                row.completed_at.isoformat()
                if row.completed_at is not None
                else None
            ),
        }
        for row in rows
    }


def capture_controlled_state(session) -> dict[str, object]:
    """Capture bounded-start evidence without changing durable state."""

    return {
        "jobs_by_status": _jobs_by_status(session),
        "worker_job_count": int(
            session.scalar(select(func.count()).select_from(WorkerJob)) or 0
        ),
        "firmoteka_job_count": int(
            session.scalar(
                select(func.count())
                .select_from(WorkerJob)
                .where(WorkerJob.source_id == "firmoteka")
            )
            or 0
        ),
        "raw_count": _table_family_count(
            session,
            names=("worker_raw_manifests",),
            suffix="_raw_artifacts",
        ),
        "snapshot_count": _table_family_count(
            session,
            names=(),
            suffix="_snapshots",
        ),
        "firmoteka_crawls": _firmoteka_crawl_state(session),
    }


def _firmoteka_crawl_delta(
    before: dict[str, dict[str, object]],
    after: dict[str, dict[str, object]],
) -> dict[str, object]:
    before_ids = set(before)
    after_ids = set(after)
    return {
        "count_delta": len(after) - len(before),
        "created_ids": sorted(after_ids - before_ids),
        "removed_ids": sorted(before_ids - after_ids),
        "advanced_ids": sorted(
            crawl_id
            for crawl_id in before_ids & after_ids
            if before[crawl_id] != after[crawl_id]
        ),
    }


def run_controlled_interval(
    registry: HandlerRegistry,
    *,
    allowed_source_ids: tuple[str, ...] | list[str],
    allowed_lanes: tuple[str, ...] | list[str] = (),
    work_budget: int = CONTROLLED_DEFAULT_WORK_BUDGET,
) -> dict[str, object]:
    """Recover stale DB state, execute one admitted budget, and exit.

    This path intentionally never calls ``run_due_updates`` or
    ``run_company_enrichment_cycle``.  Registration has already happened, but
    scheduling, Firmoteka admission and Master enrichment remain off unless an
    existing job is explicitly admitted by both source and lane.
    """

    if not isinstance(work_budget, int) or isinstance(work_budget, bool) or work_budget < 0:
        raise ValueError("work_budget must be a non-negative integer")
    sources = tuple(dict.fromkeys(source.strip() for source in allowed_source_ids))
    if any(not source for source in sources):
        raise ValueError("allowed source ids must be non-empty")
    lanes = tuple(dict.fromkeys(allowed_lanes))
    if not set(lanes) <= set(FACTORY_LANES):
        raise ValueError("unknown factory lane")

    with SessionLocal() as session:
        before = capture_controlled_state(session)
        recovered = recover_stale_runs(
            session,
            stale_after=timedelta(minutes=2),
            retry_policy=RetryPolicy(),
        )
        session.commit()

    remaining = work_budget
    claims = []
    completed_run_ids: list[str] = []
    execution_errors: list[dict[str, str]] = []
    executor = WorkerExecutor(
        session_factory=SessionLocal,
        registry=registry,
        worker_id="data-readiness-controlled-start",
    )
    while remaining > 0 and sources and lanes:
        with SessionLocal() as session:
            claim = claim_next_job(
                session,
                registry,
                worker_id="data-readiness-controlled-start",
                lease_ttl=executor.lease_ttl,
                allowed_lanes=lanes,
                allowed_source_ids=sources,
                max_work_units=remaining,
            )
            if claim is None:
                session.rollback()
                break
            session.commit()
        claims.append(claim)
        remaining -= claim.work_units
        try:
            completed_run_ids.append(str(executor.execute_claim(claim)))
        except WorkerFoundationError as error:
            execution_errors.append(
                {
                    "run_id": str(claim.run_id),
                    "source_id": claim.source_id,
                    "kind": error.kind.value,
                    "message": str(error),
                }
            )
            sync_worker_failure_signals()

    with SessionLocal() as session:
        after = capture_controlled_state(session)

    source_counts = Counter(claim.source_id for claim in claims)
    firmoteka_claims = tuple(
        claim for claim in claims if claim.source_id == "firmoteka"
    )
    firmoteka_child_items = sum(
        max(
            (
                len(value)
                for key in ("items", "catalog_pages")
                if isinstance(
                    value := claim.schedule_metadata.get(key), (list, tuple)
                )
            ),
            default=0,
        )
        for claim in firmoteka_claims
    )
    firmoteka_before = before["firmoteka_crawls"]
    firmoteka_after = after["firmoteka_crawls"]
    assert isinstance(firmoteka_before, dict) and isinstance(firmoteka_after, dict)
    return {
        "mode": "controlled_start",
        "scheduler_admission": "off",
        "scheduler_jobs_created": 0,
        "jobs_created_total": (
            int(after["worker_job_count"]) - int(before["worker_job_count"])
        ),
        "allowed_sources": list(sources),
        "allowed_lanes": list(lanes),
        "work_budget": work_budget,
        "jobs_before_by_status": before["jobs_by_status"],
        "jobs_after_by_status": after["jobs_by_status"],
        "runs_created": len(claims),
        "worker_run_ids": [str(claim.run_id) for claim in claims],
        "completed_run_ids": completed_run_ids,
        "sources_claimed": dict(sorted(source_counts.items())),
        "work_units_admitted": sum(claim.work_units for claim in claims),
        "work_units_remaining": remaining,
        "firmoteka_jobs_created": (
            int(after["firmoteka_job_count"])
            - int(before["firmoteka_job_count"])
        ),
        "firmoteka_jobs_claimed": len(firmoteka_claims),
        "firmoteka_work_units_admitted": sum(
            claim.work_units for claim in firmoteka_claims
        ),
        "firmoteka_child_items_admitted": firmoteka_child_items,
        # The automatic Master enrichment cycle is not invoked in this mode.
        "automatic_enrichment": "off",
        "enrichment_mutations": 0,
        "stale_runs_recovered": {
            "count": len(recovered),
            "run_ids": [str(run_id) for run_id in recovered],
        },
        "raw_delta": int(after["raw_count"]) - int(before["raw_count"]),
        "snapshot_delta": (
            int(after["snapshot_count"]) - int(before["snapshot_count"])
        ),
        "firmoteka_crawl_delta": _firmoteka_crawl_delta(
            firmoteka_before, firmoteka_after
        ),
        "execution_errors": execution_errors,
    }


def run_workers(registry: HandlerRegistry, *, max_jobs: int) -> list[str]:
    # A supervised process can be killed after a job is claimed but before its
    # normal timeout/failure transaction runs.  Recover those durable rows on
    # every polling cycle so a service restart cannot strand a source forever
    # in ``running``.  The normal retry policy remains authoritative and the
    # fencing token prevents the interrupted process from publishing later.
    with SessionLocal() as session:
        recover_stale_runs(
            session,
            stale_after=timedelta(minutes=2),
            retry_policy=RetryPolicy(),
        )
        # Consume a bounded Master slice in the same supervised process.  This
        # is durable DB work: a restart simply resumes pending coverage/jobs.
        run_company_enrichment_cycle(
            session,
            signal_limit=max(10, max_jobs * 25),
            reconcile_limit=max(50, max_jobs * 50),
        )
        session.commit()
    config = FactoryScaleConfig.from_environment()
    budgets = config.lane_budgets()
    lane_order = tuple(budgets)
    bounded = {lane: 0 for lane in lane_order}
    remaining = max(0, max_jobs)
    while remaining and any(bounded[lane] < budgets[lane] for lane in lane_order):
        for lane in lane_order:
            if not remaining:
                break
            if bounded[lane] >= budgets[lane]:
                continue
            bounded[lane] += 1
            remaining -= 1

    def run_lane(lane: str, budget: int) -> list[str]:
        executor = WorkerExecutor(
            session_factory=SessionLocal,
            registry=registry,
            worker_id=f"data-readiness-supervisor:{lane}",
        )
        lane_completed: list[str] = []
        for _ in range(budget):
            try:
                run_id = executor.run_once(allowed_lanes=(lane,))
            except WorkerFoundationError:
                sync_worker_failure_signals()
                continue
            if run_id is None:
                break
            lane_completed.append(str(run_id))
        return lane_completed

    completed: list[str] = []
    active_budgets = {lane: budget for lane, budget in bounded.items() if budget}
    if active_budgets:
        with ThreadPoolExecutor(
            max_workers=len(active_budgets), thread_name_prefix="factory-lane"
        ) as pool:
            futures = {
                lane: pool.submit(run_lane, lane, budget)
                for lane, budget in active_budgets.items()
            }
            for lane in lane_order:
                if lane in futures:
                    completed.extend(futures[lane].result())
    # A completed Worker job can satisfy the last frozen source expectation;
    # reconcile immediately instead of waiting for the next polling minute.
    with SessionLocal() as session:
        run_company_enrichment_cycle(
            session,
            signal_limit=max(10, max_jobs * 25),
            reconcile_limit=max(50, max_jobs * 50),
        )
        session.commit()
    return completed


def prepare_worker_state(*, max_jobs: int) -> dict[str, object]:
    """Recover durable state before any potentially slow source discovery.

    ``worker_loop`` normally runs Worker Foundation maintenance after source
    scheduling.  A slow official endpoint must not delay restart recovery or
    the bounded Master enrichment queue, so the supervised entry point performs
    this DB-only preparation once before entering that loop.  It deliberately
    does not claim a source job; the single registered executor remains the
    only network-processing path.
    """

    with SessionLocal() as session:
        recovered = recover_stale_runs(
            session,
            stale_after=timedelta(minutes=2),
            retry_policy=RetryPolicy(),
        )
        enrichment = run_company_enrichment_cycle(
            session,
            signal_limit=max(10, max_jobs * 25),
            reconcile_limit=max(50, max_jobs * 50),
        )
        session.commit()
    return {
        "recovered_run_ids": tuple(str(run_id) for run_id in recovered),
        "enrichment": enrichment,
    }


def ensure_activation_datasets(dataset_codes: list[str]) -> None:
    """Create control rows needed by explicitly requested activations only."""

    if "cbr_warning_list" in dataset_codes:
        ensure_cbr_warning_list_dataset()
    if "cbr_finorg" in dataset_codes:
        ensure_cbr_finorg_dataset()
    if "fns_tax_regime" in dataset_codes:
        ensure_default_dataset("fns_tax_regime")
    if "fns_sme_support" in dataset_codes:
        ensure_fns_sme_support_dataset()
    if "fns_disqualified" in dataset_codes:
        ensure_default_dataset("fns_disqualified")
    if "erknm_inspections" in dataset_codes:
        ensure_erknm_dataset()
    if set(dataset_codes) & ROSZDRAV_LICENSE_DATASET_CODES:
        ensure_roszdrav_datasets()
    if set(dataset_codes) & {
        "firmoteka", "fns_npd", "nostroy_sro_members_on_demand",
        "nopriz_sro_members_on_demand", "prime_corporate_disclosure", "eis_rnp",
        "rkn_personal_data_operators",
        "rkn_communications_licenses", "rkn_broadcast_licenses",
        "rkn_registered_media", "rkn_information_distributors",
        "rkn_hosting_providers",
        "fns_egrul", "fns_egrip", "mintrans_ted_registry", "girbo_accounting"
    }:
        with SessionLocal() as session:
            ensure_source_factory_datasets(session)
            session.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run a supervised polling loop")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--max-jobs", type=int, default=2)
    parser.add_argument(
        "--controlled-start",
        action="store_true",
        help=(
            "Run exactly one fail-closed interval: scheduler and automatic "
            "enrichment are off"
        ),
    )
    parser.add_argument(
        "--allow-source",
        action="append",
        default=[],
        metavar="SOURCE_ID",
        help=(
            "Admit an existing queued source in controlled mode; repeat for "
            "additional sources (Firmoteka requires --allow-source firmoteka)"
        ),
    )
    parser.add_argument(
        "--allow-lane",
        action="append",
        choices=FACTORY_LANES,
        default=[],
        metavar="LANE",
        help=(
            "Admit one factory lane in controlled mode; at least one source "
            "and one lane are required before any job can be claimed"
        ),
    )
    parser.add_argument(
        "--work-budget",
        type=int,
        default=CONTROLLED_DEFAULT_WORK_BUDGET,
        metavar="UNITS",
        help="Maximum real work units admitted in one controlled interval (default: 20)",
    )
    parser.add_argument(
        "--activate-s03-s04",
        action="store_true",
        help="Deprecated compatibility gate: enable both S04/S03 schedules",
    )
    parser.add_argument(
        "--activate",
        action="append",
        choices=sorted(SCHEDULED_SOURCE_DATASET_CODES),
        default=[],
        metavar="SOURCE_ID",
        help="Enable one source schedule; repeat to enable more than one",
    )
    parser.add_argument(
        "--deactivate",
        action="append",
        choices=sorted(SCHEDULED_SOURCE_DATASET_CODES),
        default=[],
        metavar="SOURCE_ID",
        help="Disable one source schedule; repeat to disable more than one",
    )
    parser.add_argument(
        "--activate-roszdrav-licenses",
        action="store_true",
        help="Enable the three distinct Roszdrav licence schedules as one family",
    )
    parser.add_argument(
        "--deactivate-roszdrav-licenses",
        action="store_true",
        help="Disable the three distinct Roszdrav licence schedules as one family",
    )
    args = parser.parse_args()
    if args.work_budget < 0:
        parser.error("--work-budget must be non-negative")
    if not args.controlled_start and (args.allow_source or args.allow_lane):
        parser.error("--allow-source/--allow-lane require --controlled-start")
    if args.controlled_start and args.loop:
        parser.error("--controlled-start is one-shot and cannot be combined with --loop")
    if args.controlled_start and bool(args.allow_source) != bool(args.allow_lane):
        parser.error(
            "controlled execution requires both --allow-source and --allow-lane"
        )
    if args.controlled_start and (
        args.activate_s03_s04
        or args.activate
        or args.deactivate
        or args.activate_roszdrav_licenses
        or args.deactivate_roszdrav_licenses
    ):
        parser.error("controlled startup cannot change scheduler activation")
    overlap = set(args.activate) & set(args.deactivate)
    if overlap:
        parser.error("cannot activate and deactivate the same source: " + ", ".join(sorted(overlap)))
    if args.activate_s03_s04 and (
        args.activate
        or args.deactivate
        or args.activate_roszdrav_licenses
        or args.deactivate_roszdrav_licenses
    ):
        parser.error("--activate-s03-s04 cannot be combined with source-scoped gates")
    if args.activate_roszdrav_licenses and args.deactivate_roszdrav_licenses:
        parser.error("cannot activate and deactivate Roszdrav licence family together")
    family_overlap = set(args.activate) & ROSZDRAV_LICENSE_DATASET_CODES
    if family_overlap and (
        args.activate_roszdrav_licenses or args.deactivate_roszdrav_licenses
    ):
        parser.error(
            "Roszdrav family gate cannot be combined with individual Roszdrav sources"
        )
    family_overlap = set(args.deactivate) & ROSZDRAV_LICENSE_DATASET_CODES
    if family_overlap and (
        args.activate_roszdrav_licenses or args.deactivate_roszdrav_licenses
    ):
        parser.error(
            "Roszdrav family gate cannot be combined with individual Roszdrav sources"
        )
    registry = build_registry()
    if args.controlled_start:
        report = run_controlled_interval(
            registry,
            allowed_source_ids=args.allow_source,
            allowed_lanes=args.allow_lane,
            work_budget=args.work_budget,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if args.activate_s03_s04:
        configure_fns_bulk_schedules(
            enabled=True,
            dataset_codes=("fns_tax_offence", "fns_revenue_expenses"),
        )
    if args.activate:
        # Registration is deliberately tied to explicit activation; a
        # supervisor restart must not silently enable a new source.  Existing
        # production databases predate the tax-regime family control row; its
        # scoped upsert never enables the child projection datasets.
        ensure_activation_datasets(args.activate)
        configure_source_schedules(enabled=True, dataset_codes=args.activate)
    if args.activate_roszdrav_licenses:
        family_codes = sorted(ROSZDRAV_LICENSE_DATASET_CODES)
        ensure_activation_datasets(family_codes)
        configure_source_schedules(enabled=True, dataset_codes=family_codes)
    if args.deactivate:
        configure_source_schedules(enabled=False, dataset_codes=args.deactivate)
    if args.deactivate_roszdrav_licenses:
        configure_source_schedules(
            enabled=False,
            dataset_codes=sorted(ROSZDRAV_LICENSE_DATASET_CODES),
        )
    if args.loop:
        prepare_worker_state(max_jobs=args.max_jobs)
        worker_loop(
            poll_seconds=args.poll_seconds,
            after_poll=lambda: run_workers(registry, max_jobs=args.max_jobs),
        )
    else:
        scheduled = run_due_updates()
        completed = run_workers(registry, max_jobs=args.max_jobs)
        print(json.dumps({"scheduled": scheduled, "worker_run_ids": completed}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
