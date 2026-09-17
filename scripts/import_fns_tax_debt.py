import argparse
import sys
from pathlib import Path

from app.ingestion.fns_tax_debt import (
    DATASET_CODE,
    SOURCE_FILE_BASE_URL,
    import_fns_tax_debt_zip,
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
            "Массовый импорт "
            "налоговой задолженности ФНС"
        )
    )

    parser.add_argument(
        "zip_path",
        help=(
            "Путь к официальному "
            "ZIP-файлу debtam ФНС"
        ),
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=10000,
        help=(
            "Размер batch "
            "для PostgreSQL"
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Разрешить повторный импорт "
            "уже обработанного файла"
        ),
    )

    return parser.parse_args()


def main():
    args = parse_arguments()

    path = Path(
        args.zip_path
    )

    if not path.exists():

        print(
            "Файл не найден:",
            path,
        )

        sys.exit(1)

    print()
    print(
        "=========================================="
    )
    print(
        "ФНС — НАЛОГОВАЯ ЗАДОЛЖЕННОСТЬ"
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

        print()
        print(
            "Этот файл уже был "
            "успешно импортирован."
        )

        print(
            "Повторная загрузка отменена."
        )

        print(
            "Для осознанного повторного "
            "импорта используй --force."
        )

        print()

        return

    if (
        duplicate
        and args.force
    ):

        print()
        print(
            "Файл импортировался ранее."
        )

        print(
            "--force включён."
        )

        print()

    run_id = start_ingestion(
        dataset_code=DATASET_CODE,
        source_file_name=(
            path.name
        ),
        source_url=(
            SOURCE_FILE_BASE_URL
            + path.name
        ),
        file_checksum=checksum,
        details={
            "batch_size": (
                args.batch_size
            ),
            "force": (
                args.force
            ),
        },
    )

    print()
    print(
        "Ingestion run:",
        run_id,
    )

    print()

    try:

        totals = (
            import_fns_tax_debt_zip(
                zip_path=path,
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

        print()
        print(
            "ИМПОРТ ОСТАНОВЛЕН"
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
        )

        print()
        print(
            "ОШИБКА ИМПОРТА:"
        )

        print(
            str(error)
        )

        print()

        raise

    data_date = (
        totals[
            "data_date"
        ]
    )

    details = dict(
        totals
    )

    if data_date is not None:
        details[
            "data_date"
        ] = data_date.isoformat()

    finish_ingestion_success(
        run_id=run_id,
        rows_read=(
            totals[
                "rows_read"
            ]
        ),
        rows_inserted=(
            totals[
                "snapshots"
            ]
        ),
        rows_updated=0,
        rows_skipped=(
            totals[
                "unmatched"
            ]
            + totals[
                "invalid"
            ]
        ),
        errors_count=0,
        data_date=(
            data_date
        ),
        details=details,
    )

    print()
    print(
        "=========================================="
    )
    print(
        "ИМПОРТ ЗАДОЛЖЕННОСТИ ЗАВЕРШЁН"
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
        "Валидных:",
        totals[
            "valid"
        ],
    )

    print(
        "Некорректных:",
        totals[
            "invalid"
        ],
    )

    print(
        "Совпало с companies:",
        totals[
            "matched"
        ],
    )

    print(
        "Не найдено в companies:",
        totals[
            "unmatched"
        ],
    )

    print(
        "Debt snapshots:",
        totals[
            "snapshots"
        ],
    )

    print(
        "Debt items:",
        totals[
            "items"
        ],
    )

    print(
        "XML-файлов:",
        totals[
            "xml_files"
        ],
    )

    print(
        "Дата данных:",
        totals[
            "data_date"
        ],
    )

    print()


if __name__ == "__main__":
    main()
