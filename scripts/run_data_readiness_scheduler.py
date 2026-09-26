"""Run the data-readiness scheduler once or as a supervised worker."""

import argparse
from datetime import timedelta
import json

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
    RetryPolicy,
    WorkerExecutor,
    recover_stale_runs,
)
from app.worker.registry import HandlerRegistry


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
    executor = WorkerExecutor(
        session_factory=SessionLocal,
        registry=registry,
        worker_id="data-readiness-supervisor",
    )
    completed: list[str] = []
    for _ in range(max_jobs):
        try:
            run_id = executor.run_once()
        except WorkerFoundationError:
            sync_worker_failure_signals()
            continue
        if run_id is None:
            break
        completed.append(str(run_id))
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
