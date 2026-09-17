from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from sqlalchemy import select

from app.database.postgres import (
    engine,
    get_session,
)
from app.models.source import DataSet


DATASET_CODE = "fns_tax_debt"

SOURCE_PAGE_URL = (
    "https://www.nalog.gov.ru/"
    "opendata/7707329152-debtam/"
)

SOURCE_FILE_BASE_URL = (
    "https://file.nalog.ru/"
    "opendata/7707329152-debtam/"
)

DEFAULT_BATCH_SIZE = 10000

ZERO = Decimal("0.00")


# =========================================================
# XML HELPERS
# =========================================================


def local_name(tag):
    if "}" in tag:
        return tag.rsplit(
            "}",
            1,
        )[-1]

    return tag


def parse_date(value):
    if not value:
        return None

    for date_format in (
        "%d.%m.%Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(
                value,
                date_format,
            ).date()

        except ValueError:
            pass

    return None


def parse_decimal(value):
    if value is None:
        return ZERO

    text = str(
        value
    ).strip().replace(
        ",",
        ".",
    )

    if not text:
        return ZERO

    try:
        return Decimal(
            text
        ).quantize(
            Decimal("0.01")
        )

    except InvalidOperation as error:
        raise ValueError(
            "Некорректное денежное значение "
            f"в FNS Tax Debt: {value!r}"
        ) from error


# =========================================================
# DOCUMENT PARSER
# =========================================================


def parse_tax_debt_document(
    document,
):
    document_id = (
        document.attrib.get(
            "ИдДок"
        )
    )

    document_date = parse_date(
        document.attrib.get(
            "ДатаДок"
        )
    )

    data_date = parse_date(
        document.attrib.get(
            "ДатаСост"
        )
    )

    inn = None
    company_name = None

    items_by_name = {}

    for element in document:

        tag = local_name(
            element.tag
        )

        # -------------------------------------------------
        # COMPANY
        # -------------------------------------------------

        if tag == "СведНП":

            inn = (
                element.attrib.get(
                    "ИННЮЛ"
                )
                or element.attrib.get(
                    "ИНН"
                )
            )

            company_name = (
                element.attrib.get(
                    "НаимОрг"
                )
            )

        # -------------------------------------------------
        # DEBT ITEM
        # -------------------------------------------------

        elif tag == "СведНедоим":

            tax_name = (
                element.attrib.get(
                    "НаимНалог"
                )
                or "Не указано"
            )

            tax_name = (
                tax_name.strip()
            )

            arrears = parse_decimal(
                element.attrib.get(
                    "СумНедНалог"
                )
            )

            penalties = parse_decimal(
                element.attrib.get(
                    "СумПени"
                )
            )

            fines = parse_decimal(
                element.attrib.get(
                    "СумШтраф"
                )
            )

            total = parse_decimal(
                element.attrib.get(
                    "ОбщСумНедоим"
                )
            )

            component_total = (
                arrears
                + penalties
                + fines
            )

            if total != component_total:
                raise ValueError(
                    "ОбщСумНедоим не равна сумме "
                    "недоимки, пеней и штрафов: "
                    f"tax_name={tax_name!r}, "
                    f"total={total}, "
                    f"components={component_total}"
                )

            # Если один и тот же НаимНалог
            # встретится несколько раз,
            # безопасно суммируем.
            existing = (
                items_by_name.get(
                    tax_name
                )
            )

            if existing is None:

                items_by_name[
                    tax_name
                ] = {
                    "tax_name": (
                        tax_name
                    ),
                    "arrears": (
                        arrears
                    ),
                    "penalties": (
                        penalties
                    ),
                    "fines": (
                        fines
                    ),
                    "total": (
                        total
                    ),
                }

            else:

                existing[
                    "arrears"
                ] += arrears

                existing[
                    "penalties"
                ] += penalties

                existing[
                    "fines"
                ] += fines

                existing[
                    "total"
                ] += total

    if not inn:
        return None

    inn = str(
        inn
    ).strip()

    # Этот dataset относится к ЮЛ.
    if (
        len(inn) != 10
        or not inn.isdigit()
    ):
        return None

    if data_date is None:
        return None

    items = list(
        items_by_name.values()
    )

    if not items:
        return None

    total_arrears = sum(
        (
            item["arrears"]
            for item in items
        ),
        ZERO,
    )

    total_penalties = sum(
        (
            item["penalties"]
            for item in items
        ),
        ZERO,
    )

    total_fines = sum(
        (
            item["fines"]
            for item in items
        ),
        ZERO,
    )

    total_debt = sum(
        (
            item["total"]
            for item in items
        ),
        ZERO,
    )

    return {
        "inn": inn,
        "company_name": (
            company_name
        ),
        "document_id": (
            document_id
        ),
        "document_date": (
            document_date
        ),
        "data_date": (
            data_date
        ),
        "total_arrears": (
            total_arrears
        ),
        "total_penalties": (
            total_penalties
        ),
        "total_fines": (
            total_fines
        ),
        "total_debt": (
            total_debt
        ),
        "item_count": (
            len(items)
        ),
        "items": items,
    }


# =========================================================
# DATASET
# =========================================================


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
                "Запусти scripts.init_sources."
            )

        return dataset_id

    finally:
        session.close()


# =========================================================
# TEMP TABLES
# =========================================================


def create_temp_tables(
    cursor,
):
    cursor.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS
        tmp_fns_tax_debt_docs
        (
            inn TEXT PRIMARY KEY,
            company_name TEXT,
            document_id TEXT,
            document_date DATE,
            data_date DATE NOT NULL,
            total_arrears NUMERIC(20, 2) NOT NULL,
            total_penalties NUMERIC(20, 2) NOT NULL,
            total_fines NUMERIC(20, 2) NOT NULL,
            total_debt NUMERIC(20, 2) NOT NULL,
            item_count INTEGER NOT NULL
        )
        ON COMMIT PRESERVE ROWS
        """
    )

    cursor.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS
        tmp_fns_tax_debt_items
        (
            inn TEXT NOT NULL,
            data_date DATE NOT NULL,
            tax_name TEXT NOT NULL,
            arrears NUMERIC(20, 2) NOT NULL,
            penalties NUMERIC(20, 2) NOT NULL,
            fines NUMERIC(20, 2) NOT NULL,
            total NUMERIC(20, 2) NOT NULL
        )
        ON COMMIT PRESERVE ROWS
        """
    )


# =========================================================
# BATCH IMPORT
# =========================================================


def process_batch(
    raw_connection,
    cursor,
    records,
    dataset_id,
):
    if not records:

        return {
            "matched": 0,
            "unmatched": 0,
            "snapshots": 0,
            "items": 0,
        }

    # Один актуальный документ по ИНН
    # внутри текущего batch.
    by_inn = {
        record["inn"]: record
        for record in records
    }

    records = list(
        by_inn.values()
    )

    try:

        cursor.execute(
            "TRUNCATE tmp_fns_tax_debt_docs"
        )

        cursor.execute(
            "TRUNCATE tmp_fns_tax_debt_items"
        )

        # -------------------------------------------------
        # COPY DOCUMENTS
        # -------------------------------------------------

        with cursor.copy(
            """
            COPY tmp_fns_tax_debt_docs
            (
                inn,
                company_name,
                document_id,
                document_date,
                data_date,
                total_arrears,
                total_penalties,
                total_fines,
                total_debt,
                item_count
            )
            FROM STDIN
            """
        ) as copy:

            for record in records:

                copy.write_row(
                    (
                        record["inn"],
                        record[
                            "company_name"
                        ],
                        record[
                            "document_id"
                        ],
                        record[
                            "document_date"
                        ],
                        record[
                            "data_date"
                        ],
                        record[
                            "total_arrears"
                        ],
                        record[
                            "total_penalties"
                        ],
                        record[
                            "total_fines"
                        ],
                        record[
                            "total_debt"
                        ],
                        record[
                            "item_count"
                        ],
                    )
                )

        # -------------------------------------------------
        # COPY ITEMS
        # -------------------------------------------------

        with cursor.copy(
            """
            COPY tmp_fns_tax_debt_items
            (
                inn,
                data_date,
                tax_name,
                arrears,
                penalties,
                fines,
                total
            )
            FROM STDIN
            """
        ) as copy:

            for record in records:

                for item in record[
                    "items"
                ]:

                    copy.write_row(
                        (
                            record["inn"],
                            record[
                                "data_date"
                            ],
                            item[
                                "tax_name"
                            ],
                            item[
                                "arrears"
                            ],
                            item[
                                "penalties"
                            ],
                            item[
                                "fines"
                            ],
                            item[
                                "total"
                            ],
                        )
                    )

        # -------------------------------------------------
        # MATCH STATISTICS
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM tmp_fns_tax_debt_docs t
            JOIN companies c
              ON c.inn = t.inn
            """
        )

        matched = (
            cursor.fetchone()[0]
        )

        unmatched = (
            len(records)
            - matched
        )

        # -------------------------------------------------
        # SNAPSHOT UPSERT
        # -------------------------------------------------

        cursor.execute(
            """
            INSERT INTO company_tax_debt_snapshots
            (
                company_id,
                dataset_id,
                data_date,
                document_date,
                source_document_id,
                total_arrears,
                total_penalties,
                total_fines,
                total_debt,
                item_count
            )

            SELECT
                c.id,
                %s,
                t.data_date,
                t.document_date,
                t.document_id,
                t.total_arrears,
                t.total_penalties,
                t.total_fines,
                t.total_debt,
                t.item_count

            FROM tmp_fns_tax_debt_docs t

            JOIN companies c
              ON c.inn = t.inn

            ON CONFLICT
            (
                company_id,
                dataset_id,
                data_date
            )

            DO UPDATE SET

                document_date =
                    EXCLUDED.document_date,

                source_document_id =
                    EXCLUDED.source_document_id,

                total_arrears =
                    EXCLUDED.total_arrears,

                total_penalties =
                    EXCLUDED.total_penalties,

                total_fines =
                    EXCLUDED.total_fines,

                total_debt =
                    EXCLUDED.total_debt,

                item_count =
                    EXCLUDED.item_count,

                updated_at = NOW()
            """,
            (
                dataset_id,
            ),
        )

        # -------------------------------------------------
        # Для этого snapshot заменяем детализацию целиком.
        # -------------------------------------------------

        cursor.execute(
            """
            DELETE FROM company_tax_debt_items i

            USING company_tax_debt_snapshots s,
                  tmp_fns_tax_debt_docs t,
                  companies c

            WHERE
                i.snapshot_id = s.id

                AND s.company_id = c.id

                AND c.inn = t.inn

                AND s.dataset_id = %s

                AND s.data_date = t.data_date
            """,
            (
                dataset_id,
            ),
        )

        # -------------------------------------------------
        # ITEMS INSERT
        # -------------------------------------------------

        cursor.execute(
            """
            INSERT INTO company_tax_debt_items
            (
                snapshot_id,
                tax_name,
                arrears,
                penalties,
                fines,
                total
            )

            SELECT
                s.id,
                t.tax_name,
                t.arrears,
                t.penalties,
                t.fines,
                t.total

            FROM tmp_fns_tax_debt_items t

            JOIN companies c
              ON c.inn = t.inn

            JOIN company_tax_debt_snapshots s
              ON s.company_id = c.id
             AND s.dataset_id = %s
             AND s.data_date = t.data_date
            """,
            (
                dataset_id,
            ),
        )

        cursor.execute(
            """
            SELECT COUNT(*)

            FROM tmp_fns_tax_debt_items t

            JOIN companies c
              ON c.inn = t.inn
            """
        )

        items_inserted = (
            cursor.fetchone()[0]
        )

        raw_connection.commit()

        return {
            "matched": matched,
            "unmatched": (
                unmatched
            ),
            "snapshots": (
                matched
            ),
            "items": (
                items_inserted
            ),
        }

    except Exception:

        raw_connection.rollback()

        raise


# =========================================================
# STREAM XML
# =========================================================


def iter_xml_records(
    xml_file,
):
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

        record = (
            parse_tax_debt_document(
                element
            )
        )

        yield record

        element.clear()
        root.clear()


# =========================================================
# FULL ZIP IMPORT
# =========================================================


def import_fns_tax_debt_zip(
    zip_path,
    batch_size=DEFAULT_BATCH_SIZE,
    progress_every=100000,
):
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
        "valid": 0,
        "invalid": 0,
        "matched": 0,
        "unmatched": 0,
        "snapshots": 0,
        "items": 0,
        "xml_files": 0,
        "data_date": None,
    }

    batch = []

    raw_connection = (
        engine.raw_connection()
    )

    try:

        cursor = (
            raw_connection.cursor()
        )

        create_temp_tables(
            cursor
        )

        raw_connection.commit()

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
                    "В ZIP не найдено XML"
                )

            print(
                "XML-файлов:",
                len(xml_files),
            )

            print(
                "Batch size:",
                batch_size,
            )

            print()

            for (
                file_number,
                info,
            ) in enumerate(
                xml_files,
                start=1,
            ):

                totals[
                    "xml_files"
                ] += 1

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

                        if record is None:

                            totals[
                                "invalid"
                            ] += 1

                            continue

                        totals[
                            "valid"
                        ] += 1

                        data_date = (
                            record[
                                "data_date"
                            ]
                        )

                        if (
                            totals[
                                "data_date"
                            ]
                            is None
                            or data_date
                            > totals[
                                "data_date"
                            ]
                        ):

                            totals[
                                "data_date"
                            ] = (
                                data_date
                            )

                        batch.append(
                            record
                        )

                        if (
                            len(batch)
                            >= batch_size
                        ):

                            result = (
                                process_batch(
                                    raw_connection=(
                                        raw_connection
                                    ),
                                    cursor=cursor,
                                    records=batch,
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
                                "unmatched"
                            ] += result[
                                "unmatched"
                            ]

                            totals[
                                "snapshots"
                            ] += result[
                                "snapshots"
                            ]

                            totals[
                                "items"
                            ] += result[
                                "items"
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
                                "| matched:",
                                totals[
                                    "matched"
                                ],
                                "| snapshots:",
                                totals[
                                    "snapshots"
                                ],
                                "| items:",
                                totals[
                                    "items"
                                ],
                                "| invalid:",
                                totals[
                                    "invalid"
                                ],
                            )

                if (
                    file_number
                    % 250
                    == 0
                ):

                    print(
                        "XML обработано:",
                        file_number,
                        "/",
                        len(
                            xml_files
                        ),
                    )

            if batch:

                result = process_batch(
                    raw_connection=(
                        raw_connection
                    ),
                    cursor=cursor,
                    records=batch,
                    dataset_id=(
                        dataset_id
                    ),
                )

                totals[
                    "matched"
                ] += result[
                    "matched"
                ]

                totals[
                    "unmatched"
                ] += result[
                    "unmatched"
                ]

                totals[
                    "snapshots"
                ] += result[
                    "snapshots"
                ]

                totals[
                    "items"
                ] += result[
                    "items"
                ]

                batch.clear()

        cursor.close()

    finally:

        raw_connection.close()

    if totals["invalid"]:
        raise RuntimeError(
            "FNS Tax Debt snapshot contains "
            f"{totals['invalid']} invalid documents; "
            "dataset date was not published"
        )

    return totals
