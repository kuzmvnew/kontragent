import argparse
import sys
from decimal import Decimal
from pathlib import Path

from app.ingestion.fns_revenue_expense import (
    DATASET_CODE,
    SOURCE_URL,
    import_fns_revenue_expense_zip,
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
            "Потоковый импорт доходов "
            "и расходов ФНС REVEXP"
        )
    )

    parser.add_argument(
        "zip_path",
        help=(
            "Путь к официальному "
            "ZIP-файлу REVEXP"
        ),
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=10000,
        help=(
            "Размер порции "
            "для PostgreSQL"
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Разрешить повторный импорт "
            "успешно обработанного файла"
        ),
    )

    return parser.parse_args()


def decimal_text(
    value: Decimal,
) -> str:
    """
    Преобразует Decimal в строку
    для сохранения в JSONB.
    """

    return format(
        value,
        "f",
    )


def main():
    args = parse_arguments()

    path = Path(
        args.zip_path
    )

    if not path.is_file():

        print(
            "Файл не найден:",
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
        "ФНС — ДОХОДЫ И РАСХОДЫ REVEXP"
    )

    print(
        "=" * 70
    )

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
            dataset_code=(
                DATASET_CODE
            ),
            file_checksum=(
                checksum
            ),
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
        dataset_code=(
            DATASET_CODE
        ),
        source_file_name=(
            path.name
        ),
        source_url=(
            SOURCE_URL
        ),
        file_checksum=(
            checksum
        ),
        details={
            "batch_size": (
                args.batch_size
            ),
            "force": (
                args.force
            ),
            "source_kind": (
                "fns_revexp"
            ),
        },
    )

    print(
        "Ingestion run:",
        run_id,
    )

    try:
        totals = (
            import_fns_revenue_expense_zip(
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

        print(
            "Импорт остановлен. "
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

    details = {
        "valid": (
            totals[
                "valid"
            ]
        ),
        "invalid": (
            totals[
                "invalid"
            ]
        ),
        "matched": (
            totals[
                "matched"
            ]
        ),
        "unmatched": (
            totals[
                "unmatched"
            ]
        ),
        "xml_files": (
            totals[
                "xml_files"
            ]
        ),
        "revenue_total": (
            decimal_text(
                totals[
                    "revenue_total"
                ]
            )
        ),
        "expenses_total": (
            decimal_text(
                totals[
                    "expenses_total"
                ]
            )
        ),
        "profit_loss_total": (
            decimal_text(
                totals[
                    "profit_loss_total"
                ]
            )
        ),
        "force": (
            args.force
        ),
    }

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
                "unmatched"
            ]
            + totals[
                "invalid"
            ]
        ),
        errors_count=0,
        data_date=(
            totals[
                "data_date"
            ]
        ),
        details=details,
    )

    print(
        "=" * 70
    )

    print(
        "ИМПОРТ REVEXP ЗАВЕРШЁН"
    )

    print(
        "=" * 70
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


if __name__ == "__main__":
    main()