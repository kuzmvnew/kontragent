import argparse
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.ingestion.fns_tax_payment import (
    DATASET_CODE,
    import_fns_tax_payment_zip,
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
            "уплаченных налогов "
            "и платежей ФНС PAYTAX"
        )
    )

    parser.add_argument(
        "zip_path",
        help=(
            "Путь к официальному "
            "ZIP-файлу PAYTAX ФНС"
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


def make_json_safe(
    values,
):
    """
    Преобразует Decimal/date
    в значения, пригодные для JSONB.
    """

    result = {}

    for key, value in (
        values.items()
    ):

        if isinstance(
            value,
            Decimal,
        ):

            result[
                key
            ] = str(
                value
            )

        elif isinstance(
            value,
            (
                date,
                datetime,
            ),
        ):

            result[
                key
            ] = (
                value.isoformat()
            )

        else:

            result[
                key
            ] = value

    return result


def print_money(
    label,
    value,
):
    """
    Упрощённый вывод больших Decimal.
    """

    print(
        label,
        value,
    )


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
        "ФНС — PAYTAX"
    )
    print(
        "УПЛАЧЕННЫЕ НАЛОГИ И ПЛАТЕЖИ"
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
        dataset_code=(
            DATASET_CODE
        ),
        source_file_name=(
            path.name
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
                "fns_paytax"
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
            import_fns_tax_payment_zip(
                zip_path=(
                    path
                ),
                batch_size=(
                    args.batch_size
                ),
            )
        )

    except KeyboardInterrupt:

        finish_ingestion_failure(
            run_id=(
                run_id
            ),
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
            run_id=(
                run_id
            ),
            error_message=(
                str(
                    error
                )
            ),
            errors_count=1,
        )

        print()
        print(
            "ОШИБКА ИМПОРТА:"
        )

        print(
            str(
                error
            )
        )

        print()

        raise

    data_date = (
        totals[
            "data_date"
        ]
    )

    details = (
        make_json_safe(
            totals
        )
    )

    finish_ingestion_success(
        run_id=(
            run_id
        ),
        rows_read=(
            totals[
                "rows_read"
            ]
        ),
        rows_inserted=(
            totals[
                "snapshots_inserted"
            ]
        ),
        rows_updated=(
            totals[
                "snapshots_updated"
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
            data_date
        ),
        details=(
            details
        ),
    )

    print()
    print(
        "=========================================="
    )
    print(
        "ИМПОРТ PAYTAX ЗАВЕРШЁН"
    )
    print(
        "=========================================="
    )
    print()

    print(
        "Прочитано документов:",
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

    print()

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

    print()

    print(
        "Snapshots inserted:",
        totals[
            "snapshots_inserted"
        ],
    )

    print(
        "Snapshots updated:",
        totals[
            "snapshots_updated"
        ],
    )

    print(
        "Items inserted:",
        totals[
            "items_inserted"
        ],
    )

    print()

    print(
        "Позиций в исходном XML:",
        totals[
            "source_items"
        ],
    )

    print(
        "Ненулевых позиций "
        "в исходном XML:",
        totals[
            "stored_source_items"
        ],
    )

    print()

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

    print(
        "Дата документа:",
        totals[
            "document_date"
        ],
    )

    print()

    print(
        "СУММЫ ПО ВСЕМУ "
        "ИСХОДНОМУ НАБОРУ"
    )

    print_money(
        "Всего:",
        totals[
            "source_total_amount"
        ],
    )

    print_money(
        "Налоги:",
        totals[
            "source_tax_amount"
        ],
    )

    print_money(
        "Страховые взносы:",
        totals[
            "source_insurance_amount"
        ],
    )

    print_money(
        "Пени:",
        totals[
            "source_penalty_amount"
        ],
    )

    print_money(
        "Неналоговые платежи:",
        totals[
            "source_non_tax_amount"
        ],
    )

    print_money(
        "Прочее:",
        totals[
            "source_other_amount"
        ],
    )

    print()

    print(
        "СУММЫ ПО КОМПАНИЯМ, "
        "КОТОРЫЕ ЕСТЬ В НАШЕЙ БАЗЕ"
    )

    print_money(
        "Всего:",
        totals[
            "matched_total_amount"
        ],
    )

    print_money(
        "Налоги:",
        totals[
            "matched_tax_amount"
        ],
    )

    print_money(
        "Страховые взносы:",
        totals[
            "matched_insurance_amount"
        ],
    )

    print_money(
        "Пени:",
        totals[
            "matched_penalty_amount"
        ],
    )

    print_money(
        "Неналоговые платежи:",
        totals[
            "matched_non_tax_amount"
        ],
    )

    print_money(
        "Прочее:",
        totals[
            "matched_other_amount"
        ],
    )

    print()


if __name__ == "__main__":
    main()