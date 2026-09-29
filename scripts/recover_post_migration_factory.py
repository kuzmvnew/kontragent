#!/usr/bin/env python3
"""Plan, apply, or verify the controlled post-migration factory recovery."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from sqlalchemy import text


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database.postgres import SessionLocal  # noqa: E402
from app.services.dataset_applicability_sync_service import (  # noqa: E402
    apply_dataset_applicability_sync,
    plan_dataset_applicability_sync,
)
from app.services.post_migration_recovery_service import (  # noqa: E402
    ACTIVE_ENRICHMENT_STATUSES,
    apply_enrichment_recovery_batch,
    apply_stale_worker_recovery,
    evidence_counts,
    plan_enrichment_recovery_batch,
    plan_stale_worker_recovery,
    public_ready_count,
    reconcile_not_applicable_replay_batch,
    recovery_invariants,
    replay_blocker_counts,
    utc_now,
)
from app.worker.execution import RetryPolicy  # noqa: E402


EXPECTED_DATABASE_REVISION = "b9e2c4d6f8a0"


def _current_sha() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def _database_revision(session) -> str:
    return str(session.scalar(text("SELECT version_num FROM alembic_version")))


def _read_only(session) -> None:
    session.execute(text("SET TRANSACTION READ ONLY"))


def _enrichment_plan(session, *, batch_size: int) -> dict[str, Any]:
    cursor = None
    active_items: list[dict[str, Any]] = []
    actions: Counter[str] = Counter()
    candidates = 0
    while True:
        batch = plan_enrichment_recovery_batch(
            session,
            after_cursor=cursor,
            limit=batch_size,
        )
        if not batch.items:
            break
        candidates += batch.scanned
        actions.update(item.action for item in batch.items)
        active_items.extend(
            item.as_dict()
            for item in batch.items
            if item.status in ACTIVE_ENRICHMENT_STATUSES
        )
        cursor = batch.next_cursor
    return {
        "run_candidates": candidates,
        "action_counts": dict(sorted(actions.items())),
        "active_recovery_runs": active_items,
    }


def _plan(args: argparse.Namespace) -> dict[str, Any]:
    now = utc_now()
    retry_policy = RetryPolicy(
        base_delay_seconds=args.retry_base_seconds,
        max_delay_seconds=args.retry_max_seconds,
    )
    with SessionLocal() as session:
        _read_only(session)
        result = {
            "mode": "plan",
            "observed_at": now.isoformat(),
            "database_revision": _database_revision(session),
            "main_sha": _current_sha(),
            "applicability": plan_dataset_applicability_sync(session).as_dict(),
            "worker_stale": plan_stale_worker_recovery(
                session,
                stale_after=timedelta(seconds=args.stale_after_seconds),
                retry_policy=retry_policy,
                now=now,
                limit=args.batch_size,
            ).as_dict(),
            "enrichment": _enrichment_plan(session, batch_size=args.batch_size),
            "replay_blockers": replay_blocker_counts(session, now=now),
            "readiness_invariants": recovery_invariants(session, now=now),
            "public_ready": public_ready_count(session),
            "evidence_counts": evidence_counts(session),
        }
        session.rollback()
        return result


def _assert_apply_preconditions(session, args: argparse.Namespace) -> dict[str, str]:
    actual_revision = _database_revision(session)
    actual_sha = _current_sha()
    if actual_revision != args.expected_db_revision:
        raise RuntimeError(
            f"database revision mismatch: {actual_revision} != {args.expected_db_revision}"
        )
    if actual_sha != args.expected_main_sha:
        raise RuntimeError(f"main SHA mismatch: {actual_sha} != {args.expected_main_sha}")
    return {"database_revision": actual_revision, "main_sha": actual_sha}


def _apply_worker_recovery_batches(
    *,
    stale_after: timedelta,
    retry_policy: RetryPolicy,
    batch_size: int,
    now: datetime,
    session_factory=SessionLocal,
) -> dict[str, Any]:
    """Recover exact planned worker IDs in fresh bounded transactions."""

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    batches: list[dict[str, Any]] = []
    initial_total_stale = initial_total_recoverable = 0
    first_plan = True
    while True:
        with session_factory() as session:
            plan = plan_stale_worker_recovery(
                session,
                stale_after=stale_after,
                retry_policy=retry_policy,
                now=now,
                limit=batch_size,
            )
            if first_plan:
                initial_total_stale = plan.total_stale
                initial_total_recoverable = plan.total_recoverable
                first_plan = False
            planned_ids = tuple(item.run_id for item in plan.batch_candidates)
            if not planned_ids:
                session.rollback()
                break
            recovered_ids = apply_stale_worker_recovery(
                session,
                stale_after=stale_after,
                retry_policy=retry_policy,
                now=now,
                candidate_run_ids=planned_ids,
                limit=batch_size,
            )
            unexpected = set(recovered_ids) - set(planned_ids)
            if unexpected or len(recovered_ids) > batch_size:
                raise RuntimeError("worker recovery exceeded its planned batch")
            session.commit()
        batches.append(
            {
                "planned_run_ids": [str(value) for value in planned_ids],
                "recovered_run_ids": [str(value) for value in recovered_ids],
                "recovered": len(recovered_ids),
            }
        )
        if not recovered_ids:
            raise RuntimeError("planned worker batch made no progress")
    all_recovered_ids = [
        run_id
        for batch in batches
        for run_id in batch["recovered_run_ids"]
    ]
    return {
        "initial_total_stale": initial_total_stale,
        "initial_total_recoverable": initial_total_recoverable,
        "batch_size": batch_size,
        "batches": batches,
        "recovered": len(all_recovered_ids),
        "run_ids": all_recovered_ids,
    }


def _apply(args: argparse.Namespace) -> dict[str, Any]:
    now = utc_now()
    retry_policy = RetryPolicy(
        base_delay_seconds=args.retry_base_seconds,
        max_delay_seconds=args.retry_max_seconds,
    )
    with SessionLocal() as session:
        preconditions = _assert_apply_preconditions(session, args)
        before = {
            "invariants": recovery_invariants(session, now=now),
            "public_ready": public_ready_count(session),
            "evidence_counts": evidence_counts(session),
            "replay_blockers": replay_blocker_counts(session, now=now),
        }
        applicability = apply_dataset_applicability_sync(session)
        session.commit()

    worker_recovery = _apply_worker_recovery_batches(
        stale_after=timedelta(seconds=args.stale_after_seconds),
        retry_policy=retry_policy,
        batch_size=args.batch_size,
        now=now,
    )

    replay_totals = Counter()
    while True:
        with SessionLocal() as session:
            batch = reconcile_not_applicable_replay_batch(
                session, limit=args.batch_size, now=now
            )
            session.commit()
        replay_totals.update(batch)
        if batch["signals_reconciled"] == 0:
            break

    enrichment_totals: Counter[str] = Counter()
    cursor = None
    while True:
        with SessionLocal() as session:
            batch = apply_enrichment_recovery_batch(
                session,
                after_cursor=cursor,
                limit=args.batch_size,
                now=now,
            )
            session.commit()
        if not batch.items:
            break
        enrichment_totals["batches"] += 1
        enrichment_totals["scanned"] += batch.scanned
        enrichment_totals["changed"] += batch.changed
        enrichment_totals.update(
            {f"action_{key}": value for key, value in batch.reasons.items()}
        )
        for item in batch.items:
            if item.public_ready and item.action in {
                "INVALIDATE_AND_PARK",
                "BLOCKED_APPLICABILITY",
                "BLOCKED_FAILED_REPLAY",
            }:
                enrichment_totals["demoted"] += 1
                enrichment_totals[f"demotion_{item.action}"] += 1
            elif not item.public_ready and item.action == "RECONCILE_TO_COMPLETE":
                enrichment_totals["promoted"] += 1
        cursor = batch.next_cursor

    with SessionLocal() as session:
        after = {
            "invariants": recovery_invariants(session, now=now),
            "public_ready": public_ready_count(session),
            "evidence_counts": evidence_counts(session),
            "replay_blockers": replay_blocker_counts(session, now=now),
            "applicability": plan_dataset_applicability_sync(session).as_dict(),
        }
        session.rollback()
    before_ready = int(before["public_ready"])
    after_ready = int(after["public_ready"])
    return {
        "mode": "apply",
        "observed_at": now.isoformat(),
        "preconditions": preconditions,
        "before": before,
        "applicability": applicability.as_dict(),
        "worker": worker_recovery,
        "not_applicable_replay": dict(replay_totals),
        "enrichment": dict(enrichment_totals),
        "after": after,
        "readiness_recalculation": {
            "public_ready_before": before_ready,
            "public_ready_after": after_ready,
            "demoted": enrichment_totals["demoted"],
            "promoted": enrichment_totals["promoted"],
            "net_change": after_ready - before_ready,
            "reason_distribution": {
                key.removeprefix("demotion_"): value
                for key, value in enrichment_totals.items()
                if key.startswith("demotion_")
            },
        },
    }


def _verify() -> dict[str, Any]:
    now = utc_now()
    with SessionLocal() as session:
        _read_only(session)
        applicability = plan_dataset_applicability_sync(session)
        invariants = recovery_invariants(session, now=now)
        applicability_pass = (
            applicability.totals["missing_before"] == 0
            and applicability.totals["invalid_before"] == 0
            and applicability.totals["unmapped"] == 0
            and applicability.totals["valid_before"]
            == applicability.totals["total"]
        )
        invariants_pass = all(value == 0 for value in invariants.values())
        result = {
            "mode": "verify",
            "observed_at": now.isoformat(),
            "database_revision": _database_revision(session),
            "main_sha": _current_sha(),
            "applicability": applicability.as_dict(),
            "replay_blockers": replay_blocker_counts(session, now=now),
            "readiness_invariants": invariants,
            "required_applicability_pass": applicability_pass,
            "required_zero_invariants_pass": invariants_pass,
            "recovery_verified": applicability_pass and invariants_pass,
            "evidence_counts": evidence_counts(session),
        }
        session.rollback()
        return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed post-migration factory recovery"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan", action="store_true", help="strictly read-only plan")
    modes.add_argument("--apply", action="store_true", help="explicit controlled apply")
    modes.add_argument("--verify", action="store_true", help="read-only postcondition check")
    parser.add_argument("--expected-db-revision")
    parser.add_argument("--expected-main-sha")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--stale-after-seconds", type=int, default=300)
    parser.add_argument("--retry-base-seconds", type=int, default=30)
    parser.add_argument("--retry-max-seconds", type=int, default=900)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.batch_size <= 0 or args.stale_after_seconds <= 0:
        parser.error("batch size and stale threshold must be positive")
    if args.apply:
        if not args.expected_db_revision or not args.expected_main_sha:
            parser.error("--apply requires --expected-db-revision and --expected-main-sha")
        if args.expected_db_revision != EXPECTED_DATABASE_REVISION:
            parser.error(
                f"expected DB revision must be {EXPECTED_DATABASE_REVISION} for this package"
            )
    try:
        result = _apply(args) if args.apply else _plan(args) if args.plan else _verify()
    except Exception as error:
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "plan" if args.plan else "verify",
                    "status": "FAILED",
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.verify and not result["recovery_verified"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
