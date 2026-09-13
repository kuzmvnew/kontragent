from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.company import Company
from app.models.headcount import (
    CompanyHeadcount,
)
from app.models.source import DataSet


DATASET_CODE = "fns_headcount"

DEFAULT_BATCH_SIZE = 2000


def local_name(tag):
    """
    Убирает XML namespace.

    Например:

    {namespace}Документ
        ↓
    Документ
    """

    if "}" in tag:
        return tag.rsplit(
            "}",
            1,
        )[-1]

    return tag


def parse_fns_date(value):
    """
    ФНС обычно использует даты
    вида:

    25.08.2026

    Если даты нет или формат другой,
    возвращаем None.
    """

    if not value:
        return None

    formats = [
        "%d.%m.%Y",
        "%Y-%m-%d",
    ]

    for date_format in formats:
        try:
            return datetime.strptime(
                value,
                date_format,
            ).date()

        except ValueError:
            pass

    return None


def extract_document(
    document,
):
    """
    Превращает один XML <Документ>
    ФНС в нормализованный словарь.

    Ожидаем структуру примерно:

    <Документ ...>
        <СведНП
            ИННЮЛ="..."
            НаимОрг="..."
        />
        <СведССЧР
            КолРаб="..."
        />
    </Документ>
    """

    taxpayer = None
    headcount = None

    for child in document:

        name = local_name(
            child.tag
        )

        if name == "СведНП":
            taxpayer = child

        elif name == "СведССЧР":
            headcount = child

    if (
        taxpayer is None
        or headcount is None
    ):
        return None

    inn = (
        taxpayer.attrib.get(
            "ИННЮЛ"
        )
        or taxpayer.attrib.get(
            "ИНН"
        )
    )

    employee_count_raw = (
        headcount.attrib.get(
            "КолРаб"
        )
    )

    if not inn:
        return None

    inn = str(
        inn
    ).strip()

    if len(inn) != 10:
        return None

    if (
        not employee_count_raw
    ):
        return None

    try:
        employee_count = int(
            employee_count_raw
        )

    except (
        TypeError,
        ValueError,
    ):
        return None

    document_id = (
        document.attrib.get(
            "ИдДок"
        )
    )

    document_date = (
        parse_fns_date(
            document.attrib.get(
                "ДатаДок"
            )
        )
    )

    return {
        "inn": inn,
        "employee_count": (
            employee_count
        ),
        "document_id": (
            document_id
        ),
        "document_date": (
            document_date
        ),
    }


def get_dataset_id():
    session = get_session()

    try:
        dataset_id = (
            session.execute(
                select(
                    DataSet.id
                )
                .where(
                    DataSet.code
                    == DATASET_CODE
                )
            )
            .scalar_one_or_none()
        )

        if dataset_id is None:
            raise RuntimeError(
                "Dataset "
                f"{DATASET_CODE} "
                "не найден. "
                "Сначала запусти "
                "scripts.init_sources."
            )

        return dataset_id

    finally:
        session.close()


def process_batch(
    records,
    year,
    dataset_id,
):
    """
    Обрабатывает одну порцию XML-записей.

    1. Находит наши компании по ИНН.
    2. Пропускает компании,
       которых пока нет в master registry.
    3. Upsert в company_headcounts.
    """

    if not records:
        return {
            "matched": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
        }

    # В одном batch могут встретиться
    # одинаковые ИНН.
    records_by_inn = {}

    for record in records:
        records_by_inn[
            record["inn"]
        ] = record

    inns = list(
        records_by_inn.keys()
    )

    session = get_session()

    try:
        company_rows = (
            session.execute(
                select(
                    Company.id,
                    Company.inn,
                )
                .where(
                    Company.inn.in_(
                        inns
                    )
                )
            )
            .all()
        )

        company_ids = {
            inn: company_id
            for company_id, inn
            in company_rows
        }

        values = []

        for (
            inn,
            record,
        ) in records_by_inn.items():

            company_id = (
                company_ids.get(
                    inn
                )
            )

            if company_id is None:
                continue

            values.append(
                {
                    "company_id": (
                        company_id
                    ),
                    "dataset_id": (
                        dataset_id
                    ),
                    "year": year,
                    "employee_count": (
                        record[
                            "employee_count"
                        ]
                    ),
                    "source_document_id": (
                        record[
                            "document_id"
                        ]
                    ),
                    "source_document_date": (
                        record[
                            "document_date"
                        ]
                    ),
                }
            )

        matched = len(
            values
        )

        skipped = (
            len(records_by_inn)
            - matched
        )

        if not values:
            return {
                "matched": 0,
                "inserted": 0,
                "updated": 0,
                "skipped": skipped,
            }

        matched_company_ids = [
            item["company_id"]
            for item in values
        ]

        existing_company_ids = set(
            session.execute(
                select(
                    CompanyHeadcount.company_id
                )
                .where(
                    CompanyHeadcount.dataset_id
                    == dataset_id,
                    CompanyHeadcount.year
                    == year,
                    CompanyHeadcount.company_id.in_(
                        matched_company_ids
                    ),
                )
            )
            .scalars()
            .all()
        )

        updated = len(
            existing_company_ids
        )

        inserted = (
            matched
            - updated
        )

        statement = insert(
            CompanyHeadcount
        ).values(
            values
        )

        statement = (
            statement.on_conflict_do_update(
                constraint=(
                    "uq_company_headcounts_"
                    "company_year_dataset"
                ),
                set_={
                    "employee_count": (
                        statement.excluded
                        .employee_count
                    ),
                    "source_document_id": (
                        statement.excluded
                        .source_document_id
                    ),
                    "source_document_date": (
                        statement.excluded
                        .source_document_date
                    ),
                },
            )
        )

        session.execute(
            statement
        )

        session.commit()

        return {
            "matched": matched,
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
        }

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def iter_xml_records(
    xml_file,
):
    """
    Потоково читает XML.

    В память не загружается
    весь файл целиком.
    """

    context = ET.iterparse(
        xml_file,
        events=(
            "start",
            "end",
        ),
    )

    try:
        _event, root = next(
            context
        )

    except StopIteration:
        return

    for event, element in context:

        if event != "end":
            continue

        if (
            local_name(
                element.tag
            )
            != "Документ"
        ):
            continue

        record = extract_document(
            element
        )

        if record is not None:
            yield record

        element.clear()

        # Не позволяем корневому
        # элементу накапливать
        # миллионы пустых детей.
        root.clear()


def import_fns_headcount_zip(
    zip_path,
    year,
    batch_size=DEFAULT_BATCH_SIZE,
    progress_every=100000,
):
    """
    Импортирует официальный ZIP ФНС.

    ZIP НЕ распаковывается на диск.

    XML читается непосредственно
    из архива потоково.
    """

    path = Path(
        zip_path
    )

    if not path.exists():
        raise FileNotFoundError(
            f"ZIP не найден: {path}"
        )

    dataset_id = (
        get_dataset_id()
    )

    totals = {
        "rows_read": 0,
        "matched": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "invalid": 0,
        "xml_files": 0,
    }

    batch = []

    with ZipFile(
        path,
        "r",
    ) as archive:

        xml_files = [
            info
            for info
            in archive.infolist()
            if (
                not info.is_dir()
                and info.filename
                .lower()
                .endswith(".xml")
            )
        ]

        if not xml_files:
            raise RuntimeError(
                "В ZIP не найдено "
                "ни одного XML-файла"
            )

        print(
            "XML-файлов в архиве:",
            len(xml_files),
        )

        for file_number, info in enumerate(
            xml_files,
            start=1,
        ):
            totals[
                "xml_files"
            ] += 1

            print()
            print(
                f"[{file_number}/"
                f"{len(xml_files)}] "
                f"{info.filename}"
            )

            with archive.open(
                info,
                "r",
            ) as xml_file:

                for record in (
                    iter_xml_records(
                        xml_file
                    )
                ):

                    totals[
                        "rows_read"
                    ] += 1

                    batch.append(
                        record
                    )

                    if (
                        len(batch)
                        >= batch_size
                    ):

                        result = (
                            process_batch(
                                records=batch,
                                year=year,
                                dataset_id=(
                                    dataset_id
                                ),
                            )
                        )

                        totals[
                            "matched"
                        ] += result[
                            "matched"
                        ]

                        totals[
                            "inserted"
                        ] += result[
                            "inserted"
                        ]

                        totals[
                            "updated"
                        ] += result[
                            "updated"
                        ]

                        totals[
                            "skipped"
                        ] += result[
                            "skipped"
                        ]

                        batch.clear()

                    if (
                        progress_every
                        and totals[
                            "rows_read"
                        ]
                        % progress_every
                        == 0
                    ):
                        print(
                            "Прочитано:",
                            totals[
                                "rows_read"
                            ],
                            "| найдено в нашей БД:",
                            totals[
                                "matched"
                            ],
                        )

        if batch:

            result = process_batch(
                records=batch,
                year=year,
                dataset_id=dataset_id,
            )

            totals[
                "matched"
            ] += result[
                "matched"
            ]

            totals[
                "inserted"
            ] += result[
                "inserted"
            ]

            totals[
                "updated"
            ] += result[
                "updated"
            ]

            totals[
                "skipped"
            ] += result[
                "skipped"
            ]

            batch.clear()

    return totals