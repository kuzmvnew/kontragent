from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from sqlalchemy import select

from app.database.postgres import (
    engine,
    get_session,
)
from app.models.source import DataSet


DATASET_CODE = "fns_msp"

DEFAULT_BATCH_SIZE = 20000


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


def safe_int(value):
    if value is None:
        return None

    try:
        return int(
            str(value).strip()
        )

    except (
        TypeError,
        ValueError,
    ):
        return None


# =========================================================
# NORMALIZATION
# =========================================================


def parse_msp_document(
    document,
):
    """
    Нормализует один <Документ>
    Реестра МСП ФНС.

    Поддерживает:
    - ЮЛ
    - ИП
    """

    document_id = (
        document.attrib.get(
            "ИдДок"
        )
    )

    data_date = parse_date(
        document.attrib.get(
            "ДатаСост"
        )
    )

    inclusion_date = parse_date(
        document.attrib.get(
            "ДатаВклМСП"
        )
    )

    subject_type_code = (
        document.attrib.get(
            "ВидСубМСП"
        )
    )

    category_code = (
        document.attrib.get(
            "КатСубМСП"
        )
    )

    is_new_code = (
        document.attrib.get(
            "ПризНовМСП"
        )
    )

    social_enterprise_code = (
        document.attrib.get(
            "СведСоцПред"
        )
    )

    employee_count = safe_int(
        document.attrib.get(
            "ССЧР"
        )
    )

    entity_type = None

    inn = None
    ogrn = None

    name = None
    short_name = None
    full_name = None

    region_code = None

    okved = None
    activity = None

    # -----------------------------------------------------
    # CHILDREN
    # -----------------------------------------------------

    for element in document.iter():

        tag = local_name(
            element.tag
        )

        # -------------------------------------------------
        # LEGAL ENTITY
        # -------------------------------------------------

        if tag == "ОргВклМСП":

            legal_inn = (
                element.attrib.get(
                    "ИННЮЛ"
                )
            )

            if legal_inn:

                entity_type = "legal"

                inn = str(
                    legal_inn
                ).strip()

                ogrn = (
                    element.attrib.get(
                        "ОГРН"
                    )
                )

                full_name = (
                    element.attrib.get(
                        "НаимОрг"
                    )
                )

                short_name = (
                    element.attrib.get(
                        "НаимОргСокр"
                    )
                )

                name = (
                    short_name
                    or full_name
                )

        # -------------------------------------------------
        # INDIVIDUAL ENTREPRENEUR
        # -------------------------------------------------

        elif tag == "ИПВклМСП":

            ip_inn = (
                element.attrib.get(
                    "ИННФЛ"
                )
            )

            if ip_inn:

                entity_type = (
                    "individual_entrepreneur"
                )

                inn = str(
                    ip_inn
                ).strip()

                ogrn = (
                    element.attrib.get(
                        "ОГРНИП"
                    )
                )

        elif (
            tag == "ФИОИП"
            and entity_type
            == "individual_entrepreneur"
        ):

            parts = [
                element.attrib.get(
                    "Фамилия"
                ),
                element.attrib.get(
                    "Имя"
                ),
                element.attrib.get(
                    "Отчество"
                ),
            ]

            fio = " ".join(
                part.strip()
                for part in parts
                if part
                and part.strip()
            )

            if fio:

                name = (
                    f"ИП {fio}"
                )

                short_name = name

                full_name = (
                    "Индивидуальный "
                    f"предприниматель {fio}"
                )

        # -------------------------------------------------
        # LOCATION
        # -------------------------------------------------

        elif tag == "СведМН":

            region_code = (
                element.attrib.get(
                    "КодРегион"
                )
            )

        # -------------------------------------------------
        # MAIN OKVED
        # -------------------------------------------------

        elif tag == "СвОКВЭДОсн":

            okved = (
                element.attrib.get(
                    "КодОКВЭД"
                )
            )

            activity = (
                element.attrib.get(
                    "НаимОКВЭД"
                )
            )

    # =====================================================
    # VALIDATION
    # =====================================================

    if entity_type == "legal":

        if (
            not inn
            or len(inn) != 10
            or not inn.isdigit()
        ):
            return None

    elif (
        entity_type
        == "individual_entrepreneur"
    ):

        if (
            not inn
            or len(inn) != 12
            or not inn.isdigit()
        ):
            return None

    else:
        return None

    if not name:
        return None

    if data_date is None:
        return None

    return {
        "inn": inn,
        "entity_type": (
            entity_type
        ),
        "name": name,
        "short_name": (
            short_name
        ),
        "full_name": (
            full_name
        ),
        "ogrn": ogrn,
        "region_code": (
            region_code
        ),
        "okved": okved,
        "activity": activity,
        "data_date": (
            data_date
        ),
        "inclusion_date": (
            inclusion_date
        ),
        "subject_type_code": (
            subject_type_code
        ),
        "category_code": (
            category_code
        ),
        "is_new_code": (
            is_new_code
        ),
        "social_enterprise_code": (
            social_enterprise_code
        ),
        "employee_count": (
            employee_count
        ),
        "document_id": (
            document_id
        ),
    }


# =========================================================
# DATASET INFO
# =========================================================


def get_dataset_info():
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

        return {
            "id": dataset.id,
            "priority": (
                dataset.priority
            ),
        }

    finally:
        session.close()


# =========================================================
# TEMP STAGING
# =========================================================


def create_temp_table(
    cursor,
):
    cursor.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS
        tmp_fns_msp
        (
            inn TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            company_name TEXT NOT NULL,
            short_name TEXT,
            full_name TEXT,
            ogrn TEXT,
            region_code TEXT,
            okved TEXT,
            activity TEXT,
            data_date DATE NOT NULL,
            inclusion_date DATE,
            subject_type_code TEXT,
            category_code TEXT,
            is_new_code TEXT,
            social_enterprise_code TEXT,
            employee_count INTEGER,
            document_id TEXT
        )
        ON COMMIT PRESERVE ROWS
        """
    )


# =========================================================
# BATCH
# =========================================================


def process_batch(
    raw_connection,
    cursor,
    records,
    dataset_id,
    dataset_priority,
):
    if not records:

        return {
            "inserted": 0,
            "updated": 0,
            "profiles": 0,
        }

    # Внутри одного batch оставляем
    # последнюю запись по ИНН.
    by_inn = {
        record["inn"]: record
        for record in records
    }

    unique_records = list(
        by_inn.values()
    )

    try:

        cursor.execute(
            "TRUNCATE tmp_fns_msp"
        )

        with cursor.copy(
            """
            COPY tmp_fns_msp
            (
                inn,
                entity_type,
                company_name,
                short_name,
                full_name,
                ogrn,
                region_code,
                okved,
                activity,
                data_date,
                inclusion_date,
                subject_type_code,
                category_code,
                is_new_code,
                social_enterprise_code,
                employee_count,
                document_id
            )
            FROM STDIN
            """
        ) as copy:

            for record in unique_records:

                copy.write_row(
                    (
                        record["inn"],
                        record[
                            "entity_type"
                        ],
                        record["name"],
                        record[
                            "short_name"
                        ],
                        record[
                            "full_name"
                        ],
                        record["ogrn"],
                        record[
                            "region_code"
                        ],
                        record["okved"],
                        record[
                            "activity"
                        ],
                        record[
                            "data_date"
                        ],
                        record[
                            "inclusion_date"
                        ],
                        record[
                            "subject_type_code"
                        ],
                        record[
                            "category_code"
                        ],
                        record[
                            "is_new_code"
                        ],
                        record[
                            "social_enterprise_code"
                        ],
                        record[
                            "employee_count"
                        ],
                        record[
                            "document_id"
                        ],
                    )
                )

        # -------------------------------------------------
        # Сколько уже существовало ДО UPSERT.
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM tmp_fns_msp t
            JOIN companies c
              ON c.inn = t.inn
            """
        )

        existing_count = (
            cursor.fetchone()[0]
        )

        batch_count = len(
            unique_records
        )

        inserted_count = (
            batch_count
            - existing_count
        )

        updated_count = (
            existing_count
        )

        priority = int(
            dataset_priority
        )

        # -------------------------------------------------
        # MASTER COMPANIES UPSERT
        # -------------------------------------------------

        master_sql = f"""
        INSERT INTO companies
        (
            inn,
            entity_type,
            name,
            short_name,
            full_name,
            ogrn,
            region_code,
            okved,
            activity,
            master_dataset_id,
            master_data_date,
            source,
            source_updated_at
        )
        SELECT
            t.inn,
            t.entity_type,
            t.company_name,
            t.short_name,
            t.full_name,

            CASE
                WHEN t.ogrn IS NULL
                    THEN NULL

                WHEN EXISTS
                (
                    SELECT 1
                    FROM companies c2
                    WHERE c2.ogrn = t.ogrn
                      AND c2.inn <> t.inn
                )
                    THEN NULL

                ELSE t.ogrn
            END,

            t.region_code,
            t.okved,
            t.activity,
            %s,
            t.data_date,
            'fns',
            NOW()

        FROM tmp_fns_msp t

        ON CONFLICT (inn)
        DO UPDATE SET

            entity_type =
                CASE
                    WHEN
                        companies.master_dataset_id
                        IS NULL

                        OR {priority} <= COALESCE(
                            (
                                SELECT ds.priority
                                FROM data_sets ds
                                WHERE ds.id =
                                    companies.master_dataset_id
                            ),
                            999
                        )

                    THEN EXCLUDED.entity_type

                    ELSE COALESCE(
                        companies.entity_type,
                        EXCLUDED.entity_type
                    )
                END,

            name =
                CASE
                    WHEN
                        companies.master_dataset_id
                        IS NULL

                        OR {priority} <= COALESCE(
                            (
                                SELECT ds.priority
                                FROM data_sets ds
                                WHERE ds.id =
                                    companies.master_dataset_id
                            ),
                            999
                        )

                    THEN EXCLUDED.name

                    ELSE companies.name
                END,

            short_name =
                CASE
                    WHEN
                        companies.master_dataset_id
                        IS NULL

                        OR {priority} <= COALESCE(
                            (
                                SELECT ds.priority
                                FROM data_sets ds
                                WHERE ds.id =
                                    companies.master_dataset_id
                            ),
                            999
                        )

                    THEN COALESCE(
                        EXCLUDED.short_name,
                        companies.short_name
                    )

                    ELSE COALESCE(
                        companies.short_name,
                        EXCLUDED.short_name
                    )
                END,

            full_name =
                CASE
                    WHEN
                        companies.master_dataset_id
                        IS NULL

                        OR {priority} <= COALESCE(
                            (
                                SELECT ds.priority
                                FROM data_sets ds
                                WHERE ds.id =
                                    companies.master_dataset_id
                            ),
                            999
                        )

                    THEN COALESCE(
                        EXCLUDED.full_name,
                        companies.full_name
                    )

                    ELSE COALESCE(
                        companies.full_name,
                        EXCLUDED.full_name
                    )
                END,

            ogrn =
                CASE
                    WHEN
                        companies.master_dataset_id
                        IS NULL

                        OR {priority} <= COALESCE(
                            (
                                SELECT ds.priority
                                FROM data_sets ds
                                WHERE ds.id =
                                    companies.master_dataset_id
                            ),
                            999
                        )

                    THEN COALESCE(
                        EXCLUDED.ogrn,
                        companies.ogrn
                    )

                    ELSE COALESCE(
                        companies.ogrn,
                        EXCLUDED.ogrn
                    )
                END,

            region_code =
                CASE
                    WHEN
                        companies.master_dataset_id
                        IS NULL

                        OR {priority} <= COALESCE(
                            (
                                SELECT ds.priority
                                FROM data_sets ds
                                WHERE ds.id =
                                    companies.master_dataset_id
                            ),
                            999
                        )

                    THEN COALESCE(
                        EXCLUDED.region_code,
                        companies.region_code
                    )

                    ELSE COALESCE(
                        companies.region_code,
                        EXCLUDED.region_code
                    )
                END,

            okved =
                CASE
                    WHEN
                        companies.master_dataset_id
                        IS NULL

                        OR {priority} <= COALESCE(
                            (
                                SELECT ds.priority
                                FROM data_sets ds
                                WHERE ds.id =
                                    companies.master_dataset_id
                            ),
                            999
                        )

                    THEN COALESCE(
                        EXCLUDED.okved,
                        companies.okved
                    )

                    ELSE COALESCE(
                        companies.okved,
                        EXCLUDED.okved
                    )
                END,

            activity =
                CASE
                    WHEN
                        companies.master_dataset_id
                        IS NULL

                        OR {priority} <= COALESCE(
                            (
                                SELECT ds.priority
                                FROM data_sets ds
                                WHERE ds.id =
                                    companies.master_dataset_id
                            ),
                            999
                        )

                    THEN COALESCE(
                        EXCLUDED.activity,
                        companies.activity
                    )

                    ELSE COALESCE(
                        companies.activity,
                        EXCLUDED.activity
                    )
                END,

            master_dataset_id =
                CASE
                    WHEN
                        companies.master_dataset_id
                        IS NULL

                        OR {priority} <= COALESCE(
                            (
                                SELECT ds.priority
                                FROM data_sets ds
                                WHERE ds.id =
                                    companies.master_dataset_id
                            ),
                            999
                        )

                    THEN EXCLUDED.master_dataset_id

                    ELSE companies.master_dataset_id
                END,

            master_data_date =
                CASE
                    WHEN
                        companies.master_dataset_id
                        IS NULL

                        OR {priority} <= COALESCE(
                            (
                                SELECT ds.priority
                                FROM data_sets ds
                                WHERE ds.id =
                                    companies.master_dataset_id
                            ),
                            999
                        )

                    THEN EXCLUDED.master_data_date

                    ELSE companies.master_data_date
                END,

            source =
                COALESCE(
                    companies.source,
                    EXCLUDED.source
                ),

            source_updated_at =
                COALESCE(
                    companies.source_updated_at,
                    EXCLUDED.source_updated_at
                ),

            updated_at = NOW()
        """

        cursor.execute(
            master_sql,
            (
                dataset_id,
            ),
        )

        # -------------------------------------------------
        # MSP PROFILE UPSERT
        # -------------------------------------------------

        cursor.execute(
            """
            INSERT INTO company_msp_profiles
            (
                company_id,
                dataset_id,
                data_date,
                inclusion_date,
                subject_type_code,
                category_code,
                is_new_code,
                social_enterprise_code,
                employee_count,
                source_document_id
            )

            SELECT
                c.id,
                %s,
                t.data_date,
                t.inclusion_date,
                t.subject_type_code,
                t.category_code,
                t.is_new_code,
                t.social_enterprise_code,
                t.employee_count,
                t.document_id

            FROM tmp_fns_msp t

            JOIN companies c
              ON c.inn = t.inn

            ON CONFLICT
            (
                company_id,
                dataset_id
            )

            DO UPDATE SET

                data_date =
                    EXCLUDED.data_date,

                inclusion_date =
                    EXCLUDED.inclusion_date,

                subject_type_code =
                    EXCLUDED.subject_type_code,

                category_code =
                    EXCLUDED.category_code,

                is_new_code =
                    EXCLUDED.is_new_code,

                social_enterprise_code =
                    EXCLUDED.social_enterprise_code,

                employee_count =
                    EXCLUDED.employee_count,

                source_document_id =
                    EXCLUDED.source_document_id,

                updated_at = NOW()
            """,
            (
                dataset_id,
            ),
        )

        raw_connection.commit()

        return {
            "inserted": (
                inserted_count
            ),
            "updated": (
                updated_count
            ),
            "profiles": (
                batch_count
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
            parse_msp_document(
                element
            )
        )

        yield record

        element.clear()
        root.clear()


# =========================================================
# IMPORT ZIP
# =========================================================


def import_fns_msp_zip(
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

    dataset = (
        get_dataset_info()
    )

    dataset_id = dataset[
        "id"
    ]

    dataset_priority = dataset[
        "priority"
    ]

    totals = {
        "rows_read": 0,
        "valid": 0,
        "invalid": 0,
        "inserted": 0,
        "updated": 0,
        "profiles": 0,
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
                                    dataset_priority=(
                                        dataset_priority
                                    ),
                                )
                            )

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
                                "profiles"
                            ] += result[
                                "profiles"
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
                                "| добавлено:",
                                totals[
                                    "inserted"
                                ],
                                "| обновлено:",
                                totals[
                                    "updated"
                                ],
                                "| invalid:",
                                totals[
                                    "invalid"
                                ],
                            )

                if (
                    file_number
                    % 500
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
                        dataset_priority=(
                            dataset_priority
                        ),
                    )
                )

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
                    "profiles"
                ] += result[
                    "profiles"
                ]

                batch.clear()

        cursor.close()

    finally:

        raw_connection.close()

    return totals


# =========================================================
# WORKER FOUNDATION / OFFICIAL RELEASE SUPERVISION
# =========================================================


SOURCE_ID = DATASET_CODE
SOURCE_PAGE_URL = "https://www.nalog.gov.ru/opendata/7707329152-rsmp/"
HANDLER_VERSION = "msp-official-v1"


def _worker_spec():
    from app.ingestion.fns_bulk_worker import FnsBulkSourceSpec

    return FnsBulkSourceSpec(
        source_id=SOURCE_ID,
        dataset_code=DATASET_CODE,
        source_page_url=SOURCE_PAGE_URL,
        source_path="7707329152-rsmp",
        handler_version=HANDLER_VERSION,
        kind="msp",
        api_projection="msp_profile",
        card_projection="company_card.msp",
    )


def fns_msp_worker_handler(context):
    from app.ingestion.fns_bulk_worker import run_bulk_handler

    return run_bulk_handler(context, spec=_worker_spec(), iterator=iter_xml_records)


def publish_fns_msp_worker_result(session, claim, result):
    from app.ingestion.fns_bulk_worker import publish_bulk_result

    return publish_bulk_result(session, claim, result, spec=_worker_spec())


def register_fns_msp_worker(session, registry):
    from app.ingestion.fns_bulk_worker import register_bulk_handler

    return register_bulk_handler(
        session,
        registry,
        spec=_worker_spec(),
        handler=fns_msp_worker_handler,
        publisher=publish_fns_msp_worker_result,
    )


def schedule_fns_msp_check(session, *, raw_root, now=None):
    from app.ingestion.fns_bulk_worker import schedule_source_check

    return schedule_source_check(
        session, spec=_worker_spec(), raw_root=Path(raw_root), now=now
    )
