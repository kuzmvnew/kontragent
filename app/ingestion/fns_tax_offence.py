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


DATASET_CODE = "fns_tax_offence"

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

    text = (
        str(value)
        .strip()
        .replace(
            ",",
            ".",
        )
    )

    if not text:
        return ZERO

    try:
        return Decimal(
            text
        ).quantize(
            Decimal("0.01")
        )

    except InvalidOperation:
        return ZERO


# =========================================================
# DOCUMENT PARSER
# =========================================================


def parse_tax_offence_document(
    document,
):
    """
    Разбирает один <Документ> ФНС.

    Реальная структура текущего XML:

    <Документ
        ИдДок="..."
        ДатаДок="02.12.2025"
        ДатаСост="31.12.2024"
    >
        <СведНП
            НаимОрг="..."
            ИННЮЛ="..."
        />

        <СведНаруш
            СумШтраф="..."
        />
    </Документ>
    """

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
    fine_amount = ZERO

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
        # TAX OFFENCE
        # -------------------------------------------------

        elif tag == "СведНаруш":

            fine_amount += (
                parse_decimal(
                    element.attrib.get(
                        "СумШтраф"
                    )
                )
            )

    if not document_id:
        return None

    if not inn:
        return None

    inn = str(
        inn
    ).strip()

    # Этот официальный набор относится
    # к юридическим лицам.
    if (
        len(inn) != 10
        or not inn.isdigit()
    ):
        return None

    if data_date is None:
        return None

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
        "fine_amount": (
            fine_amount
        ),
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
                "не зарегистрирован."
            )

        return dataset_id

    finally:
        session.close()


# =========================================================
# TEMP TABLE
# =========================================================


def create_temp_table(
    cursor,
):
    cursor.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS
        tmp_fns_tax_offence
        (
            inn TEXT NOT NULL,
            document_id TEXT NOT NULL,
            document_date DATE,
            data_date DATE NOT NULL,
            fine_amount NUMERIC(20, 2) NOT NULL
        )
        ON COMMIT PRESERVE ROWS
        """
    )


# =========================================================
# BATCH PROCESSOR
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
            "saved": 0,
        }

    # Если один и тот же ИдДок случайно
    # встретился в batch повторно,
    # оставляем одну запись.
    by_document_id = {}

    for record in records:

        by_document_id[
            record["document_id"]
        ] = record

    records = list(
        by_document_id.values()
    )

    try:
        cursor.execute(
            """
            TRUNCATE tmp_fns_tax_offence
            """
        )

        # -------------------------------------------------
        # COPY INTO TEMP TABLE
        # -------------------------------------------------

        with cursor.copy(
            """
            COPY tmp_fns_tax_offence
            (
                inn,
                document_id,
                document_date,
                data_date,
                fine_amount
            )
            FROM STDIN
            """
        ) as copy:

            for record in records:

                copy.write_row(
                    (
                        record[
                            "inn"
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
                            "fine_amount"
                        ],
                    )
                )

        # -------------------------------------------------
        # MATCH STATISTICS
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM tmp_fns_tax_offence t

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
        # UPSERT
        # -------------------------------------------------

        cursor.execute(
            """
            INSERT INTO company_tax_offences
            (
                company_id,
                dataset_id,
                data_date,
                document_date,
                source_document_id,
                fine_amount
            )

            SELECT
                c.id,
                %s,
                t.data_date,
                t.document_date,
                t.document_id,
                t.fine_amount

            FROM tmp_fns_tax_offence t

            JOIN companies c
              ON c.inn = t.inn

            ON CONFLICT
            (
                dataset_id,
                source_document_id
            )

            DO UPDATE SET

                company_id =
                    EXCLUDED.company_id,

                data_date =
                    EXCLUDED.data_date,

                document_date =
                    EXCLUDED.document_date,

                fine_amount =
                    EXCLUDED.fine_amount,

                updated_at =
                    NOW()
            """,
            (
                dataset_id,
            ),
        )

        raw_connection.commit()

        return {
            "matched": (
                matched
            ),
            "unmatched": (
                unmatched
            ),
            "saved": (
                matched
            ),
        }

    except Exception:
        raw_connection.rollback()
        raise


# =========================================================
# XML STREAM
# =========================================================


def iter_xml_records(
    xml_file,
):
    """
    Читает XML потоково.

    Весь XML-файл не загружается
    целиком в оперативную память.
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

        record = (
            parse_tax_offence_document(
                element
            )
        )

        yield record

        element.clear()
        root.clear()


# =========================================================
# ZIP IMPORT
# =========================================================


def import_fns_tax_offence_zip(
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
        "saved": 0,
        "xml_files": 0,
        "data_date": None,
        "fine_total": ZERO,
    }

    batch = []

    raw_connection = (
        engine.raw_connection()
    )

    try:
        cursor = (
            raw_connection.cursor()
        )

        create_temp_table(
            cursor
        )

        raw_connection.commit()

        with ZipFile(
            path,
            "r",
        ) as archive:

            xml_files = [
                info
                for info in archive.infolist()
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

                        totals[
                            "fine_total"
                        ] += (
                            record[
                                "fine_amount"
                            ]
                        )

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
                            ] += (
                                result[
                                    "matched"
                                ]
                            )

                            totals[
                                "unmatched"
                            ] += (
                                result[
                                    "unmatched"
                                ]
                            )

                            totals[
                                "saved"
                            ] += (
                                result[
                                    "saved"
                                ]
                            )

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
                                "| saved:",
                                totals[
                                    "saved"
                                ],
                                "| invalid:",
                                totals[
                                    "invalid"
                                ],
                            )

                if (
                    file_number
                    % 25
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

            # Последний неполный batch.
            if batch:

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
                ] += (
                    result[
                        "matched"
                    ]
                )

                totals[
                    "unmatched"
                ] += (
                    result[
                        "unmatched"
                    ]
                )

                totals[
                    "saved"
                ] += (
                    result[
                        "saved"
                    ]
                )

                batch.clear()

        cursor.close()

    finally:
        raw_connection.close()

    return totals