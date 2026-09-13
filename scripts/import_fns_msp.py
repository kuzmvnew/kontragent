import argparse
import sys
from pathlib import Path

from app.ingestion.fns_msp import (
    DATASET_CODE,
    import_fns_msp_zip,
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
            "Реестра МСП ФНС"
        )
    )

    parser.add_argument(
        "zip_path",
        help=(
            "Путь к официальному "
            "ZIP-файлу Реестра МСП"
        ),
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=20000,
        help=(
            "Размер batch "
            "для PostgreSQL"
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
        "ФНС — РЕЕСТР МСП"
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

    if duplicate:

        print()
        print(
            "Этот файл уже был "
            "успешно импортирован."
        )

        print(
            "Повторная загрузка отменена."
        )

        print()

        return

    run_id = start_ingestion(
        dataset_code=DATASET_CODE,
        source_file_name=(
            path.name
        ),
        file_checksum=checksum,
        details={
            "batch_size": (
                args.batch_size
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
            import_fns_msp_zip(
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
        print(
            "Частично импортированные "
            "данные можно безопасно "
            "обработать повторно."
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
        "ИМПОРТ МСП ЗАВЕРШЁН"
    )
    print(
        "=========================================="
    )

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
        "Новых компаний:",
        totals[
            "inserted"
        ],
    )

    print(
        "Обновлено компаний:",
        totals[
            "updated"
        ],
    )

    print(
        "MSP profiles:",
        totals[
            "profiles"
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