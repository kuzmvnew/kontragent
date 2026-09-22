"""Run the bounded Mintrans TED fixture pipeline; never downloads source data."""

import argparse
from datetime import datetime
import json
from pathlib import Path

from app.ingestion.mintrans_ted_registry import run_mintrans_ted_fixture
from app.services.mintrans_ted_registry_service import (
    ensure_mintrans_ted_fixture_dataset,
)


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("source-as-of must include a timezone")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish a local Mintrans TED XLSX fixture (live ingestion is disabled)."
    )
    parser.add_argument("xlsx", type=Path)
    parser.add_argument("--artifact-store", type=Path, required=True)
    parser.add_argument("--source-as-of", type=_aware_datetime, required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument(
        "--source-metadata-json",
        default="{}",
        help="JSON object recorded in the immutable manifest",
    )
    args = parser.parse_args()
    metadata = json.loads(args.source_metadata_json)
    if not isinstance(metadata, dict):
        parser.error("--source-metadata-json must contain a JSON object")

    ensure_mintrans_ted_fixture_dataset()
    run_id = run_mintrans_ted_fixture(
        args.xlsx,
        artifact_store=args.artifact_store,
        source_as_of=args.source_as_of,
        source_metadata=metadata,
        expected_sha256=args.expected_sha256,
    )
    print(
        json.dumps(
            {
                "dataset_code": "mintrans_ted_registry",
                "run_id": run_id,
                "live_ingestion": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
