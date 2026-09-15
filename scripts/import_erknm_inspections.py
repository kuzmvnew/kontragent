import argparse
import re
import sys
from datetime import date
from pathlib import Path

from app.ingestion.erknm import (
    DATASET_CODE,
    import_erknm_zip,
)
from app.services.ingestion_service import (
    calculate_file_checksum,
    finish_ingestion_failure,
    finish_ingestion_success,
    start_ingestion,
    was_file_successfully_imported,
)


DATA_DATE_RE = re.compile(r"data-(\d{8})-structure-")


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Дата должна быть в формате YYYY-MM-DD"
        ) from error


def infer_data_date(path: Path) -> date | None:
    match = DATA_DATE_RE.search(path.name)
    if not match:
        return None

    value = match.group(1)
    return date(
        int(value[0:4]),
        int(value[4:6]),
        int(value[6:8]),
    )


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Импорт официального XML ФГИС ЕРКНМ (248-ФЗ) из ZIP"
    )
    parser.add_argument("zip_path", help="Путь к официальному ZIP ЕРКНМ")
    parser.add_argument("--year", type=int, required=True, help="Логический год набора")
    parser.add_argument("--month", type=int, required=True, help="Логический месяц набора")
    parser.add_argument(
        "--data-date",
        type=parse_iso_date,
        default=None,
        help=(
            "Дата версии данных YYYY-MM-DD. Если не указана, "
            "берётся из имени data-YYYYMMDD-structure-*.zip"
        ),
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main():
    args = parse_arguments()
    path = Path(args.zip_path)

    if not path.is_file():
        print("ZIP не найден:", path)
        sys.exit(1)

    if args.month < 1 or args.month > 12:
        print("--month должен быть от 1 до 12")
        sys.exit(1)

    if args.batch_size < 1:
        print("--batch-size должен быть больше нуля")
        sys.exit(1)

    data_date = args.data_date or infer_data_date(path)
    if data_date is None:
        print("Не удалось определить data_date. Укажи --data-date YYYY-MM-DD")
        sys.exit(1)

    source_url = (
        "https://proverki.gov.ru/blob/erknm-opendata/"
        f"{args.year}/{args.month}/{path.name}"
    )

    print("=" * 72)
    print("ГЕНПРОКУРАТУРА — ФГИС ЕРКНМ (248-ФЗ)")
    print("=" * 72)
    print("Файл:", path)
    print("Период:", f"{args.year}-{args.month:02d}")
    print("Дата версии:", data_date)
    print("Batch size:", args.batch_size)
    print("Force:", args.force)
    print("Считаем SHA-256...")

    checksum = calculate_file_checksum(path)
    print("SHA-256:", checksum)

    duplicate = was_file_successfully_imported(
        dataset_code=DATASET_CODE,
        file_checksum=checksum,
    )

    if duplicate and not args.force:
        print("Этот ZIP уже успешно импортирован.")
        print("Повторная загрузка отменена. Для повтора добавь --force.")
        return

    run_id = start_ingestion(
        dataset_code=DATASET_CODE,
        source_file_name=path.name,
        source_url=source_url,
        data_date=data_date,
        file_checksum=checksum,
        details={
            "period_year": args.year,
            "period_month": args.month,
            "batch_size": args.batch_size,
            "force": args.force,
            "source_kind": "erknm_248fz",
        },
    )

    print("Ingestion run:", run_id)

    try:
        totals = import_erknm_zip(
            path,
            data_date=data_date,
            period_year=args.year,
            period_month=args.month,
            batch_size=args.batch_size,
        )
    except KeyboardInterrupt:
        finish_ingestion_failure(
            run_id=run_id,
            error_message="Импорт остановлен пользователем",
            errors_count=1,
            details={"interrupted": True},
        )
        print("Импорт остановлен. Повторный запуск безопасен благодаря UPSERT.")
        sys.exit(130)
    except Exception as error:
        finish_ingestion_failure(
            run_id=run_id,
            error_message=str(error),
            errors_count=1,
        )
        print("ОШИБКА ИМПОРТА:", error)
        raise

    finish_ingestion_success(
        run_id=run_id,
        rows_read=totals["processed"],
        rows_inserted=totals["inserted"],
        rows_updated=totals["updated"],
        rows_skipped=totals["skipped"],
        errors_count=0,
        data_date=data_date,
        details={
            "period_year": args.year,
            "period_month": args.month,
            "batches": totals["batches"],
            "with_inn": totals["with_inn"],
            "with_ogrn": totals["with_ogrn"],
            "without_identifiers": totals["without_identifiers"],
            "legal_subjects": totals["legal_subjects"],
            "ip_subjects": totals["ip_subjects"],
            "other_subjects": totals["other_subjects"],
        },
    )

    print()
    print("=" * 72)
    print("IMPORT FINISHED")
    print("=" * 72)
    print("Processed:", f"{totals['processed']:,}")
    print("Inserted:", f"{totals['inserted']:,}")
    print("Updated:", f"{totals['updated']:,}")
    print("Skipped duplicate ERPID in batch:", f"{totals['skipped']:,}")
    print("With subject INN:", f"{totals['with_inn']:,}")
    print("With subject OGRN:", f"{totals['with_ogrn']:,}")
    print("Without INN/OGRN:", f"{totals['without_identifiers']:,}")
    print("Legal subjects:", f"{totals['legal_subjects']:,}")
    print("IP subjects:", f"{totals['ip_subjects']:,}")
    print("Other subject types:", f"{totals['other_subjects']:,}")
    print("Batches:", totals["batches"])


if __name__ == "__main__":
    main()
