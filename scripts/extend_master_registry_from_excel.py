import argparse
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.company import Company
from scripts.import_companies import (
    clean_digits,
    clean_text,
    company_row_score,
    read_excel,
)


SOURCE_NAME = "excel_import"


def classify_entity_type(inn: str | None):
    if inn is None:
        return None

    if len(inn) == 10:
        return "legal"

    if len(inn) == 12:
        return "individual_entrepreneur"

    return None


def clean_optional_identifier(
    value,
    *,
    max_length,
):
    """
    Нормализует необязательный цифровой идентификатор.

    Если значение длиннее ограничения колонки PostgreSQL,
    не обрезаем его и не ломаем импорт всей компании —
    сохраняем поле как None. Для расширения покрытия Master
    Registry ключевым идентификатором остаётся валидный ИНН.
    """

    digits = clean_digits(value)

    if digits is None:
        return None

    if len(digits) > max_length:
        return None

    return digits


def build_company_payload(row):
    """
    Строит только базовую запись Company.

    ВАЖНО:
    этот поток предназначен для безопасного расширения
    покрытия Master Registry. Он не обновляет существующие
    компании и не перезаписывает их источник/официальные поля.
    """

    inn = clean_digits(
        row.get("ИНН")
    )

    name = clean_text(
        row.get("Название")
    )

    if (
        not inn
        or len(inn) not in (10, 12)
        or not name
    ):
        raise ValueError(
            "Для добавления компании нужны валидный ИНН и название"
        )

    return {
        "inn": inn,
        "kpp": clean_optional_identifier(
            row.get("КПП"),
            max_length=9,
        ),
        "ogrn": clean_optional_identifier(
            row.get("ОГРН"),
            max_length=15,
        ),
        "okpo": clean_optional_identifier(
            row.get("ОКПО"),
            max_length=12,
        ),
        "name": name,
        "address": clean_text(
            row.get("Адрес")
        ),
        "activity": clean_text(
            row.get("Вид деятельности")
        ),
        "website": clean_text(
            row.get("Сайт")
        ),
        "entity_type": classify_entity_type(
            inn
        ),
        "source": SOURCE_NAME,
    }


def build_insert_statement(payloads):
    """
    INSERT-only statement.

    on_conflict_do_nothing() без conflict target означает:
    при конфликте любого уникального ограничения строка
    пропускается, а существующая запись не изменяется.
    """

    return (
        insert(Company)
        .values(payloads)
        .on_conflict_do_nothing()
        .returning(Company.inn)
    )


def extend_master_registry(
    file_path,
    batch_size=1000,
):
    if batch_size < 1:
        raise ValueError(
            "batch_size должен быть больше нуля"
        )

    grouped = read_excel(
        file_path
    )

    total_unique = len(
        grouped
    )

    session = get_session()

    inserted = 0
    skipped = 0
    batches = 0
    batch = []

    def flush_batch():
        nonlocal inserted
        nonlocal skipped
        nonlocal batches

        if not batch:
            return

        statement = build_insert_statement(
            list(batch)
        )

        try:
            inserted_inns = (
                session.execute(
                    statement
                )
                .scalars()
                .all()
            )

            session.commit()

        except Exception:
            session.rollback()
            raise

        batch_size_actual = len(
            batch
        )

        inserted_count = len(
            inserted_inns
        )

        inserted += inserted_count
        skipped += (
            batch_size_actual
            - inserted_count
        )
        batches += 1

        print(
            "Обработано:",
            f"{inserted + skipped:,}".replace(
                ",",
                " ",
            ),
            "| добавлено:",
            f"{inserted:,}".replace(
                ",",
                " ",
            ),
            "| пропущено существующих/конфликтов:",
            f"{skipped:,}".replace(
                ",",
                " ",
            ),
        )

        batch.clear()

    try:
        for rows in grouped.values():
            canonical_row = max(
                rows,
                key=company_row_score,
            )

            payload = build_company_payload(
                canonical_row
            )

            batch.append(
                payload
            )

            if len(batch) >= batch_size:
                flush_batch()

        flush_batch()

    finally:
        session.close()

    result = {
        "total_unique": total_unique,
        "inserted": inserted,
        "skipped": skipped,
        "batches": batches,
    }

    print()
    print(
        "=" * 70
    )
    print(
        "MASTER REGISTRY EXTENSION FINISHED"
    )
    print(
        "=" * 70
    )
    print(
        "Уникальных ИНН в Excel:",
        f"{total_unique:,}".replace(
            ",",
            " ",
        ),
    )
    print(
        "Новых компаний добавлено:",
        f"{inserted:,}".replace(
            ",",
            " ",
        ),
    )
    print(
        "Существующих/конфликтов пропущено:",
        f"{skipped:,}".replace(
            ",",
            " ",
        ),
    )
    print(
        "Batch:",
        batches,
    )
    print()
    print(
        "Существующие компании не обновлялись."
    )

    return result


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Безопасно расширяет Master Registry новыми "
            "компаниями из Excel без обновления существующих записей"
        )
    )

    parser.add_argument(
        "file",
        help="Путь к Excel-файлу",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Размер PostgreSQL batch, по умолчанию 1000",
    )

    args = parser.parse_args()

    file_path = Path(
        args.file
    ).expanduser().resolve()

    if not file_path.exists():
        raise FileNotFoundError(
            f"Файл не найден: {file_path}"
        )

    print(
        "Файл:",
        file_path,
    )
    print(
        "Batch size:",
        args.batch_size,
    )
    print(
        "Режим: INSERT ONLY — существующие компании не изменяются"
    )

    extend_master_registry(
        file_path=file_path,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()
