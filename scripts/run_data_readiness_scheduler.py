"""Run the data-readiness scheduler once or as a supervised worker."""

import argparse
import json

from app.database.postgres import SessionLocal
from app.ingestion.cbr_warning_worker import register_cbr_warning_worker
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
from app.services.cbr_warning_registry_service import (
    ensure_cbr_warning_list_dataset,
)
from app.services.fns_sme_support_registry_service import (
    ensure_fns_sme_support_dataset,
)
from app.services.source_service import ensure_default_dataset
from app.services.data_readiness_scheduler import (
    FNS_BULK_DATASET_CODES,
    SCHEDULED_SOURCE_DATASET_CODES,
    configure_fns_bulk_schedules,
    configure_source_schedules,
    run_due_updates,
    sync_worker_failure_signals,
    worker_loop,
)
from app.worker.errors import HandlerNotRegisteredError, WorkerFoundationError
from app.worker.execution import WorkerExecutor
from app.worker.registry import HandlerRegistry


def build_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    with SessionLocal() as session:
        # Sources are deliberately registered in scheduler priority order.
        # Each retains an independent source id, lease, job namespace and
        # publication generation.
        register_fns_tax_offence_worker(session, registry)
        register_fns_revenue_expense_worker(session, registry)
        # S02 is registered only after its explicit durable cohort approval.
        try:
            register_fns_tax_debt_controlled_live_handler(session, registry)
        except HandlerNotRegisteredError:
            pass
        register_fns_tax_payment_worker(session, registry)
        register_cbr_warning_worker(session, registry)
        register_fns_headcount_worker(session, registry)
        register_fns_msp_worker(session, registry)
        register_fns_tax_regime_worker(session, registry)
        register_fns_sme_support_worker(session, registry)
        register_fns_disqualified_worker(session, registry)
        session.commit()
    return registry


def run_workers(registry: HandlerRegistry, *, max_jobs: int) -> list[str]:
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
    return completed


def ensure_activation_datasets(dataset_codes: list[str]) -> None:
    """Create control rows needed by explicitly requested activations only."""

    if "cbr_warning_list" in dataset_codes:
        ensure_cbr_warning_list_dataset()
    if "fns_tax_regime" in dataset_codes:
        ensure_default_dataset("fns_tax_regime")
    if "fns_sme_support" in dataset_codes:
        ensure_fns_sme_support_dataset()
    if "fns_disqualified" in dataset_codes:
        ensure_default_dataset("fns_disqualified")


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
    args = parser.parse_args()
    overlap = set(args.activate) & set(args.deactivate)
    if overlap:
        parser.error("cannot activate and deactivate the same source: " + ", ".join(sorted(overlap)))
    if args.activate_s03_s04 and (args.activate or args.deactivate):
        parser.error("--activate-s03-s04 cannot be combined with source-scoped gates")
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
    if args.deactivate:
        configure_source_schedules(enabled=False, dataset_codes=args.deactivate)
    if args.loop:
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
