"""Benchmark 100/500/1000/5000-company set-based enrichment joins."""

from __future__ import annotations

import argparse
import json

from app.database.postgres import SessionLocal
from app.services.factory_benchmark_service import (
    benchmark_set_based_batches,
    verify_benchmark_failure_recovery,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--batch-size", action="append", type=int, dest="batch_sizes"
    )
    parser.add_argument("--verify-failure-recovery", action="store_true")
    args = parser.parse_args()
    sizes = tuple(args.batch_sizes or (100, 500, 1_000, 5_000))
    with SessionLocal() as session:
        results = benchmark_set_based_batches(session, batch_sizes=sizes)
        session.rollback()
        recovered = (
            verify_benchmark_failure_recovery(session)
            if args.verify_failure_recovery
            else None
        )
        session.rollback()
    print(
        json.dumps(
            {
                "mode": "read_only_set_based_exact_identity",
                "batches": results,
                "failure_recovery_pass": recovered,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
