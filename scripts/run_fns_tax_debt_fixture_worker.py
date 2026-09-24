"""Run one caller-supplied S02 fixture through the DEV-009 worker pipeline."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from app.database.postgres import SessionLocal
from app.ingestion.fns_tax_debt_pipeline import (
    enqueue_fns_tax_debt_fixture_job,
    register_fns_tax_debt_handler,
)
from app.worker.execution import WorkerExecutor
from app.worker.registry import HandlerRegistry


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must contain a timezone")
    return parsed.astimezone(timezone.utc)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Run a local official-format FNS debtam fixture via S02 worker"
    )
    parser.add_argument("zip_path", type=Path)
    parser.add_argument("--artifact-store", type=Path, required=True)
    parser.add_argument("--source-as-of", type=_timestamp, required=True)
    parser.add_argument(
        "--retrieved-at",
        type=_timestamp,
        default=datetime.now(timezone.utc),
    )
    parser.add_argument("--expected-sha256")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    registry = HandlerRegistry()
    with SessionLocal() as session:
        register_fns_tax_debt_handler(session, registry)
        creation = enqueue_fns_tax_debt_fixture_job(
            session,
            source_path=args.zip_path,
            artifact_store=args.artifact_store,
            source_as_of=args.source_as_of,
            retrieved_at=args.retrieved_at,
            expected_sha256=args.expected_sha256,
            timeout_seconds=args.timeout_seconds,
        )
        session.commit()

    if not creation.created and creation.job.status == "succeeded":
        print(f"S02 job already succeeded: {creation.job.id}")
        return

    executor = WorkerExecutor(
        session_factory=SessionLocal,
        registry=registry,
        worker_id="fns-tax-debt-fixture-worker",
    )
    run_id = executor.run_once()
    print(f"S02 worker run succeeded: {run_id}")


if __name__ == "__main__":
    main()

