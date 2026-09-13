import argparse
import sys
from datetime import date
from pathlib import Path

from app.ingestion.fns_headcount import (
    DATASET_CODE,
    import_fns_headcount_zip,
)
from app.services.ingestion_service import (
    calculate_file_checksum,
    finish_ingestion_failure,
    finish_ingestion_success,
    start_ingestion,
    was_file_successfully_imported,
)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Импорт среднесписочной "
            "численности ФНС"
        )
    )

    parser.add_argument(
        "zip_path",
        help=(
            "Путь к официальному "
            "ZIP-файлу ФНС"
        ),
    )

    parser.add_argument(
        "--year",
        type=int,
        required=True,
        help=(
            "Год, к которому относятся "
            "сведения. Например: 2025"
        ),
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=2000,
        help=(
            "Размер batch для PostgreSQL"
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Разрешить повторный импорт "
            "файла, который уже был "
            "успешно обработан"
        ),
    )

    return parser.parse_args()


def main():
    args = parse_arguments()

    path = Path(
        args.zip_path
    )

    if not path.exists():
        print()
        print(
            "Файл не найден:"
        )
        print(
            path
        )
        print()

        sys.exit(1)

    print()
    print(
        "=========================================="
    )
    print(
        "ФНС — СРЕДНЕСПИСОЧНАЯ ЧИСЛЕННОСТЬ"
    )
    print(
        "=========================================="
    )
    print()

    print(
        "Файл:",
        path,
    )

    print(
        "Год данных:",
        args.year,
    )

    print(
        "Batch size:",
        args.batch_size,
    )

    print(
        "Force:",
        args.force,
    )

    print()
    print(
        "Считаем SHA-256..."
    )

    checksum = calculate_file_checksum(
        path
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
        print()
        print(
            "Этот файл уже был "
            "успешно импортирован."
        )

        print(
            "Повторная загрузка отменена."
        )

        print()
        print(
            "Если нужно повторно "
            "сопоставить этот файл "
            "с обновлённым Master Registry,"
        )

        print(
            "запусти команду "
            "с параметром --force."
        )

        print()

        return

    if (
        duplicate
        and args.force
    ):
        print()
        print(
            "Файл уже импортировался ранее."
        )

        print(
            "--force включён."
        )

        print(
            "Запускаем повторное "
            "сопоставление с текущей "
            "базой companies."
        )

        print()

    run_id = start_ingestion(
        dataset_code=DATASET_CODE,
        source_file_name=(
            path.name
        ),
        data_date=date(
            args.year,
            12,
            31,
        ),
        file_checksum=checksum,
        details={
            "year": args.year,
            "batch_size": (
                args.batch_size
            ),
            "force": (
                args.force
            ),
        },
    )

    print(
        "Ingestion run:",
        run_id,
    )

    print()

    try:
        totals = (
            import_fns_headcount_zip(
                zip_path=path,
                year=args.year,
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
                "force": (
                    args.force
                ),
            },
        )

        print()
        print(
            "=========================================="
        )
        print(
            "ИМПОРТ ОСТАНОВЛЕН"
        )
        print(
            "=========================================="
        )
        print()

        print(
            "Ingestion run отмечен "
            "как failed."
        )

        print(
            "Уже записанные batches "
            "останутся в PostgreSQL."
        )

        print(
            "Повторный запуск безопасен, "
            "потому что используется UPSERT."
        )

        print()

        sys.exit(130)

    except Exception as error:
        finish_ingestion_failure(
            run_id=run_id,
            error_message=str(
                error
            ),
            errors_count=1,
            details={
                "force": (
                    args.force
                ),
            },
        )

        print()
        print(
            "=========================================="
        )
        print(
            "ОШИБКА ИМПОРТА"
        )
        print(
            "=========================================="
        )
        print()

        print(
            str(error)
        )

        print()

        raise

    finish_ingestion_success(
        run_id=run_id,
        rows_read=(
            totals[
                "rows_read"
            ]
        ),
        rows_inserted=(
            totals[
                "inserted"
            ]
        ),
        rows_updated=(
            totals[
                "updated"
            ]
        ),
        rows_skipped=(
            totals[
                "skipped"
            ]
        ),
        errors_count=(
            totals[
                "invalid"
            ]
        ),
        data_date=date(
            args.year,
            12,
            31,
        ),
        details={
            **totals,
            "force": (
                args.force
            ),
        },
    )

    print()
    print(
        "=========================================="
    )
    print(
        "ИМПОРТ ЗАВЕРШЁН"
    )
    print(
        "=========================================="
    )
    print()

    print(
        "Прочитано:",
        totals[
            "rows_read"
        ],
    )

    print(
        "Совпало с нашей БД:",
        totals[
            "matched"
        ],
    )

    print(
        "Добавлено:",
        totals[
            "inserted"
        ],
    )

    print(
        "Обновлено:",
        totals[
            "updated"
        ],
    )

    print(
        "Не найдено в нашей БД:",
        totals[
            "skipped"
        ],
    )

    print(
        "Некорректных:",
        totals[
            "invalid"
        ],
    )

    print(
        "XML-файлов:",
        totals[
            "xml_files"
        ],
    )

    print()


if __name__ == "__main__":
    main()