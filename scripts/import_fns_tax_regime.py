import argparse
from pathlib import Path

from app.ingestion.fns_tax_regime import (
    LEGAL_DATASET_CODE,
    get_dataset_id,
    iter_legal_records_from_zip,
    process_legal_batch,
)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Импорт специальных налоговых "
            "режимов ФНС"
        )
    )

    parser.add_argument(
        "zip_path",
        help="Путь к ZIP ФНС SNR",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=10000,
        help="Размер batch, по умолчанию 10000",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Ограничить число документов",
    )

    args = parser.parse_args()

    zip_path = Path(
        args.zip_path
    )

    if not zip_path.exists():
        raise SystemExit(
            f"ZIP не найден: {zip_path}"
        )

    if args.batch_size <= 0:
        raise SystemExit(
            "batch-size должен быть > 0"
        )

    dataset_id = get_dataset_id(
        LEGAL_DATASET_CODE
    )

    print()
    print(
        "=========================================="
    )
    print(
        "ФНС — СПЕЦИАЛЬНЫЕ НАЛОГОВЫЕ РЕЖИМЫ ЮЛ"
    )
    print(
        "=========================================="
    )

    print()
    print(
        "ZIP:",
        zip_path,
    )

    print(
        "Dataset:",
        LEGAL_DATASET_CODE,
    )

    print(
        "Dataset ID:",
        dataset_id,
    )

    print(
        "Batch size:",
        args.batch_size,
    )

    print(
        "Limit:",
        args.limit,
    )

    batch = []

    processed = 0
    matched = 0
    inserted = 0
    updated = 0
    skipped = 0
    batches = 0

    def flush_batch():
        nonlocal batch
        nonlocal matched
        nonlocal inserted
        nonlocal updated
        nonlocal skipped
        nonlocal batches

        if not batch:
            return

        result = process_legal_batch(
            batch,
            dataset_id,
        )

        matched += result[
            "matched"
        ]

        inserted += result[
            "inserted"
        ]

        updated += result[
            "updated"
        ]

        skipped += result[
            "skipped"
        ]

        batches += 1

        print(
            "PROGRESS:",
            f"{processed:,}",
            "| matched:",
            f"{matched:,}",
            "| inserted:",
            f"{inserted:,}",
            "| updated:",
            f"{updated:,}",
            "| skipped:",
            f"{skipped:,}",
        )

        batch = []

    for record in iter_legal_records_from_zip(
        zip_path,
        limit=args.limit,
    ):
        batch.append(
            record
        )

        processed += 1

        if len(batch) >= args.batch_size:
            flush_batch()

    flush_batch()

    print()
    print(
        "=========================================="
    )
    print(
        "IMPORT FINISHED"
    )
    print(
        "=========================================="
    )

    print(
        "Processed:",
        f"{processed:,}",
    )

    print(
        "Matched:",
        f"{matched:,}",
    )

    print(
        "Inserted:",
        f"{inserted:,}",
    )

    print(
        "Updated:",
        f"{updated:,}",
    )

    print(
        "Skipped:",
        f"{skipped:,}",
    )

    print(
        "Batches:",
        batches,
    )


if __name__ == "__main__":
    main()
