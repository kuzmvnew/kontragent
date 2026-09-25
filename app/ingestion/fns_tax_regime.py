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



# =========================================================
# ZIP STREAMING — INDIVIDUAL ENTREPRENEURS
# =========================================================


def iter_ip_records_from_zip(
    zip_path,
    limit=None,
):
    """
    Потоково читает XML из ZIP ФНС SNRIP.
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

            with archive.open(info) as stream:

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
                        parse_ip_document(
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


def process_ip_batch(
    records,
    dataset_id,
):
    """
    Сохраняет SNRIP snapshots.

    Использует общий batch-механизм:
    matching по ИНН + PostgreSQL upsert.
    """

    return process_legal_batch(
        records,
        dataset_id,
    )


# =========================================================
# WORKER FOUNDATION / OFFICIAL TWO-ARTIFACT FAMILY
# =========================================================


SOURCE_ID = "fns_tax_regime"
FAMILY_DATASET_CODE = SOURCE_ID
HANDLER_VERSION = "tax-regime-family-official-v1"
LEGAL_SOURCE_PAGE_URL = "https://www.nalog.gov.ru/opendata/7707329152-snr/"
IP_SOURCE_PAGE_URL = "https://www.nalog.gov.ru/opendata/7707329152-snrip/"


def _family_spec():
    from app.ingestion.fns_bulk_worker import FnsBulkSourceSpec

    return FnsBulkSourceSpec(
        source_id=SOURCE_ID,
        dataset_code=FAMILY_DATASET_CODE,
        source_page_url=LEGAL_SOURCE_PAGE_URL,
        source_path="7707329152-snr",
        handler_version=HANDLER_VERSION,
        kind="tax_regime",
        api_projection="tax_regime_profile",
        card_projection="company_card.tax_regime",
    )


def _member_specs():
    from app.ingestion.fns_bulk_worker import FnsBulkSourceSpec

    common = {
        "source_id": SOURCE_ID,
        "handler_version": HANDLER_VERSION,
        "kind": "tax_regime",
        "api_projection": "tax_regime_profile",
        "card_projection": "company_card.tax_regime",
    }
    return {
        "legal": FnsBulkSourceSpec(
            dataset_code=LEGAL_DATASET_CODE,
            source_page_url=LEGAL_SOURCE_PAGE_URL,
            source_path="7707329152-snr",
            **common,
        ),
        "ip": FnsBulkSourceSpec(
            dataset_code=IP_DATASET_CODE,
            source_page_url=IP_SOURCE_PAGE_URL,
            source_path="7707329152-snrip",
            **common,
        ),
    }


def iter_legal_xml_records(xml_file):
    from xml.etree import ElementTree as ET

    for _event, element in ET.iterparse(xml_file, events=("end",)):
        if local_name(element.tag) != "Документ":
            continue
        yield parse_legal_document(element)
        element.clear()


def iter_ip_xml_records(xml_file):
    from xml.etree import ElementTree as ET

    for _event, element in ET.iterparse(xml_file, events=("end",)):
        if local_name(element.tag) != "Документ":
            continue
        yield parse_ip_document(element)
        element.clear()


def _bundle_from_metadata(metadata):
    from app.ingestion.fns_bulk_worker import FnsReleaseBundle, release_from_metadata
    from app.worker.errors import InvalidDataError

    raw = metadata.get("releases")
    if not isinstance(raw, dict) or set(raw) != {"legal", "ip"}:
        raise InvalidDataError("tax-regime release requires legal and IP members")
    bundle = FnsReleaseBundle(
        releases={name: release_from_metadata(item) for name, item in raw.items()}
    )
    if bundle.identity != metadata.get("release_identity"):
        raise InvalidDataError("tax-regime bundle identity differs from job metadata")
    return bundle


def coalesce_tax_regime_records(staging_path):
    """Deterministically union duplicate-INN SNR/SNRIP rows on disk.

    The official SNRIP release legitimately contains duplicate INNs.  Keeping
    only the first or last document would lose regimes.  A temporary SQLite
    spool bounds Python memory, sorts across XML members, validates that every
    duplicate describes the same snapshot/entity, and retains every source
    document in the accepted normalized JSONL.
    """

    import json
    import os
    from pathlib import Path
    import sqlite3
    import tempfile

    from app.worker.errors import SchemaMismatchError

    source = Path(staging_path)
    db_handle, db_name = tempfile.mkstemp(
        prefix="tax-regime-coalesce-", suffix=".sqlite", dir=source.parent
    )
    os.close(db_handle)
    output_handle, output_name = tempfile.mkstemp(
        prefix="tax-regime-coalesced-", suffix=".jsonl.tmp", dir=source.parent
    )
    os.close(output_handle)
    database = Path(db_name)
    output = Path(output_name)
    connection = sqlite3.connect(database)
    total = unique = 0
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(
            "CREATE TABLE records (inn TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        batch = []
        with source.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                inn = str(row.get("inn") or "")
                if not inn:
                    raise SchemaMismatchError("tax-regime normalized row has no INN")
                batch.append((inn, json.dumps(row, ensure_ascii=False, sort_keys=True)))
                total += 1
                if len(batch) >= 10000:
                    connection.executemany(
                        "INSERT INTO records (inn, payload) VALUES (?, ?)", batch
                    )
                    batch.clear()
            if batch:
                connection.executemany(
                    "INSERT INTO records (inn, payload) VALUES (?, ?)", batch
                )
        connection.execute("CREATE INDEX records_inn ON records (inn)")

        def merged(rows):
            first = dict(rows[0])
            identity = (first.get("entity_type"), first.get("dataset_code"))
            data_date = first.get("data_date")
            regime_codes = set()
            unknown_codes = set()
            documents = set()
            ogrns = set()
            for row in rows:
                if (row.get("entity_type"), row.get("dataset_code")) != identity:
                    raise SchemaMismatchError(
                        f"tax-regime duplicate INN changes applicability: {first['inn']}"
                    )
                if row.get("data_date") != data_date:
                    raise SchemaMismatchError(
                        f"tax-regime duplicate INN has mixed source dates: {first['inn']}"
                    )
                regime_codes.update(str(code) for code in row.get("regime_codes") or ())
                unknown_codes.update(str(code) for code in row.get("unknown_codes") or ())
                document = (
                    str(row.get("source_document_id") or ""),
                    str(row.get("source_document_date") or ""),
                )
                if any(document):
                    documents.add(document)
                if row.get("ogrn"):
                    ogrns.add(str(row["ogrn"]))
            if len(ogrns) > 1:
                raise SchemaMismatchError(
                    f"tax-regime duplicate INN has conflicting OGRN: {first['inn']}"
                )
            ordered_documents = sorted(documents)
            first["regime_codes"] = sorted(regime_codes)
            first["unknown_codes"] = sorted(unknown_codes)
            first["source_documents"] = [
                {"source_document_id": document_id or None,
                 "source_document_date": document_date or None}
                for document_id, document_date in ordered_documents
            ]
            if ordered_documents:
                first["source_document_id"] = ordered_documents[0][0] or None
                first["source_document_date"] = ordered_documents[0][1] or None
            if ogrns:
                first["ogrn"] = next(iter(ogrns))
            return first

        with output.open("w", encoding="utf-8") as target:
            current_inn = None
            group = []
            for inn, payload in connection.execute(
                "SELECT inn, payload FROM records ORDER BY inn"
            ):
                if current_inn is not None and inn != current_inn:
                    target.write(
                        json.dumps(merged(group), ensure_ascii=False, sort_keys=True)
                        + "\n"
                    )
                    unique += 1
                    group.clear()
                current_inn = inn
                group.append(json.loads(payload))
            if group:
                target.write(
                    json.dumps(merged(group), ensure_ascii=False, sort_keys=True) + "\n"
                )
                unique += 1
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        connection.close()
        database.unlink(missing_ok=True)
    return output, {
        "normalized_unique_inns": unique,
        "duplicate_inn_rows": total - unique,
    }


def fns_tax_regime_worker_handler(context):
    import json
    from pathlib import Path

    from app.ingestion.fns_bulk_worker import (
        _hash_file,
        _write_once,
        normalize_release,
        stage_release,
        utc_now,
    )
    from app.worker.contracts import (
        ExecutionCounters,
        HandlerResult,
        RawArtifactReference,
        StagingResult,
        ValidationResult,
    )
    from app.worker.errors import InvalidDataError, SchemaMismatchError

    metadata = context.schedule_metadata
    bundle = _bundle_from_metadata(metadata)
    if metadata.get("check_only"):
        return HandlerResult(
            checksum_metadata={
                "release_identity": bundle.identity,
                "check_only": True,
                "replay_snapshot": bool(metadata.get("replay_snapshot")),
            },
            counters=ExecutionCounters(),
        )
    raw_value = str(metadata.get("raw_root") or "").strip()
    if not raw_value:
        raise InvalidDataError("raw_root is required")
    raw_root = Path(raw_value).resolve()
    context.ensure_active(now=utc_now())

    iterators = {"legal": iter_legal_xml_records, "ip": iter_ip_xml_records}
    members = {}
    raw_artifacts = []
    total_seen = total_written = total_rejected = 0
    for name, spec in _member_specs().items():
        release = bundle.releases[name]
        zip_path, xsd_path, manifest = stage_release(
            spec, release, raw_root=raw_root
        )
        context.heartbeat()
        normalized_path, normalized_checksum, counters = normalize_release(
            zip_path,
            xsd_path=xsd_path,
            iterator=iterators[name],
            postprocess=coalesce_tax_regime_records,
        )
        if counters["source_data_date"] != release.source_data_date.isoformat():
            raise SchemaMismatchError(
                f"parsed {name} source data date differs from official passport"
            )
        members[name] = {
            "dataset_code": spec.dataset_code,
            "staging_pointer": normalized_path.as_uri(),
            "normalized_sha256": normalized_checksum,
            "release": release.as_metadata(),
            "coverage": counters,
        }
        raw_artifacts.append(
            RawArtifactReference(
                artifact_reference=zip_path.as_uri(),
                checksum=manifest["artifact_sha256"],
                manifest=manifest,
            )
        )
        total_seen += int(counters["records_seen"])
        total_written += int(counters["records_valid"])
        total_rejected += int(counters["records_rejected"])

    descriptor = {
        "manifest_version": 1,
        "source_id": SOURCE_ID,
        "release_identity": bundle.identity,
        "members": members,
        "immutable": True,
    }
    payload = (json.dumps(descriptor, ensure_ascii=False, sort_keys=True) + "\n").encode()
    bundle_dir = raw_root / SOURCE_ID / "bundles" / bundle.identity
    bundle_dir.mkdir(parents=True, exist_ok=True)
    descriptor_path = bundle_dir / "normalized-bundle.json"
    _write_once(descriptor_path, payload)
    descriptor_checksum, _size = _hash_file(descriptor_path)
    execution = ExecutionCounters(
        records_seen=total_seen,
        records_written=total_written,
        records_rejected=total_rejected,
    )
    context.report_counters(execution)
    return HandlerResult(
        raw_artifacts=tuple(raw_artifacts),
        staging_result=StagingResult(
            staging_pointer=descriptor_path.as_uri(),
            checksum=descriptor_checksum,
            validation=ValidationResult(
                accepted=True,
                metadata={
                    "release_identity": bundle.identity,
                    "source_data_date": bundle.source_data_date.isoformat(),
                    "member_release_identities": {
                        name: release.identity for name, release in bundle.releases.items()
                    },
                },
            ),
            metadata={
                "source_id": SOURCE_ID,
                "dataset_code": FAMILY_DATASET_CODE,
                "member_dataset_codes": [LEGAL_DATASET_CODE, IP_DATASET_CODE],
            },
        ),
        checksum_metadata={
            "normalized_bundle_sha256": descriptor_checksum,
            "release_identity": bundle.identity,
            "member_checksums": {
                name: item["normalized_sha256"] for name, item in members.items()
            },
        },
        counters=execution,
    )


def publish_fns_tax_regime_worker_result(session, claim, result):
    import json
    from dataclasses import replace
    from datetime import datetime, timezone

    from sqlalchemy import func, select

    from app.contracts.data_readiness import AutoUpdateStatus
    from app.ingestion.fns_bulk_worker import (
        _accepted_replay_path,
        _apply_successful_check,
        _fact_model,
        _file_path,
        _hash_file,
        _project_normalized_snapshot,
        utc_now,
    )
    from app.models.source import DataSet
    from app.worker.contracts import ExecutionCounters
    from app.worker.errors import InvalidDataError

    bundle = _bundle_from_metadata(claim.schedule_metadata)
    family_spec = _family_spec()
    family = session.scalar(
        select(DataSet).where(DataSet.code == FAMILY_DATASET_CODE).with_for_update()
    )
    if family is None:
        raise InvalidDataError(f"dataset is not registered: {FAMILY_DATASET_CODE}")
    children = {
        dataset.code: dataset
        for dataset in session.scalars(
            select(DataSet)
            .where(DataSet.code.in_((LEGAL_DATASET_CODE, IP_DATASET_CODE)))
            .with_for_update()
        )
    }
    if set(children) != {LEGAL_DATASET_CODE, IP_DATASET_CODE}:
        raise InvalidDataError("tax-regime child datasets are not registered")

    check_only = bool(claim.schedule_metadata.get("check_only"))
    if check_only:
        descriptor_path = _accepted_replay_path(
            session, claim=claim, spec=family_spec
        )
    else:
        if result.staging_result is None:
            raise InvalidDataError("tax-regime publisher requires normalized bundle")
        descriptor_path = _file_path(result.staging_result.staging_pointer)
    try:
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InvalidDataError("tax-regime normalized bundle cannot be read") from error
    if descriptor.get("release_identity") != bundle.identity:
        raise InvalidDataError("tax-regime normalized bundle release changed")
    members = descriptor.get("members")
    if not isinstance(members, dict) or set(members) != {"legal", "ip"}:
        raise InvalidDataError("tax-regime normalized bundle members differ")

    now = utc_now()
    totals = {"matched": 0, "unmatched": 0, "changed": 0, "published": 0}
    matched_companies = set()
    member_coverage = {}
    for name, spec in _member_specs().items():
        member = members[name]
        release = bundle.releases[name]
        if (
            member.get("dataset_code") != spec.dataset_code
            or (member.get("release") or {}).get("release_identity") != release.identity
        ):
            raise InvalidDataError(f"tax-regime {name} descriptor identity differs")
        staging_path = _file_path(str(member.get("staging_pointer") or ""))
        checksum, _size = _hash_file(staging_path)
        if checksum != member.get("normalized_sha256"):
            raise InvalidDataError(f"tax-regime {name} normalized checksum mismatch")
        dataset = children[spec.dataset_code]
        matched, unmatched, companies, changed = _project_normalized_snapshot(
            session,
            dataset=dataset,
            spec=spec,
            staging_path=staging_path,
            replace_existing=not check_only,
        )
        published = int(
            session.scalar(
                select(func.count())
                .select_from(_fact_model(spec))
                .where(_fact_model(spec).dataset_id == dataset.id)
            )
            or 0
        )
        totals["matched"] += matched
        totals["unmatched"] += unmatched
        totals["changed"] += changed
        totals["published"] += published
        matched_companies.update(companies)
        if not check_only:
            dataset.enabled = True
            dataset.last_success_at = now
            dataset.last_data_date = release.source_data_date
            dataset.source_as_of = datetime.combine(
                release.source_data_date, datetime.min.time(), tzinfo=timezone.utc
            )
            dataset.retrieved_at = now
            dataset.published_at = now
        dataset.record_count = published
        child_status = _apply_successful_check(
            dataset,
            actual_until=release.actual_until,
            now=now,
            check_interval=spec.check_interval,
        )
        # Child datasets are projections owned by the one family schedule.
        dataset.auto_update_status = AutoUpdateStatus.NOT_CONFIGURED
        dataset.next_expected_update_at = None
        dataset.coverage = {
            "managed_by_source_id": SOURCE_ID,
            "source_records": int((member.get("coverage") or {}).get("records_seen") or 0),
            "matched": matched,
            "unmatched": unmatched,
            "published_facts": published,
            "freshness": child_status.value,
            "release_identity": release.identity,
        }
        member_coverage[name] = dict(dataset.coverage)

    if not check_only:
        family.enabled = True
        family.auto_update_status = AutoUpdateStatus.CONFIGURED
        family.last_success_at = now
        family.last_data_date = bundle.source_data_date
        family.source_as_of = datetime.combine(
            bundle.source_data_date, datetime.min.time(), tzinfo=timezone.utc
        )
        family.retrieved_at = now
        family.published_at = now
    family.record_count = totals["published"]
    status = _apply_successful_check(
        family,
        actual_until=bundle.actual_until,
        now=now,
        check_interval=family_spec.check_interval,
    )
    family.coverage = {
        "source_records": totals["matched"] + totals["unmatched"],
        "matched": totals["matched"],
        "unmatched": totals["unmatched"],
        "published_facts": totals["published"],
        "risk_summary_candidate_companies": len(matched_companies),
        "api_projection": family_spec.api_projection,
        "card_projection": family_spec.card_projection,
        "release_identity": bundle.identity,
        "members": member_coverage,
    }
    counters = ExecutionCounters(
        records_seen=totals["matched"] + totals["unmatched"],
        records_written=(totals["changed"] if check_only else totals["matched"]),
        records_published=(totals["changed"] if check_only else totals["published"]),
    )
    if check_only:
        return replace(
            result,
            staging_result=None,
            checksum_metadata={
                **result.checksum_metadata,
                "freshness": status.value,
                "official_actual_until": (
                    bundle.actual_until.isoformat() if bundle.actual_until else None
                ),
            },
            counters=counters,
        )
    validation = replace(
        result.staging_result.validation,
        metadata={
            **result.staging_result.validation.metadata,
            **totals,
            "risk_summary_candidate_companies": len(matched_companies),
            "freshness": status.value,
            "official_actual_until": (
                bundle.actual_until.isoformat() if bundle.actual_until else None
            ),
        },
    )
    return replace(
        result,
        staging_result=replace(result.staging_result, validation=validation),
        counters=counters,
    )


def register_fns_tax_regime_worker(session, registry):
    from app.ingestion.fns_bulk_worker import register_bulk_handler

    return register_bulk_handler(
        session,
        registry,
        spec=_family_spec(),
        handler=fns_tax_regime_worker_handler,
        publisher=publish_fns_tax_regime_worker_result,
    )


def schedule_fns_tax_regime_check(session, *, raw_root, now=None):
    from pathlib import Path

    from app.ingestion.fns_bulk_worker import schedule_source_bundle_check

    return schedule_source_bundle_check(
        session,
        spec=_family_spec(),
        member_specs=_member_specs(),
        raw_root=Path(raw_root),
        now=now,
    )
