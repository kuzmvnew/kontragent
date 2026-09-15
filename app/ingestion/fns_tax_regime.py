from datetime import datetime


LEGAL_DATASET_CODE = "fns_snr"
IP_DATASET_CODE = "fns_snrip"


REGIME_NAMES = {
    "usn": "Упрощенная система налогообложения (УСН)",
    "ausn": (
        "Автоматизированная упрощенная "
        "система налогообложения (АУСН)"
    ),
    "eshn": (
        "Единый сельскохозяйственный налог (ЕСХН)"
    ),
    "srp": (
        "Система налогообложения при выполнении "
        "соглашения о разделе продукции (СРП)"
    ),
    "psn": "Патентная система налогообложения (ПСН)",
    "npd": "Налог на профессиональный доход (НПД)",
}


LEGAL_FLAG_TO_REGIME = {
    "ПризнУСН": "usn",
    "ПризнАУСН": "ausn",
    "ПризнЕСХН": "eshn",
    "ПризнСРП": "srp",
}


IP_CODE_TO_REGIME = {
    "1": "usn",
    "2": "ausn",
    "3": "eshn",
    "4": "psn",
    "5": "npd",
}


def local_name(tag):
    if "}" in tag:
        return tag.rsplit(
            "}",
            1,
        )[-1]

    return tag


def parse_fns_date(value):
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


def deduplicate(values):
    result = []

    for value in values:
        if value not in result:
            result.append(value)

    return result


def parse_legal_document(document):
    """
    Специальные налоговые режимы ЮЛ.

    ФНС публикует отдельные флаги 0/1:
    УСН, АУСН, ЕСХН и СРП.
    """

    taxpayer = None
    regime_element = None

    for element in document.iter():

        tag = local_name(
            element.tag
        )

        if (
            tag == "СведНП"
            and taxpayer is None
        ):
            taxpayer = element

        elif (
            tag == "СведСНР"
            and regime_element is None
        ):
            regime_element = element

    if (
        taxpayer is None
        or regime_element is None
    ):
        return None

    inn = taxpayer.attrib.get(
        "ИННЮЛ"
    )

    if not inn:
        return None

    inn = str(inn).strip()

    if (
        len(inn) != 10
        or not inn.isdigit()
    ):
        return None

    data_date = parse_fns_date(
        document.attrib.get(
            "ДатаСост"
        )
    )

    if data_date is None:
        return None

    regime_codes = []
    unknown_codes = []

    for (
        attribute_name,
        regime_code,
    ) in LEGAL_FLAG_TO_REGIME.items():

        value = regime_element.attrib.get(
            attribute_name
        )

        if value == "1":
            regime_codes.append(
                regime_code
            )

        elif value not in {
            None,
            "0",
        }:
            unknown_codes.append(
                f"{attribute_name}={value}"
            )

    return {
        "inn": inn,
        "entity_type": "legal",
        "dataset_code": LEGAL_DATASET_CODE,
        "data_date": data_date,
        "source_document_date": (
            parse_fns_date(
                document.attrib.get(
                    "ДатаДок"
                )
            )
        ),
        "source_document_id": (
            document.attrib.get(
                "ИдДок"
            )
        ),
        "regime_codes": (
            deduplicate(
                regime_codes
            )
        ),
        "unknown_codes": (
            deduplicate(
                unknown_codes
            )
        ),
    }


def parse_ip_document(document):
    """
    Специальные налоговые режимы ИП.

    Один документ может содержать
    несколько элементов СведСНР.

    Коды ФНС:
    1 — УСН
    2 — АУСН
    3 — ЕСХН
    4 — ПСН
    5 — НПД
    """

    taxpayer = None
    regime_elements = []

    for element in document.iter():

        tag = local_name(
            element.tag
        )

        if (
            tag == "СведНП"
            and taxpayer is None
        ):
            taxpayer = element

        elif tag == "СведСНР":
            regime_elements.append(
                element
            )

    if taxpayer is None:
        return None

    inn = taxpayer.attrib.get(
        "ИННФЛ"
    )

    if not inn:
        return None

    inn = str(inn).strip()

    if (
        len(inn) != 12
        or not inn.isdigit()
    ):
        return None

    data_date = parse_fns_date(
        document.attrib.get(
            "ДатаСост"
        )
    )

    if data_date is None:
        return None

    regime_codes = []
    unknown_codes = []

    for element in regime_elements:

        source_code = element.attrib.get(
            "ПризнСНР"
        )

        if source_code is None:
            continue

        source_code = str(
            source_code
        ).strip()

        regime_code = (
            IP_CODE_TO_REGIME.get(
                source_code
            )
        )

        if regime_code is None:
            unknown_codes.append(
                source_code
            )
        else:
            regime_codes.append(
                regime_code
            )

    return {
        "inn": inn,
        "ogrn": (
            taxpayer.attrib.get(
                "ОГРНИП"
            )
        ),
        "entity_type": (
            "individual_entrepreneur"
        ),
        "dataset_code": IP_DATASET_CODE,
        "data_date": data_date,
        "source_document_date": (
            parse_fns_date(
                document.attrib.get(
                    "ДатаДок"
                )
            )
        ),
        "source_document_id": (
            document.attrib.get(
                "ИдДок"
            )
        ),
        "regime_codes": (
            deduplicate(
                regime_codes
            )
        ),
        "unknown_codes": (
            deduplicate(
                unknown_codes
            )
        ),
    }


def get_regime_name(regime_code):
    if regime_code is None:
        return None

    return REGIME_NAMES.get(
        str(regime_code).strip()
    )


# =========================================================
# DATABASE INGESTION — LEGAL ENTITIES
# =========================================================


def get_dataset_id(
    dataset_code,
):
    from sqlalchemy import select

    from app.database.postgres import get_session
    from app.models.source import DataSet

    session = get_session()

    try:
        dataset_id = (
            session.execute(
                select(
                    DataSet.id
                ).where(
                    DataSet.code
                    == dataset_code
                )
            )
            .scalar_one_or_none()
        )

        if dataset_id is None:
            raise RuntimeError(
                f"Dataset {dataset_code} "
                "не найден. "
                "Сначала запусти "
                "scripts.init_sources."
            )

        return dataset_id

    finally:
        session.close()


def process_legal_batch(
    records,
    dataset_id,
):
    """
    Сопоставляет записи SNR ЮЛ
    с master registry по ИНН
    и сохраняет snapshots.
    """

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from app.database.postgres import get_session
    from app.models.company import Company
    from app.models.tax_regime import (
        CompanyTaxRegimeSnapshot,
    )

    if not records:
        return {
            "matched": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
        }

    records_by_inn = {
        record["inn"]: record
        for record in records
    }

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
                ).where(
                    Company.inn.in_(
                        inns
                    )
                )
            )
            .all()
        )

        company_by_inn = {
            inn: company_id
            for company_id, inn
            in company_rows
        }

        payloads = []

        skipped = 0

        for inn, record in (
            records_by_inn.items()
        ):

            company_id = (
                company_by_inn.get(
                    inn
                )
            )

            if company_id is None:
                skipped += 1
                continue

            payloads.append(
                {
                    "company_id": (
                        company_id
                    ),
                    "dataset_id": (
                        dataset_id
                    ),
                    "entity_type": (
                        record[
                            "entity_type"
                        ]
                    ),
                    "data_date": (
                        record[
                            "data_date"
                        ]
                    ),
                    "regime_codes": (
                        record[
                            "regime_codes"
                        ]
                    ),
                    "source_document_id": (
                        record[
                            "source_document_id"
                        ]
                    ),
                    "source_document_date": (
                        record[
                            "source_document_date"
                        ]
                    ),
                }
            )

        if not payloads:
            return {
                "matched": 0,
                "inserted": 0,
                "updated": 0,
                "skipped": skipped,
            }

        company_ids = [
            item[
                "company_id"
            ]
            for item in payloads
        ]

        data_dates = list(
            {
                item[
                    "data_date"
                ]
                for item in payloads
            }
        )

        existing_rows = (
            session.execute(
                select(
                    CompanyTaxRegimeSnapshot
                    .company_id,
                    CompanyTaxRegimeSnapshot
                    .data_date,
                ).where(
                    CompanyTaxRegimeSnapshot
                    .dataset_id
                    == dataset_id,
                    CompanyTaxRegimeSnapshot
                    .company_id
                    .in_(
                        company_ids
                    ),
                    CompanyTaxRegimeSnapshot
                    .data_date
                    .in_(
                        data_dates
                    ),
                )
            )
            .all()
        )

        existing_keys = {
            (
                company_id,
                data_date,
            )
            for (
                company_id,
                data_date,
            )
            in existing_rows
        }

        inserted = 0
        updated = 0

        for item in payloads:

            key = (
                item[
                    "company_id"
                ],
                item[
                    "data_date"
                ],
            )

            if key in existing_keys:
                updated += 1
            else:
                inserted += 1

        statement = insert(
            CompanyTaxRegimeSnapshot
        ).values(
            payloads
        )

        statement = (
            statement
            .on_conflict_do_update(
                index_elements=[
                    "company_id",
                    "dataset_id",
                    "data_date",
                ],
                set_={
                    "entity_type": (
                        statement
                        .excluded
                        .entity_type
                    ),
                    "regime_codes": (
                        statement
                        .excluded
                        .regime_codes
                    ),
                    "source_document_id": (
                        statement
                        .excluded
                        .source_document_id
                    ),
                    "source_document_date": (
                        statement
                        .excluded
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
            "matched": len(
                payloads
            ),
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
        }

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()



# =========================================================
# ZIP STREAMING — LEGAL ENTITIES
# =========================================================


def iter_legal_records_from_zip(
    zip_path,
    limit=None,
):
    """
    Потоково читает XML из ZIP ФНС SNR.

    Не загружает весь ZIP и все XML
    в память одновременно.
    """

    from zipfile import ZipFile
    from xml.etree import ElementTree as ET

    yielded = 0

    with ZipFile(zip_path) as archive:

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

        for info in xml_files:

            with archive.open(
                info
            ) as stream:

                for (
                    _event,
                    element,
                ) in ET.iterparse(
                    stream,
                    events=("end",),
                ):

                    if (
                        local_name(
                            element.tag
                        )
                        != "Документ"
                    ):
                        continue

                    record = (
                        parse_legal_document(
                            element
                        )
                    )

                    if record is not None:
                        yield record
                        yielded += 1

                    element.clear()

                    if (
                        limit is not None
                        and yielded >= limit
                    ):
                        return
