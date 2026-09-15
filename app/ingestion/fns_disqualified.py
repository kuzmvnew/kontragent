import csv
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import (
    insert as pg_insert,
)

from app.database.postgres import get_session
from app.models.disqualified_person import (
    DisqualifiedPersonSnapshot,
)
from app.models.source import DataSet


DATASET_CODE = "fns_disqualified"

SOURCE_URL = (
    "https://www.nalog.gov.ru/"
    "opendata/"
    "7707329152-registerdisqualified/"
)

EXPECTED_HEADER = [
    "G1",
    "G2",
    "G3",
    "G4",
    "G5",
    "G6",
    "G7",
    "G8",
    "G9",
    "G10",
    "G11",
    "G12",
    "G13",
    "G14",
]


def parse_fns_date(value):
    value = (value or "").strip()

    if not value:
        return None

    try:
        return datetime.strptime(
            value,
            "%d.%m.%Y",
        ).date()
    except ValueError:
        return None


def normalize_organization_inn(value):
    value = (value or "").strip()

    if not value:
        return None

    if (
        value.isdigit()
        and len(value) == 10
    ):
        return value

    return None


def parse_disqualified_row(
    row,
    data_date: date,
):
    if len(row) != 14:
        return None

    register_number = row[0].strip()
    full_name = row[1].strip()

    if not register_number:
        return None

    if not full_name:
        return None

    return {
        "data_date": data_date,
        "register_number": register_number,
        "full_name": full_name,
        "birth_date": parse_fns_date(
            row[2]
        ),
        "birth_place": (
            row[3].strip()
            or None
        ),
        "organization_name": (
            row[4].strip()
            or None
        ),
        "organization_inn": (
            normalize_organization_inn(
                row[5]
            )
        ),
        "position": (
            row[6].strip()
            or None
        ),
        "offence_article": (
            row[7].strip()
            or None
        ),
        "protocol_authority": (
            row[8].strip()
            or None
        ),
        "judge_name": (
            row[9].strip()
            or None
        ),
        "judge_position": (
            row[10].strip()
            or None
        ),
        "disqualification_term": (
            row[11].strip()
            or None
        ),
        "start_date": parse_fns_date(
            row[12]
        ),
        "end_date": parse_fns_date(
            row[13]
        ),
    }


def iter_disqualified_records(
    csv_path,
    data_date: date,
    limit=None,
):
    path = Path(csv_path)

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        reader = csv.reader(file)

        header = next(
            reader,
            None,
        )

        if header != EXPECTED_HEADER:
            raise ValueError(
                "Unexpected FNS "
                "disqualified CSV header: "
                f"{header}"
            )

        yielded = 0

        for row in reader:

            record = (
                parse_disqualified_row(
                    row=row,
                    data_date=data_date,
                )
            )

            if record is None:
                continue

            yield record

            yielded += 1

            if (
                limit is not None
                and yielded >= limit
            ):
                break



def get_dataset_id(
    dataset_code: str = DATASET_CODE,
):
    session = get_session()

    try:
        dataset_id = (
            session.execute(
                select(DataSet.id)
                .where(
                    DataSet.code
                    == dataset_code
                )
            )
            .scalar_one_or_none()
        )

        if dataset_id is None:
            raise ValueError(
                "Dataset не найден: "
                f"{dataset_code}"
            )

        return dataset_id

    finally:
        session.close()


def _upsert_disqualified_batch(
    session,
    records,
    dataset_id,
):
    """
    UPSERT одного batch в уже открытую
    транзакцию.

    Функция сама не делает commit,
    поэтому её можно безопасно тестировать
    с последующим rollback.
    """

    input_count = len(records)

    if not records:
        return {
            "processed": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
        }

    unique_records = {}

    for record in records:
        key = (
            record["data_date"],
            record["register_number"],
        )

        unique_records[key] = record

    clean_records = list(
        unique_records.values()
    )

    skipped = (
        input_count
        - len(clean_records)
    )

    keys = [
        (
            record["data_date"],
            record["register_number"],
        )
        for record in clean_records
    ]

    existing_rows = (
        session.execute(
            select(
                DisqualifiedPersonSnapshot.data_date,
                DisqualifiedPersonSnapshot.register_number,
            )
            .where(
                DisqualifiedPersonSnapshot.dataset_id
                == dataset_id,
                tuple_(
                    DisqualifiedPersonSnapshot.data_date,
                    DisqualifiedPersonSnapshot.register_number,
                ).in_(keys),
            )
        )
        .all()
    )

    existing = {
        (
            row.data_date,
            row.register_number,
        )
        for row in existing_rows
    }

    values = [
        {
            "dataset_id": dataset_id,
            **record,
        }
        for record in clean_records
    ]

    insert_stmt = pg_insert(
        DisqualifiedPersonSnapshot
    ).values(
        values
    )

    update_fields = (
        "full_name",
        "birth_date",
        "birth_place",
        "organization_name",
        "organization_inn",
        "position",
        "offence_article",
        "protocol_authority",
        "judge_name",
        "judge_position",
        "disqualification_term",
        "start_date",
        "end_date",
    )

    update_values = {
        field: getattr(
            insert_stmt.excluded,
            field,
        )
        for field in update_fields
    }

    update_values["updated_at"] = (
        func.now()
    )

    statement = (
        insert_stmt
        .on_conflict_do_update(
            constraint=(
                "uq_disqualified_person_"
                "dataset_date_register"
            ),
            set_=update_values,
        )
    )

    session.execute(
        statement
    )

    session.flush()

    updated = len(
        existing
    )

    inserted = (
        len(clean_records)
        - updated
    )

    return {
        "processed": input_count,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


def process_disqualified_batch(
    records,
    dataset_id,
):
    """
    UPSERT batch с собственной транзакцией.
    """

    session = get_session()

    try:
        result = (
            _upsert_disqualified_batch(
                session=session,
                records=records,
                dataset_id=dataset_id,
            )
        )

        session.commit()

        return result

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def import_fns_disqualified_csv(
    csv_path,
    data_date: date,
    batch_size: int = 1000,
    limit=None,
    dataset_id=None,
):
    """
    Потоковый импорт CSV ФНС.

    Повторный запуск безопасен:
    используется UPSERT по
    dataset_id + data_date + register_number.
    """

    path = Path(
        csv_path
    )

    if not path.is_file():
        raise FileNotFoundError(
            f"CSV не найден: {path}"
        )

    if batch_size < 1:
        raise ValueError(
            "batch_size должен быть > 0"
        )

    if dataset_id is None:
        dataset_id = get_dataset_id()

    totals = {
        "processed": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "batches": 0,
        "with_org_inn": 0,
        "without_org_inn": 0,
        "dataset_id": dataset_id,
    }

    batch = []

    def flush_batch():
        if not batch:
            return

        result = (
            process_disqualified_batch(
                records=list(batch),
                dataset_id=dataset_id,
            )
        )

        totals["inserted"] += (
            result["inserted"]
        )

        totals["updated"] += (
            result["updated"]
        )

        totals["skipped"] += (
            result["skipped"]
        )

        totals["batches"] += 1

        batch.clear()

    for record in (
        iter_disqualified_records(
            csv_path=path,
            data_date=data_date,
            limit=limit,
        )
    ):
        totals["processed"] += 1

        if record["organization_inn"]:
            totals[
                "with_org_inn"
            ] += 1
        else:
            totals[
                "without_org_inn"
            ] += 1

        batch.append(
            record
        )

        if (
            len(batch)
            >= batch_size
        ):
            flush_batch()

    flush_batch()

    return totals
