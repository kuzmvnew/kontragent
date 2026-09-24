from datetime import datetime
from decimal import (
    Decimal,
    InvalidOperation,
)
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from sqlalchemy import select

from app.database.postgres import (
    engine,
    get_session,
)
from app.models.company import Company
from app.models.revenue_expense import (
    CompanyRevenueExpenseSnapshot,
)
from app.models.source import DataSet


DATASET_CODE = (
    "fns_revenue_expenses"
)

SOURCE_URL = (
    "https://www.nalog.gov.ru/"
    "opendata/7707329152-revexp/"
)

DEFAULT_BATCH_SIZE = 10000

ZERO = Decimal("0.00")

MONEY_QUANT = Decimal("0.01")


def local_name(
    tag,
):
    """
    Убирает XML namespace
    из имени тега.
    """

    return tag.rsplit(
        "}",
        1,
    )[-1]


def parse_date(
    value,
):
    """
    Разбирает даты ФНС:

    31.12.2025
    2025-12-31
    """

    if not value:
        return None

    text = str(
        value
    ).strip()

    for date_format in (
        "%d.%m.%Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(
                text,
                date_format,
            ).date()

        except ValueError:
            pass

    return None


def parse_decimal(
    value,
):
    """
    Преобразует денежную сумму
    из XML ФНС в Decimal.

    Поддерживает точку и запятую.
    """

    if value is None:
        return None

    text = (
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

    if not text:
        return None

    try:
        return Decimal(
            text
        ).quantize(
            MONEY_QUANT
        )

    except InvalidOperation:
        return None


def parse_revenue_expense_document(
    document,
):
    """
    Разбирает один документ REVEXP.

    Фактическая структура:

    <Документ
        ИдДок="..."
        ДатаДок="25.08.2026"
        ДатаСост="31.12.2025"
    >
        <СведНП
            ИННЮЛ="..."
            НаимОрг="..."
        />

        <СведДохРасх
            СумДоход="..."
            СумРасход="..."
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
    revenue = None
    expenses = None

    for element in document:

        tag = local_name(
            element.tag
        )

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

        elif tag == "СведДохРасх":

            revenue = parse_decimal(
                element.attrib.get(
                    "СумДоход"
                )
            )

            expenses = parse_decimal(
                element.attrib.get(
                    "СумРасход"
                )
            )

    if (
        not document_id
        or not inn
        or data_date is None
    ):
        return None

    inn = str(
        inn
    ).strip()

    # REVEXP содержит сведения
    # о юридических лицах.
    if (
        len(inn) != 10
        or not inn.isdigit()
    ):
        return None

    if (
        revenue is None
        or expenses is None
    ):
        return None

    profit_loss = (
        revenue
        - expenses
    ).quantize(
        MONEY_QUANT
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
        "data_year": (
            data_date.year
        ),
        "revenue": revenue,
        "expenses": expenses,
        "profit_loss": (
            profit_loss
        ),
    }


def ensure_revenue_expense_table():
    """
    Создаёт таблицу REVEXP,
    если её ещё нет.

    Существующие таблицы
    не изменяются.
    """

    # Импорт Company нужен SQLAlchemy
    # для разрешения внешнего ключа
    # companies.id.
    _ = Company

    CompanyRevenueExpenseSnapshot.__table__.create(
        bind=engine,
        checkfirst=True,
    )


def get_dataset_id():
    """
    Получает зарегистрированный dataset
    и актуализирует его метаданные.
    """

    session = get_session()

    try:
        dataset = (
            session.execute(
                select(
                    DataSet
                )
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
                "не зарегистрирован. "
                "Сначала запусти: "
                "uv run python "
                "-m scripts.init_sources"
            )

        dataset.name = (
            "ФНС: Доходы и расходы"
        )

        dataset.domain = (
            "revenue_expenses"
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


def create_temp_table(
    cursor,
):
    """
    Создаёт временную таблицу
    для быстрого PostgreSQL COPY.
    """

    cursor.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS
        tmp_fns_revenue_expense
        (
            inn TEXT NOT NULL,
            company_name TEXT,
            document_id TEXT NOT NULL,
            document_date DATE,
            data_date DATE NOT NULL,
            data_year INTEGER NOT NULL,
            revenue NUMERIC(22, 2) NOT NULL,
            expenses NUMERIC(22, 2) NOT NULL,
            profit_loss NUMERIC(22, 2) NOT NULL
        )
        ON COMMIT PRESERVE ROWS
        """
    )


def process_batch(
    raw_connection,
    cursor,
    records,
    dataset_id,
):
    """
    Сопоставляет записи с companies
    и выполняет UPSERT.
    """

    if not records:
        return {
            "matched": 0,
            "unmatched": 0,
            "inserted": 0,
            "updated": 0,
        }

    # Внутри batch оставляем
    # одну запись на ИНН и дату.
    records_by_key = {
        (
            record["inn"],
            record["data_date"],
        ): record
        for record in records
    }

    records = list(
        records_by_key.values()
    )

    try:
        cursor.execute(
            """
            TRUNCATE
            tmp_fns_revenue_expense
            """
        )

        with cursor.copy(
            """
            COPY tmp_fns_revenue_expense
            (
                inn,
                company_name,
                document_id,
                document_date,
                data_date,
                data_year,
                revenue,
                expenses,
                profit_loss
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
                            "revenue"
                        ],
                        record[
                            "expenses"
                        ],
                        record[
                            "profit_loss"
                        ],
                    )
                )

        cursor.execute(
            """
            SELECT COUNT(*)

            FROM tmp_fns_revenue_expense t

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

            FROM tmp_fns_revenue_expense t

            JOIN companies c
              ON c.inn = t.inn

            JOIN company_revenue_expense_snapshots s
              ON s.company_id = c.id
             AND s.dataset_id = %s
             AND s.data_date = t.data_date
            """,
            (
                dataset_id,
            ),
        )

        updated = (
            cursor.fetchone()[0]
        )

        inserted = (
            matched
            - updated
        )

        cursor.execute(
            """
            INSERT INTO
            company_revenue_expense_snapshots
            (
                company_id,
                dataset_id,
                data_date,
                data_year,
                document_date,
                source_document_id,
                source_company_name,
                revenue,
                expenses,
                profit_loss
            )

            SELECT
                c.id,
                %s,
                t.data_date,
                t.data_year,
                t.document_date,
                t.document_id,
                t.company_name,
                t.revenue,
                t.expenses,
                t.profit_loss

            FROM tmp_fns_revenue_expense t

            JOIN companies c
              ON c.inn = t.inn

            ON CONFLICT ON CONSTRAINT
            uq_company_revexp_company_dataset_date

            DO UPDATE SET

                data_year =
                    EXCLUDED.data_year,

                document_date =
                    EXCLUDED.document_date,

                source_document_id =
                    EXCLUDED.source_document_id,

                source_company_name =
                    EXCLUDED.source_company_name,

                revenue =
                    EXCLUDED.revenue,

                expenses =
                    EXCLUDED.expenses,

                profit_loss =
                    EXCLUDED.profit_loss,

                updated_at =
                    NOW()
            """,
            (
                dataset_id,
            ),
        )

        raw_connection.commit()

        return {
            "matched": matched,
            "unmatched": unmatched,
            "inserted": inserted,
            "updated": updated,
        }

    except Exception:
        raw_connection.rollback()
        raise


def iter_xml_records(
    xml_file,
):
    """
    Потоково читает XML.

    Весь XML-файл в память
    не загружается.
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

        yield (
            parse_revenue_expense_document(
                element
            )
        )

        element.clear()
        root.clear()


def import_fns_revenue_expense_zip(
    zip_path,
    batch_size=DEFAULT_BATCH_SIZE,
    progress_every=100000,
):
    """
    Импортирует официальный ZIP REVEXP.

    ZIP читается напрямую.
    Распаковка на диск не требуется.
    """

    path = Path(
        zip_path
    )

    if not path.is_file():
        raise FileNotFoundError(
            f"ZIP не найден: {path}"
        )

    if batch_size < 1:
        raise ValueError(
            "batch_size должен "
            "быть больше нуля"
        )

    ensure_revenue_expense_table()

    dataset_id = get_dataset_id()

    totals = {
        "rows_read": 0,
        "valid": 0,
        "invalid": 0,
        "matched": 0,
        "unmatched": 0,
        "inserted": 0,
        "updated": 0,
        "xml_files": 0,
        "data_date": None,
        "revenue_total": ZERO,
        "expenses_total": ZERO,
        "profit_loss_total": ZERO,
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
                "XML-файлов:",
                len(xml_files),
            )

            print(
                "Batch size:",
                batch_size,
            )

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
                            "revenue_total"
                        ] += record[
                            "revenue"
                        ]

                        totals[
                            "expenses_total"
                        ] += record[
                            "expenses"
                        ]

                        totals[
                            "profit_loss_total"
                        ] += record[
                            "profit_loss"
                        ]

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
                            ] = data_date

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

                            for key in (
                                "matched",
                                "unmatched",
                                "inserted",
                                "updated",
                            ):
                                totals[
                                    key
                                ] += result[
                                    key
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
                                "| invalid:",
                                totals[
                                    "invalid"
                                ],
                            )

                if (
                    file_number
                    % 100
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

                for key in (
                    "matched",
                    "unmatched",
                    "inserted",
                    "updated",
                ):
                    totals[
                        key
                    ] += result[
                        key
                    ]

                batch.clear()

        cursor.close()

    finally:
        raw_connection.close()

    return totals


# =========================================================
# WORKER FOUNDATION / OFFICIAL RELEASE SUPERVISION
# =========================================================


SOURCE_ID = "fns_revenue_expenses"
HANDLER_VERSION = "s03-official-v1"


def _worker_spec():
    from app.ingestion.fns_bulk_worker import FnsBulkSourceSpec

    return FnsBulkSourceSpec(
        source_id=SOURCE_ID,
        dataset_code=DATASET_CODE,
        source_page_url=SOURCE_URL,
        source_path="7707329152-revexp",
        handler_version=HANDLER_VERSION,
        kind="revenue_expense",
        api_projection="revenue_expense_check",
        card_projection="company_card.revenue_expense",
    )


def fns_revenue_expense_worker_handler(context):
    """Download and normalize the current S03 release in an isolated worker."""

    from app.ingestion.fns_bulk_worker import run_bulk_handler

    return run_bulk_handler(
        context,
        spec=_worker_spec(),
        iterator=iter_xml_records,
    )


def publish_fns_revenue_expense_worker_result(session, claim, result):
    """Atomically publish S03 normalized facts by exact company INN."""

    from app.ingestion.fns_bulk_worker import publish_bulk_result

    return publish_bulk_result(session, claim, result, spec=_worker_spec())


def register_fns_revenue_expense_worker(session, registry):
    from app.ingestion.fns_bulk_worker import register_bulk_handler

    return register_bulk_handler(
        session,
        registry,
        spec=_worker_spec(),
        handler=fns_revenue_expense_worker_handler,
        publisher=publish_fns_revenue_expense_worker_result,
    )


def enqueue_fns_revenue_expense_release(session, *, release, raw_root, **kwargs):
    from app.ingestion.fns_bulk_worker import enqueue_bulk_release

    return enqueue_bulk_release(
        session,
        spec=_worker_spec(),
        release=release,
        raw_root=Path(raw_root),
        **kwargs,
    )


def schedule_fns_revenue_expense_check(session, *, raw_root, now=None):
    from app.ingestion.fns_bulk_worker import schedule_source_check

    return schedule_source_check(
        session,
        spec=_worker_spec(),
        raw_root=Path(raw_root),
        now=now,
    )
