from datetime import datetime, timedelta
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


# =========================================================
# SETTINGS
# =========================================================


DATASET_CODE = "fns_tax_paid"

SOURCE_URL = (
    "https://www.nalog.gov.ru/"
    "opendata/7707329152-paytax/"
)

DEFAULT_BATCH_SIZE = 10000

ZERO = Decimal("0.00")

MONEY_QUANT = Decimal("0.01")


# =========================================================
# HELPERS
# =========================================================


def local_name(tag):
    """
    Убирает XML namespace.
    """

    if "}" in tag:
        return tag.rsplit(
            "}",
            1,
        )[-1]

    return tag


def parse_date(
    value,
):
    """
    Формат ФНС:

    31.12.2025
    01.04.2026
    """

    if value is None:
        return None

    value = str(
        value
    ).strip()

    if not value:
        return None

    return datetime.strptime(
        value,
        "%d.%m.%Y",
    ).date()


def parse_decimal(
    value,
):
    """
    Безопасно преобразует сумму
    из XML ФНС в Decimal.
    """

    if value is None:
        return ZERO

    value = (
        str(value)
        .strip()
        .replace(
            " ",
            "",
        )
        .replace(
            ",",
            ".",
        )
    )

    if not value:
        return ZERO

    try:

        result = Decimal(
            value
        )

    except InvalidOperation as error:

        raise ValueError(
            f"Некорректная сумма: {value}"
        ) from error

    return result.quantize(
        MONEY_QUANT
    )


def classify_payment_type(
    tax_name,
):
    """
    Классифицирует строку PAYTAX ФНС.

    ВАЖНО:

    исходное название платежа
    всегда сохраняется без изменений.

    Классификация используется только
    для аналитики, UI и Risk Engine.
    """

    normalized = " ".join(
        str(
            tax_name
        )
        .upper()
        .split()
    )

    # -----------------------------------------------------
    # PENALTIES
    #
    # Не используем простую проверку "ПЕН",
    # потому что тогда "ПЕНСИОННОЕ"
    # ошибочно станет penalty.
    # -----------------------------------------------------

    if (
        normalized == "СУММЫ ПЕНЕЙ"
        or " СУММЫ ПЕНЕЙ " in (
            f" {normalized} "
        )
    ):
        return "penalty"

    # -----------------------------------------------------
    # NON-TAX
    #
    # Проверяется раньше налогов,
    # потому что слово НЕНАЛОГОВЫЕ
    # содержит НАЛОГ.
    # -----------------------------------------------------

    if (
        "НЕНАЛОГОВ" in normalized
    ):
        return "non_tax"

    # -----------------------------------------------------
    # INSURANCE CONTRIBUTIONS
    # -----------------------------------------------------

    if (
        "СТРАХОВ" in normalized
        or "ВЗНОС" in normalized
    ):
        return "insurance"

    # -----------------------------------------------------
    # TAXES AND FEES
    # -----------------------------------------------------

    if (
        "НАЛОГ" in normalized
        or "СБОР" in normalized
        or "АКЦИЗ" in normalized
        or "ГОСУДАРСТВЕННАЯ ПОШЛИНА"
        in normalized
    ):
        return "tax"

    # -----------------------------------------------------
    # OTHER
    #
    # Например:
    # Проценты
    #
    # Пока не присваиваем таким строкам
    # более конкретную категорию без
    # подтвержденной семантики.
    # -----------------------------------------------------

    return "other"


# =========================================================
# DATASET
# =========================================================


def get_dataset_id():
    """
    Получает существующий dataset
    fns_tax_paid.

    Также поддерживает метаданные,
    необходимые будущему Auto Update Layer.
    """

    session = get_session()

    try:

        dataset = (
            session.execute(
                select(DataSet)
                .where(
                    DataSet.code
                    == DATASET_CODE
                )
            )
            .scalar_one_or_none()
        )

        if dataset is None:

            raise RuntimeError(
                "Dataset "
                f"{DATASET_CODE} "
                "не найден"
            )

        dataset.name = (
            "ФНС: Уплаченные налоги"
        )

        dataset.domain = (
            "taxes"
        )

        dataset.update_mode = (
            "bulk"
        )

        dataset.data_format = (
            "xml"
        )

        dataset.refresh_schedule = (
            "annual"
        )

        dataset.priority = 10

        dataset.source_url = (
            SOURCE_URL
        )

        session.commit()

        return dataset.id

    except Exception:

        session.rollback()

        raise

    finally:

        session.close()


# =========================================================
# PAYMENT TOTALS
# =========================================================


def build_totals(
    items,
):
    """
    Считает суммы по категориям.
    """

    totals = {
        "tax": ZERO,
        "insurance": ZERO,
        "penalty": ZERO,
        "non_tax": ZERO,
        "other": ZERO,
    }

    for item in items:

        payment_type = (
            item[
                "payment_type"
            ]
        )

        amount = (
            item[
                "amount"
            ]
        )

        totals[
            payment_type
        ] += amount

    total_amount = sum(
        totals.values(),
        ZERO,
    )

    return {
        "total_amount": (
            total_amount
        ),
        "tax_amount": (
            totals[
                "tax"
            ]
        ),
        "insurance_amount": (
            totals[
                "insurance"
            ]
        ),
        "penalty_amount": (
            totals[
                "penalty"
            ]
        ),
        "non_tax_amount": (
            totals[
                "non_tax"
            ]
        ),
        "other_amount": (
            totals[
                "other"
            ]
        ),
    }


# =========================================================
# XML PARSER
# =========================================================


def parse_tax_payment_document(
    element,
):
    """
    Преобразует один <Документ>
    PAYTAX в нормализованную структуру.
    """

    document_id = (
        element.attrib.get(
            "ИдДок"
        )
    )

    document_date = parse_date(
        element.attrib.get(
            "ДатаДок"
        )
    )

    data_date = parse_date(
        element.attrib.get(
            "ДатаСост"
        )
    )

    inn = None

    company_name = None

    source_items = []

    source_item_count = 0

    for child in element:

        tag = local_name(
            child.tag
        )

        if tag == "СведНП":

            inn = (
                child.attrib.get(
                    "ИННЮЛ"
                )
            )

            company_name = (
                child.attrib.get(
                    "НаимОрг"
                )
            )

            if inn is not None:

                inn = str(
                    inn
                ).strip()

            if company_name is not None:

                company_name = str(
                    company_name
                ).strip()

        elif tag == "СвУплСумНал":

            source_item_count += 1

            tax_name = (
                child.attrib.get(
                    "НаимНалог"
                )
            )

            amount_raw = (
                child.attrib.get(
                    "СумУплНал"
                )
            )

            if tax_name is None:
                return None

            tax_name = str(
                tax_name
            ).strip()

            if not tax_name:
                return None

            if len(tax_name) > 1000:
                return None

            try:

                amount = (
                    parse_decimal(
                        amount_raw
                    )
                )

            except ValueError:

                return None

            payment_type = (
                classify_payment_type(
                    tax_name
                )
            )

            source_items.append(
                {
                    "tax_name": (
                        tax_name
                    ),
                    "payment_type": (
                        payment_type
                    ),
                    "amount": (
                        amount
                    ),
                }
            )

    if inn is None:
        return None

    if (
        len(inn) != 10
        or not inn.isdigit()
    ):
        return None

    if not document_id:
        return None

    document_id = str(
        document_id
    ).strip()

    if not document_id:
        return None

    if data_date is None:
        return None

    # -----------------------------------------------------
    # MERGE DUPLICATED PAYMENT NAMES
    # -----------------------------------------------------

    aggregated = {}

    for item in source_items:

        tax_name = (
            item[
                "tax_name"
            ]
        )

        if tax_name not in aggregated:

            aggregated[
                tax_name
            ] = {
                "tax_name": (
                    tax_name
                ),
                "payment_type": (
                    item[
                        "payment_type"
                    ]
                ),
                "amount": ZERO,
            }

        aggregated[
            tax_name
        ][
            "amount"
        ] += (
            item[
                "amount"
            ]
        )

    all_items = list(
        aggregated.values()
    )

    totals = build_totals(
        all_items
    )

    stored_items = [
        item
        for item in all_items
        if item[
            "amount"
        ] != ZERO
    ]

    return {
        "inn": (
            inn
        ),
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
        "data_year": (
            data_date.year
        ),
        "total_amount": (
            totals[
                "total_amount"
            ]
        ),
        "tax_amount": (
            totals[
                "tax_amount"
            ]
        ),
        "insurance_amount": (
            totals[
                "insurance_amount"
            ]
        ),
        "penalty_amount": (
            totals[
                "penalty_amount"
            ]
        ),
        "non_tax_amount": (
            totals[
                "non_tax_amount"
            ]
        ),
        "other_amount": (
            totals[
                "other_amount"
            ]
        ),
        "source_item_count": (
            source_item_count
        ),
        "stored_item_count": (
            len(
                stored_items
            )
        ),
        "items": (
            stored_items
        ),
    }


# =========================================================
# TEMP TABLES
# =========================================================


def create_temp_tables(
    cursor,
):
    """
    Создаёт временные staging-таблицы.
    """

    cursor.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS
        tmp_fns_tax_payment_docs
        (
            inn VARCHAR(10) NOT NULL,
            company_name TEXT,
            document_id VARCHAR(100) NOT NULL,
            document_date DATE,
            data_date DATE NOT NULL,
            data_year INTEGER NOT NULL,

            total_amount NUMERIC(22, 2)
                NOT NULL,

            tax_amount NUMERIC(22, 2)
                NOT NULL,

            insurance_amount NUMERIC(22, 2)
                NOT NULL,

            penalty_amount NUMERIC(22, 2)
                NOT NULL,

            non_tax_amount NUMERIC(22, 2)
                NOT NULL,

            other_amount NUMERIC(22, 2)
                NOT NULL,

            source_item_count INTEGER
                NOT NULL,

            stored_item_count INTEGER
                NOT NULL
        )
        ON COMMIT PRESERVE ROWS
        """
    )

    cursor.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS
        tmp_fns_tax_payment_items
        (
            inn VARCHAR(10) NOT NULL,
            data_date DATE NOT NULL,
            tax_name TEXT NOT NULL,
            payment_type VARCHAR(30)
                NOT NULL,
            amount NUMERIC(22, 2)
                NOT NULL
        )
        ON COMMIT PRESERVE ROWS
        """
    )


# =========================================================
# PROCESS BATCH
# =========================================================


def process_batch(
    raw_connection,
    cursor,
    records,
    dataset_id,
):
    """
    Загружает batch через PostgreSQL COPY,
    сопоставляет ИНН и делает UPSERT.
    """

    if not records:

        return {
            "matched": 0,
            "unmatched": 0,
            "snapshots_inserted": 0,
            "snapshots_updated": 0,
            "items_inserted": 0,
            "matched_total_amount": ZERO,
            "matched_tax_amount": ZERO,
            "matched_insurance_amount": ZERO,
            "matched_penalty_amount": ZERO,
            "matched_non_tax_amount": ZERO,
            "matched_other_amount": ZERO,
        }

    try:

        cursor.execute(
            """
            TRUNCATE
            tmp_fns_tax_payment_docs
            """
        )

        cursor.execute(
            """
            TRUNCATE
            tmp_fns_tax_payment_items
            """
        )

        with cursor.copy(
            """
            COPY tmp_fns_tax_payment_docs
            (
                inn,
                company_name,
                document_id,
                document_date,
                data_date,
                data_year,
                total_amount,
                tax_amount,
                insurance_amount,
                penalty_amount,
                non_tax_amount,
                other_amount,
                source_item_count,
                stored_item_count
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
                            "data_year"
                        ],
                        record[
                            "total_amount"
                        ],
                        record[
                            "tax_amount"
                        ],
                        record[
                            "insurance_amount"
                        ],
                        record[
                            "penalty_amount"
                        ],
                        record[
                            "non_tax_amount"
                        ],
                        record[
                            "other_amount"
                        ],
                        record[
                            "source_item_count"
                        ],
                        record[
                            "stored_item_count"
                        ],
                    )
                )

        with cursor.copy(
            """
            COPY tmp_fns_tax_payment_items
            (
                inn,
                data_date,
                tax_name,
                payment_type,
                amount
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
                            record[
                                "inn"
                            ],
                            record[
                                "data_date"
                            ],
                            item[
                                "tax_name"
                            ],
                            item[
                                "payment_type"
                            ],
                            item[
                                "amount"
                            ],
                        )
                    )

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM tmp_fns_tax_payment_docs t
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

        cursor.execute(
            """
            SELECT COUNT(*)

            FROM tmp_fns_tax_payment_docs t

            JOIN companies c
              ON c.inn = t.inn

            JOIN company_tax_payment_snapshots s
              ON s.company_id = c.id
             AND s.dataset_id = %s
             AND s.data_date = t.data_date
            """,
            (
                dataset_id,
            ),
        )

        existing_snapshots = (
            cursor.fetchone()[0]
        )

        snapshots_updated = (
            existing_snapshots
        )

        snapshots_inserted = (
            matched
            - existing_snapshots
        )

        cursor.execute(
            """
            SELECT
                COALESCE(
                    SUM(t.total_amount),
                    0
                ),
                COALESCE(
                    SUM(t.tax_amount),
                    0
                ),
                COALESCE(
                    SUM(t.insurance_amount),
                    0
                ),
                COALESCE(
                    SUM(t.penalty_amount),
                    0
                ),
                COALESCE(
                    SUM(t.non_tax_amount),
                    0
                ),
                COALESCE(
                    SUM(t.other_amount),
                    0
                )

            FROM tmp_fns_tax_payment_docs t

            JOIN companies c
              ON c.inn = t.inn
            """
        )

        (
            matched_total_amount,
            matched_tax_amount,
            matched_insurance_amount,
            matched_penalty_amount,
            matched_non_tax_amount,
            matched_other_amount,
        ) = cursor.fetchone()

        cursor.execute(
            """
            INSERT INTO
            company_tax_payment_snapshots
            (
                company_id,
                dataset_id,
                data_date,
                data_year,
                document_date,
                source_document_id,
                source_company_name,

                total_amount,
                tax_amount,
                insurance_amount,
                penalty_amount,
                non_tax_amount,
                other_amount,

                source_item_count,
                stored_item_count
            )

            SELECT
                c.id,
                %s,
                t.data_date,
                t.data_year,
                t.document_date,
                t.document_id,
                t.company_name,

                t.total_amount,
                t.tax_amount,
                t.insurance_amount,
                t.penalty_amount,
                t.non_tax_amount,
                t.other_amount,

                t.source_item_count,
                t.stored_item_count

            FROM tmp_fns_tax_payment_docs t

            JOIN companies c
              ON c.inn = t.inn

            ON CONFLICT
            (
                company_id,
                dataset_id,
                data_date
            )

            DO UPDATE SET

                data_year =
                    EXCLUDED.data_year,

                document_date =
                    EXCLUDED.document_date,

                source_document_id =
                    EXCLUDED.source_document_id,

                source_company_name =
                    EXCLUDED.source_company_name,

                total_amount =
                    EXCLUDED.total_amount,

                tax_amount =
                    EXCLUDED.tax_amount,

                insurance_amount =
                    EXCLUDED.insurance_amount,

                penalty_amount =
                    EXCLUDED.penalty_amount,

                non_tax_amount =
                    EXCLUDED.non_tax_amount,

                other_amount =
                    EXCLUDED.other_amount,

                source_item_count =
                    EXCLUDED.source_item_count,

                stored_item_count =
                    EXCLUDED.stored_item_count,

                updated_at = NOW()
            """,
            (
                dataset_id,
            ),
        )

        cursor.execute(
            """
            DELETE FROM
                company_tax_payment_items i

            USING
                company_tax_payment_snapshots s,
                tmp_fns_tax_payment_docs t,
                companies c

            WHERE
                i.snapshot_id = s.id

                AND s.company_id = c.id

                AND c.inn = t.inn

                AND s.dataset_id = %s

                AND s.data_date =
                    t.data_date
            """,
            (
                dataset_id,
            ),
        )

        cursor.execute(
            """
            INSERT INTO
            company_tax_payment_items
            (
                snapshot_id,
                tax_name,
                payment_type,
                amount
            )

            SELECT
                s.id,
                t.tax_name,
                t.payment_type,
                SUM(t.amount)

            FROM tmp_fns_tax_payment_items t

            JOIN companies c
              ON c.inn = t.inn

            JOIN company_tax_payment_snapshots s
              ON s.company_id = c.id
             AND s.dataset_id = %s
             AND s.data_date = t.data_date

            GROUP BY
                s.id,
                t.tax_name,
                t.payment_type

            HAVING
                SUM(t.amount) <> 0

            ON CONFLICT
            (
                snapshot_id,
                tax_name
            )

            DO UPDATE SET

                payment_type =
                    EXCLUDED.payment_type,

                amount =
                    EXCLUDED.amount,

                updated_at = NOW()
            """,
            (
                dataset_id,
            ),
        )

        items_inserted = (
            cursor.rowcount
        )

        raw_connection.commit()

        return {
            "matched": (
                matched
            ),
            "unmatched": (
                unmatched
            ),
            "snapshots_inserted": (
                snapshots_inserted
            ),
            "snapshots_updated": (
                snapshots_updated
            ),
            "items_inserted": (
                items_inserted
            ),
            "matched_total_amount": (
                matched_total_amount
            ),
            "matched_tax_amount": (
                matched_tax_amount
            ),
            "matched_insurance_amount": (
                matched_insurance_amount
            ),
            "matched_penalty_amount": (
                matched_penalty_amount
            ),
            "matched_non_tax_amount": (
                matched_non_tax_amount
            ),
            "matched_other_amount": (
                matched_other_amount
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
    """
    Потоково читает XML.
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
            parse_tax_payment_document(
                element
            )
        )

        yield record

        element.clear()

        root.clear()


# =========================================================
# FULL ZIP IMPORT
# =========================================================


def import_fns_tax_payment_zip(
    zip_path,
    batch_size=DEFAULT_BATCH_SIZE,
    progress_every=100000,
):
    """
    Полный массовый импорт PAYTAX.
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
        "valid": 0,
        "invalid": 0,

        "matched": 0,
        "unmatched": 0,

        "snapshots_inserted": 0,
        "snapshots_updated": 0,

        "source_items": 0,
        "stored_source_items": 0,
        "items_inserted": 0,

        "xml_files": 0,

        "data_date": None,
        "document_date": None,

        "source_total_amount": ZERO,
        "source_tax_amount": ZERO,
        "source_insurance_amount": ZERO,
        "source_penalty_amount": ZERO,
        "source_non_tax_amount": ZERO,
        "source_other_amount": ZERO,

        "matched_total_amount": ZERO,
        "matched_tax_amount": ZERO,
        "matched_insurance_amount": ZERO,
        "matched_penalty_amount": ZERO,
        "matched_non_tax_amount": ZERO,
        "matched_other_amount": ZERO,
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
                len(
                    xml_files
                ),
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
                            "source_items"
                        ] += (
                            record[
                                "source_item_count"
                            ]
                        )

                        totals[
                            "stored_source_items"
                        ] += (
                            record[
                                "stored_item_count"
                            ]
                        )

                        totals[
                            "source_total_amount"
                        ] += (
                            record[
                                "total_amount"
                            ]
                        )

                        totals[
                            "source_tax_amount"
                        ] += (
                            record[
                                "tax_amount"
                            ]
                        )

                        totals[
                            "source_insurance_amount"
                        ] += (
                            record[
                                "insurance_amount"
                            ]
                        )

                        totals[
                            "source_penalty_amount"
                        ] += (
                            record[
                                "penalty_amount"
                            ]
                        )

                        totals[
                            "source_non_tax_amount"
                        ] += (
                            record[
                                "non_tax_amount"
                            ]
                        )

                        totals[
                            "source_other_amount"
                        ] += (
                            record[
                                "other_amount"
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

                        document_date = (
                            record[
                                "document_date"
                            ]
                        )

                        if (
                            document_date
                            is not None
                            and (
                                totals[
                                    "document_date"
                                ]
                                is None
                                or document_date
                                > totals[
                                    "document_date"
                                ]
                            )
                        ):

                            totals[
                                "document_date"
                            ] = (
                                document_date
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
                                "snapshots_inserted"
                            ] += result[
                                "snapshots_inserted"
                            ]

                            totals[
                                "snapshots_updated"
                            ] += result[
                                "snapshots_updated"
                            ]

                            totals[
                                "items_inserted"
                            ] += result[
                                "items_inserted"
                            ]

                            totals[
                                "matched_total_amount"
                            ] += result[
                                "matched_total_amount"
                            ]

                            totals[
                                "matched_tax_amount"
                            ] += result[
                                "matched_tax_amount"
                            ]

                            totals[
                                "matched_insurance_amount"
                            ] += result[
                                "matched_insurance_amount"
                            ]

                            totals[
                                "matched_penalty_amount"
                            ] += result[
                                "matched_penalty_amount"
                            ]

                            totals[
                                "matched_non_tax_amount"
                            ] += result[
                                "matched_non_tax_amount"
                            ]

                            totals[
                                "matched_other_amount"
                            ] += result[
                                "matched_other_amount"
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
                                "| inserted:",
                                totals[
                                    "snapshots_inserted"
                                ],
                                "| updated:",
                                totals[
                                    "snapshots_updated"
                                ],
                                "| items:",
                                totals[
                                    "items_inserted"
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
                    "snapshots_inserted"
                ] += result[
                    "snapshots_inserted"
                ]

                totals[
                    "snapshots_updated"
                ] += result[
                    "snapshots_updated"
                ]

                totals[
                    "items_inserted"
                ] += result[
                    "items_inserted"
                ]

                totals[
                    "matched_total_amount"
                ] += result[
                    "matched_total_amount"
                ]

                totals[
                    "matched_tax_amount"
                ] += result[
                    "matched_tax_amount"
                ]

                totals[
                    "matched_insurance_amount"
                ] += result[
                    "matched_insurance_amount"
                ]

                totals[
                    "matched_penalty_amount"
                ] += result[
                    "matched_penalty_amount"
                ]

                totals[
                    "matched_non_tax_amount"
                ] += result[
                    "matched_non_tax_amount"
                ]

                totals[
                    "matched_other_amount"
                ] += result[
                    "matched_other_amount"
                ]

                batch.clear()

        cursor.close()

    finally:

        raw_connection.close()

    return totals


# =========================================================
# WORKER FOUNDATION / OFFICIAL RELEASE SUPERVISION
# =========================================================


SOURCE_ID = "fns_tax_paid"
HANDLER_VERSION = "paytax-official-v1"


def _worker_spec():
    from app.ingestion.fns_bulk_worker import FnsBulkSourceSpec

    return FnsBulkSourceSpec(
        source_id=SOURCE_ID,
        dataset_code=DATASET_CODE,
        source_page_url=SOURCE_URL,
        source_path="7707329152-paytax",
        handler_version=HANDLER_VERSION,
        kind="tax_payment",
        api_projection="tax_payment_check",
        card_projection="company_card.tax_payment",
        check_interval=timedelta(days=7),
        check_frequency="weekly",
    )


def fns_tax_payment_worker_handler(context):
    """Download and normalize the current PAYTAX release in an isolated worker."""

    from app.ingestion.fns_bulk_worker import run_bulk_handler

    return run_bulk_handler(
        context,
        spec=_worker_spec(),
        iterator=iter_xml_records,
    )


def publish_fns_tax_payment_worker_result(session, claim, result):
    """Atomically publish PAYTAX snapshots and items by exact company INN."""

    from app.ingestion.fns_bulk_worker import publish_bulk_result

    return publish_bulk_result(session, claim, result, spec=_worker_spec())


def register_fns_tax_payment_worker(session, registry):
    from app.ingestion.fns_bulk_worker import register_bulk_handler

    return register_bulk_handler(
        session,
        registry,
        spec=_worker_spec(),
        handler=fns_tax_payment_worker_handler,
        publisher=publish_fns_tax_payment_worker_result,
    )


def enqueue_fns_tax_payment_release(session, *, release, raw_root, **kwargs):
    from app.ingestion.fns_bulk_worker import enqueue_bulk_release

    return enqueue_bulk_release(
        session,
        spec=_worker_spec(),
        release=release,
        raw_root=Path(raw_root),
        **kwargs,
    )


def schedule_fns_tax_payment_check(session, *, raw_root, now=None):
    from app.ingestion.fns_bulk_worker import schedule_source_check

    return schedule_source_check(
        session,
        spec=_worker_spec(),
        raw_root=Path(raw_root),
        now=now,
    )
