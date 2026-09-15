import argparse
import sys
from datetime import date
from pathlib import Path

from app.ingestion.fns_disqualified import (
    DATASET_CODE,
    SOURCE_URL,
    import_fns_disqualified_csv,
)
from app.services.ingestion_service import (
    calculate_file_checksum,
    finish_ingestion_failure,
    finish_ingestion_success,
    start_ingestion,
    was_file_successfully_imported,
)


def parse_data_date(value):
    try:
        return date.fromisoformat(
            value
        )
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Дата должна быть в формате "
            "YYYY-MM-DD"
        ) from error


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Импорт Реестра "
            "дисквалифицированных лиц ФНС"
        )
    )

    parser.add_argument(
        "csv_path",
        help="Путь к официальному CSV ФНС",
    )

    parser.add_argument(
        "--data-date",
        required=True,
        type=parse_data_date,
        help=(
            "Дата публикации данных "
            "в формате YYYY-MM-DD"
        ),
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help=(
            "Размер batch для PostgreSQL, "
            "по умолчанию 1000"
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Разрешить повторный импорт "
            "уже успешно обработанного файла"
        ),
    )

    return parser.parse_args()


def main():
    args = parse_arguments()

    path = Path(
        args.csv_path
    )

    if not path.is_file():
        print(
            "CSV не найден:",
            path,
        )
        sys.exit(1)

    if args.batch_size < 1:
        print(
            "--batch-size должен "
            "быть больше нуля"
        )
        sys.exit(1)

    print(
        "=" * 70
    )
    print(
        "ФНС — РЕЕСТР "
        "ДИСКВАЛИФИЦИРОВАННЫХ ЛИЦ"
    )
    print(
        "=" * 70
    )

    print(
        "Файл:",
        path,
    )

    print(
        "Дата данных:",
        args.data_date,
    )

    print(
        "Batch size:",
        args.batch_size,
    )

    print(
        "Force:",
        args.force,
    )

    print(
        "Считаем SHA-256..."
    )

    checksum = (
        calculate_file_checksum(
            path
        )
    )

    print(
        "SHA-256:",
        checksum,
    )

    duplicate = (
        was_file_successfully_imported(
            dataset_code=DATASET_CODE,
            file_checksum=checksum,
        )
    )

    if (
        duplicate
        and not args.force
    ):
        print(
            "Этот файл уже успешно "
            "импортирован."
        )

        print(
            "Повторная загрузка отменена. "
            "Для повтора добавь --force."
        )

        return

    run_id = start_ingestion(
        dataset_code=DATASET_CODE,
        source_file_name=path.name,
        source_url=SOURCE_URL,
        data_date=args.data_date,
        file_checksum=checksum,
        details={
            "batch_size": (
                args.batch_size
            ),
            "force": (
                args.force
            ),
            "source_kind": (
                "fns_disqualified"
            ),
        },
    )

    print(
        "Ingestion run:",
        run_id,
    )

    try:
        totals = (
            import_fns_disqualified_csv(
                csv_path=path,
                data_date=args.data_date,
                batch_size=(
                    args.batch_size
                ),
            )
        )

    except KeyboardInterrupt:
        finish_ingestion_failure(
            run_id=run_id,
            error_message=(
                "Импорт остановлен "
                "пользователем"
            ),
            errors_count=1,
            details={
                "interrupted": True,
            },
        )

        print(
            "Импорт остановлен."
        )

        print(
            "Повторный запуск безопасен "
            "благодаря UPSERT."
        )

        sys.exit(130)

    except Exception as error:
        finish_ingestion_failure(
            run_id=run_id,
            error_message=str(
                error
            ),
            errors_count=1,
        )

        print(
            "ОШИБКА ИМПОРТА:",
            error,
        )

        raise

    finish_ingestion_success(
        run_id=run_id,
        rows_read=(
            totals["processed"]
        ),
        rows_inserted=(
            totals["inserted"]
        ),
        rows_updated=(
            totals["updated"]
        ),
        rows_skipped=(
            totals["skipped"]
        ),
        errors_count=0,
        data_date=args.data_date,
        details={
            "batches": (
                totals["batches"]
            ),
            "with_org_inn": (
                totals[
                    "with_org_inn"
                ]
            ),
            "without_org_inn": (
                totals[
                    "without_org_inn"
                ]
            ),
        },
    )

    print()
    print(
        "=" * 70
    )
    print(
        "IMPORT FINISHED"
    )
    print(
        "=" * 70
    )

    print(
        "Processed:",
        f"{totals['processed']:,}",
    )

    print(
        "Inserted:",
        f"{totals['inserted']:,}",
    )

    print(
        "Updated:",
        f"{totals['updated']:,}",
    )

    print(
        "Skipped:",
        f"{totals['skipped']:,}",
    )

    print(
        "With organization INN:",
        f"{totals['with_org_inn']:,}",
    )

    print(
        "Without organization INN:",
        f"{totals['without_org_inn']:,}",
    )

    print(
        "Batches:",
        totals["batches"],
    )


if __name__ == "__main__":
    main()
