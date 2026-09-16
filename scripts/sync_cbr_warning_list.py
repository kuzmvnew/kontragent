import argparse
from datetime import date
from hashlib import sha256

from app.ingestion.cbr_warning_list import (
    DATASET_CODE,
    get_dataset_id,
    parse_cbr_warning_payload,
    replace_cbr_warning_list,
)
from app.providers.cbr_warning_list_provider import (
    CbrWarningListProvider,
    FULL_LIST_JSON_URL,
)
from app.services.cbr_warning_registry_service import (
    ensure_cbr_warning_list_dataset,
)
from app.services.ingestion_service import (
    finish_ingestion_failure,
    finish_ingestion_success,
    start_ingestion,
    was_file_successfully_imported,
)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Синхронизация официального предупредительного списка "
            "Банка России"
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Повторно обработать уже импортированный JSON",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Размер batch для PostgreSQL",
    )
    return parser.parse_args()


def main():
    args = parse_arguments()

    if args.batch_size < 1:
        raise SystemExit("--batch-size должен быть больше нуля")

    ensure_cbr_warning_list_dataset()

    provider = CbrWarningListProvider()
    response = provider.fetch_full_list()

    raw_content = response["raw_content"]
    checksum = sha256(raw_content).hexdigest()
    data_date = date.today()

    if (
        was_file_successfully_imported(
            dataset_code=DATASET_CODE,
            file_checksum=checksum,
        )
        and not args.force
    ):
        print("CBR warning list уже импортирован: SHA-256 совпадает.")
        return

    dataset_id = get_dataset_id()

    run_id = start_ingestion(
        dataset_code=DATASET_CODE,
        source_file_name="black-list-json",
        source_url=FULL_LIST_JSON_URL,
        data_date=data_date,
        file_checksum=checksum,
        details={
            "batch_size": args.batch_size,
            "force": args.force,
            "source_kind": "cbr_warning_list_official_json",
        },
    )

    parsed = None

    try:
        parsed = parse_cbr_warning_payload(
            response["payload"],
            data_date=data_date,
        )

        result = replace_cbr_warning_list(
            parsed["records"],
            dataset_id=dataset_id,
            batch_size=args.batch_size,
        )

    except Exception as error:
        finish_ingestion_failure(
            run_id=run_id,
            error_message=str(error),
            rows_read=(
                parsed["source_records"]
                if parsed is not None
                else 0
            ),
            rows_skipped=(
                parsed["rejected_records"]
                + parsed["duplicate_records"]
                if parsed is not None
                else 0
            ),
            errors_count=1,
        )
        raise

    finish_ingestion_success(
        run_id=run_id,
        rows_read=parsed["source_records"],
        rows_inserted=result["inserted"],
        rows_updated=0,
        rows_skipped=(
            parsed["rejected_records"]
            + parsed["duplicate_records"]
        ),
        errors_count=0,
        data_date=data_date,
        details={
            "with_inn": parsed["with_inn"],
            "without_inn": parsed["without_inn"],
            "rejected_records": parsed["rejected_records"],
            "duplicate_records": parsed["duplicate_records"],
            "http_status": response["http_status"],
        },
    )

    print("CBR warning list sync complete")
    print("Data date:", data_date)
    print("Source records:", parsed["source_records"])
    print("Imported records:", result["inserted"])
    print("With INN:", parsed["with_inn"])
    print("Without INN:", parsed["without_inn"])
    print("Rejected:", parsed["rejected_records"])
    print("Duplicates:", parsed["duplicate_records"])
    print("SHA-256:", checksum)


if __name__ == "__main__":
    main()
