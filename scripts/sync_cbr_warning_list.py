"""One official bulk request; validate, then atomically publish or keep old data."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256

from app.ingestion.cbr_warning_list import (
    DATASET_CODE, get_dataset_id, parse_cbr_warning_payload,
    replace_cbr_warning_list, validate_cbr_warning_snapshot,
)
from app.providers.cbr_warning_list_provider import (
    CbrWarningListProvider, FULL_LIST_JSON_URL,
)
from app.services.cbr_warning_registry_service import ensure_cbr_warning_list_dataset
from app.services.ingestion_service import start_ingestion, finish_ingestion_failure


def sync_cbr_warning_list(*, batch_size=1000, provider=None):
    if batch_size < 1:
        raise ValueError("batch_size должен быть больше нуля")
    ensure_cbr_warning_list_dataset()
    dataset_id = get_dataset_id()
    # A date of retrieval, NOT a claim about the publisher's data cut-off date.
    retrieved_at = datetime.now(timezone.utc)
    data_date = retrieved_at.date()
    run_id = start_ingestion(
        dataset_code=DATASET_CODE,
        source_file_name="black-list-json",
        source_url=FULL_LIST_JSON_URL,
        data_date=data_date,
        details={"batch_size": batch_size, "date_basis": "retrieved_at_utc"},
    )
    parsed = None
    try:
        response = (provider or CbrWarningListProvider()).fetch_full_list()
        checksum = sha256(response["raw_content"]).hexdigest()
        parsed = parse_cbr_warning_payload(response["payload"], data_date=data_date)
        validate_cbr_warning_snapshot(parsed)
        stats = {key: value for key, value in parsed.items() if key != "records"}
        details = {
            **stats, "http_status": response["http_status"],
            "source_url": FULL_LIST_JSON_URL,
            "retrieved_at": retrieved_at.isoformat(),
            "date_basis": "retrieved_at_utc",
            "complete_snapshot_validated": True,
            "transport": "live_official" if provider is None else "injected_test_provider",
        }
        result = replace_cbr_warning_list(
            parsed["records"], dataset_id=dataset_id, batch_size=batch_size,
            publication={
                "run_id": run_id, "checksum": checksum,
                "source_records": parsed["source_records"],
                "duplicate_records": parsed["duplicate_records"],
                "details": details,
            },
        )
    except Exception as error:
        # Record fetch errors too. Never mark the previous snapshot as freshly
        # updated and never include connection strings or raw SQL in the log.
        finish_ingestion_failure(
            run_id=run_id,
            error_message=f"W1-001 sync failed: {type(error).__name__}",
            rows_read=parsed["source_records"] if parsed else 0,
            rows_skipped=(parsed["rejected_records"] + parsed["duplicate_records"]) if parsed else 0,
            errors_count=1,
            details={"error_kind": getattr(error, "kind", type(error).__name__),
                     "http_status": getattr(error, "http_status", None)},
        )
        raise
    return {**stats, **result, "run_id": run_id, "data_date": data_date.isoformat(),
            "retrieved_at": retrieved_at.isoformat(), "sha256": checksum,
            "http_status": response["http_status"], "source_url": FULL_LIST_JSON_URL}


def main():
    parser = argparse.ArgumentParser(description="Синхронизация полного официального списка Банка России")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--force", action="store_true",
                        help="Совместимость: каждый явный запуск проверяет и публикует полный снимок")
    args = parser.parse_args()
    result = sync_cbr_warning_list(batch_size=args.batch_size)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("W1-001 LIVE + IMPORT: PASS (browser acceptance is separate)")


if __name__ == "__main__":
    main()
