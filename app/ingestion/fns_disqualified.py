import csv
from datetime import date, datetime
from pathlib import Path


DATASET_CODE = "fns_disqualified"

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
