from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.ingestion.fns_sme_support_xml import parse_support_records
from app.ingestion.fns_sme_support_integrity import audit_archive, verify_counts, sha256_file, POLICY
from app.models.fns_sme_support import FnsSmeSupportEntry
from app.models.company import Company
from app.models.source import DataSet, IngestionRun

DATASET_CODE = "fns_sme_support"
DEFAULT_BATCH_SIZE = 2000
MAX_BATCH_SIZE = 2000
UNIT_NAMES = {"1": "рубль", "2": "квадратный метр", "3": "час", "4": "процент", "5": "единица"}


def local_name(tag) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


def parse_date(value) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


def _text(value) -> str | None:
    return str(value).strip() or None if value is not None else None


def normalize_inn(value) -> str | None:
    text = _text(value)
    return text if text and text.isascii() and text.isdigit() and len(text) in {10, 12} else None


def normalize_ogrn(value) -> str | None:
    text = _text(value)
    return text if text and text.isascii() and text.isdigit() and len(text) in {13, 15} else None


def _direct_child(element, *names):
    return next((child for child in element if local_name(child.tag) in names), None)


def _value(element, *names):
    for name in names:
        if _text(element.get(name)):
            return _text(element.get(name))
    child = _direct_child(element, *names)
    if child is None:
        return None
    if _text(child.text):
        return _text(child.text)
    for name in names:
        if _text(child.get(name)):
            return _text(child.get(name))
    if len(child.attrib) == 1:
        return _text(next(iter(child.attrib.values())))
    return None


def _complex_values(document, tag_name):
    return [child for child in document if local_name(child.tag) == tag_name]


def _attribute_or_child(element, name):
    if _text(element.get(name)):
        return _text(element.get(name))
    child = _direct_child(element, name)
    return _text(child.text) if child is not None else None


def parse_support_document(document, *, provider_inn, data_date) -> dict:
    """Normalize one flat support fact; bulk documents use parse_support_records."""
    if local_name(document.tag) != "Документ":
        raise ValueError("Ожидался элемент Документ")
    document_id = _value(document, "ИдДок", "ИдПоддержки", "ИдПоддерж", "ИдПод", "НомерПоддержки", "НомПод")
    if not document_id:
        raise ValueError("У записи поддержки нет идентификатора документа")
    inn = normalize_inn(_value(document, "ИННЮЛ")) or normalize_inn(_value(document, "ИННФЛ"))
    if inn is None:
        raise ValueError("У записи поддержки нет валидного ИНН получателя")
    ogrn = normalize_ogrn(_value(document, "ОГРН", "ОГРНИП"))
    if len(inn) == 10:
        kind = "legal"
        if ogrn and len(ogrn) != 13:
            raise ValueError("ОГРН юридического лица должен содержать 13 цифр")
    else:
        if ogrn and len(ogrn) != 15:
            raise ValueError("ОГРНИП должен содержать 15 цифр")
        kind = "individual_entrepreneur_or_kfh" if ogrn else "npd_individual"
    information_date = parse_date(_value(document, "ДатаСвед"))
    support_until = parse_date(_value(document, "СрокПод"))
    decision_date = parse_date(_value(document, "ДатаОказ"))
    raw_termination = _value(document, "ДатаПрекр")
    termination_date = parse_date(raw_termination)
    if information_date is None or support_until is None or decision_date is None:
        raise ValueError("У записи поддержки отсутствуют обязательные даты")
    if raw_termination and termination_date is None:
        raise ValueError("Некорректная ДатаПрекр")
    violation_code = _value(document, "ИнфНаруш")
    if violation_code not in {None, "1", "2"}:
        raise ValueError("Некорректный код ИнфНаруш")
    form = _direct_child(document, "ФормПод")
    support_type = _direct_child(document, "ВидПод")
    amounts = []
    for item in _complex_values(document, "РазмПод"):
        amount = _attribute_or_child(item, "РазмПод")
        unit = _attribute_or_child(item, "ЕдПод")
        if amount is None or unit is None:
            raise ValueError("Неполный элемент РазмПод")
        amounts.append({"value": amount, "unit_code": unit, "unit_name": UNIT_NAMES.get(unit, f"код {unit}")})
    if not amounts:
        raise ValueError("У записи поддержки отсутствует размер поддержки")
    violations = [{
        "kind_code": _attribute_or_child(item, "ВидНаруш"),
        "violation_date": _attribute_or_child(item, "ДатаНаруш"),
        "remedy_deadline": _attribute_or_child(item, "СрокНаруш"),
        "remedied_date": _attribute_or_child(item, "ДатаУстрНаруш"),
    } for item in _complex_values(document, "Нарушения")]
    # Open-data XSD 4.04 declares Нарушения with minOccurs=0. Preserve the
    # independent ИнфНаруш flag even when no detail element was published.
    # Empty details are NOT evidence of no violation and must not drop the fact.
    regulatory_ids = []
    for item in _complex_values(document, "РегДок"):
        identifier = _attribute_or_child(item, "ИдРД")
        if identifier and identifier not in regulatory_ids:
            regulatory_ids.append(identifier)
    giver = normalize_inn(provider_inn)
    if giver and len(giver) != 10:
        giver = None
    return {
        "data_date": data_date, "source_record_key": f"{giver or '-'}:{document_id}:{inn}",
        "source_document_id": document_id, "provider_inn": giver,
        "recipient_inn": inn, "recipient_ogrn": ogrn, "recipient_kind": kind,
        "information_date": information_date, "support_until": support_until,
        "decision_date": decision_date, "termination_date": termination_date,
        "violation_code": violation_code,
        "support_form_code": _attribute_or_child(form, "КодФорм") if form is not None else None,
        "support_form_name": _attribute_or_child(form, "НаимФорм") if form is not None else None,
        "support_type_code": _attribute_or_child(support_type, "КодВид") if support_type is not None else None,
        "support_type_name": _attribute_or_child(support_type, "НаимВид") if support_type is not None else None,
        "amounts": amounts, "violations": violations, "regulatory_document_ids": regulatory_ids,
    }


def _provider_inn_from_sender(sender):
    value = normalize_inn(sender.get("ИННЮЛ") or _value(sender, "ИННЮЛ"))
    return value if value and len(value) == 10 else None


def stream_xml_file(stream, *, data_date: date, consume, verified_counts=None) -> dict:
    provider_inn = None
    expected_documents = None
    source_documents = source_records = eligible_records = excluded_npd_records = 0
    root = None
    try:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            tag = local_name(element.tag)
            if event == "start" and root is None:
                if tag != "Файл":
                    raise ValueError("Ожидался корневой элемент Файл")
                root = element
                raw = element.get("КолДок")
                if raw is None:
                    raise ValueError("В XML ФНС отсутствует обязательный КолДок")
                try:
                    expected_documents = int(raw)
                except ValueError as error:
                    raise ValueError("Некорректный КолДок в XML ФНС") from error
                if expected_documents < 1:
                    raise ValueError("КолДок должен быть больше нуля")
            if event != "end":
                continue
            if tag == "ИдОтпр":
                provider_inn = _provider_inn_from_sender(element)
                element.clear()
            elif tag == "Документ":
                source_documents += 1
                try:
                    for record in parse_support_records(element, data_date=data_date,
                            provider_inn=provider_inn, legacy_parser=parse_support_document):
                        source_records += 1
                        if record["recipient_kind"] == "npd_individual":
                            excluded_npd_records += 1
                        else:
                            consume(record)
                            eligible_records += 1
                except ValueError as error:
                    raise ValueError(f"Документ #{source_documents}, ИдДок={element.get('ИдДок')}: {error}") from error
                root.remove(element)
                element.clear()
    except ET.ParseError as error:
        raise ValueError(f"Некорректный XML ФНС: {error}") from error
    if expected_documents is None:
        raise ValueError("Не найден корневой элемент Файл с КолДок")
    result = {"expected_documents": expected_documents, "source_records": source_records,
              "eligible_records": eligible_records, "excluded_npd_records": excluded_npd_records}
    if source_documents != source_records:
        result["source_documents"] = source_documents
    if verified_counts is not None:
        if expected_documents != verified_counts["declared_documents"]:
            raise ValueError("Header changed after independent audit")
        verify_counts(result, verified_counts)
    elif source_documents != expected_documents:
        # Standalone streaming stays strict; only a complete archive audit can
        # supply the independently counted evidence for the known header defect.
        raise ValueError(f"КолДок={expected_documents}, но распознано Документ={source_documents}")
    return result


def get_dataset_id(dataset_code=DATASET_CODE):
    with get_session() as session:
        value = session.scalar(select(DataSet.id).where(DataSet.code == dataset_code))
        if value is None:
            raise RuntimeError(f"Dataset {dataset_code} не зарегистрирован")
        return value


def create_ingestion_run(*, dataset_id, data_date, source_url, source_file_name, checksum):
    with get_session() as session:
        run = IngestionRun(dataset_id=dataset_id, status="running", data_date=data_date,
            source_url=source_url, source_file_name=source_file_name, file_checksum=checksum,
            details={"complete_snapshot": False, "person_rows_persisted": False})
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id


def mark_run_failed(run_id, error, *, details=None):
    with get_session() as session:
        run = session.get(IngestionRun, run_id)
        if run is not None:
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.errors_count = 1
            run.error_message = str(error)[:4000]
            run.details = {**(run.details or {}), **(details or {}), "complete_snapshot": False}
            session.execute(delete(FnsSmeSupportEntry).where(FnsSmeSupportEntry.ingestion_run_id == run_id))
            session.commit()


def _insert_batch(*, run_id, dataset_id, records):
    if not records:
        return 0
    with get_session() as session:
        values = [{"ingestion_run_id": run_id, "dataset_id": dataset_id, **record} for record in records]
        # Unique constraint rejects identical and conflicting duplicates alike;
        # no silently skipped records and no incomplete publication.
        session.execute(insert(FnsSmeSupportEntry).values(values))
        session.commit()
        return len(records)


def import_fns_sme_support_archive(archive_path, *, dataset_id, run_id, data_date,
                                   batch_size=DEFAULT_BATCH_SIZE):
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size должен быть от 1 до {MAX_BATCH_SIZE}")
    audit = audit_archive(archive_path)
    integrity = audit['summary']
    with get_session() as session:
        run = session.get(IngestionRun, run_id)
        if run is None or run.file_checksum != integrity['archive_sha256']:
            raise ValueError("ZIP does not match recorded download SHA-256")
        run.details = {**(run.details or {}), 'integrity': integrity,
                       'metadata_data_date': str(data_date)}
        session.commit()
    # Date of the records is ДатаСост, NOT retrieval date or next relevance date
    # from the metadata page. Keep metadata date separately for provenance.
    snapshot_date = date.fromisoformat(integrity['data_date']) if integrity['data_date'] else data_date
    batch = []
    inserted = 0
    quality = {'scope': 'persisted_company_ip_facts', 'violation_flagged_records': 0,
               'violation_missing_details_records': 0}
    totals = dict.fromkeys(['source_records', 'source_documents', 'eligible_records',
                           'excluded_npd_records', 'expected_documents'], 0)
    def consume(record):
        nonlocal inserted
        if record['violation_code'] == '1':
            quality['violation_flagged_records'] += 1
            if not record['violations']:
                quality['violation_missing_details_records'] += 1
        batch.append(record)
        if len(batch) >= batch_size:
            inserted += _insert_batch(run_id=run_id, dataset_id=dataset_id, records=batch)
            batch.clear()
            if inserted % 100000 < batch_size:
                print(f"W1-003: сохранено {inserted:,} фактов (snapshot ещё не опубликован)", flush=True)
    try:
        with ZipFile(Path(archive_path)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir() and item.filename.lower().endswith('.xml')]
            if len(members) != integrity['xml_files']:
                raise ValueError("ZIP member list changed after audit")
            for number, member in enumerate(members, 1):
                proof = audit['members'][member.filename]
                if member.CRC != proof['crc32'] or member.file_size != proof['file_size']:
                    raise ValueError("ZIP member changed after audit")
                try:
                    with archive.open(member) as stream:
                        stats = stream_xml_file(stream, data_date=snapshot_date, consume=consume,
                                                verified_counts=proof)
                except ValueError as error:
                    raise ValueError(f"{member.filename}: {error}") from error
                for key in totals:
                    totals[key] += stats.get(key, stats['source_records'])
                if number == 1 or number % 100 == 0 or number == len(members):
                    print(f"W1-003: XML {number}/{len(members)}, документов {totals['source_documents']:,}, фактов {totals['source_records']:,}", flush=True)
        if batch:
            inserted += _insert_batch(run_id=run_id, dataset_id=dataset_id, records=batch)
    except BadZipFile as error:
        raise ValueError("Файл ФНС не является корректным ZIP") from error
    if sha256_file(archive_path) != integrity['archive_sha256']:
        raise ValueError("Archive changed during import")
    if not totals['source_records']:
        raise ValueError("Пустой snapshot ФНС не публикуется")
    for key in ('source_documents', 'source_records'):
        if totals[key] != integrity['independent_counts'][key]:
            raise ValueError(f"Full archive independent {key} mismatch")
    if totals['source_records'] != totals['eligible_records'] + totals['excluded_npd_records']:
        raise ValueError("Контроль company/IP + excluded NPD records не пройден")
    if inserted != totals['eligible_records']:
        raise ValueError("Число сохранённых фактов не соответствует eligible_records")
    integrity = {**integrity, 'parser_counts_match': True, 'archive_sha256_after_matches': True}
    return {"xml_files": len(members), **totals, "inserted_records": inserted,
            "snapshot_data_date": snapshot_date.isoformat(), "integrity": integrity,
            "source_quality": quality,
            "duplicates": 0, "conflicting_duplicates": 0, "rejected_records": 0}


def publish_ingestion_run(run_id, stats):
    integrity = stats.get('integrity') or {}
    if not (integrity.get('policy') == POLICY and integrity.get('crc_eof_all_members')
            and integrity.get('parser_counts_match') and integrity.get('archive_sha256_after_matches')):
        raise ValueError("Cannot publish without full archive integrity evidence")
    for key in ('source_documents', 'source_records'):
        if stats[key] != integrity['independent_counts'][key]:
            raise ValueError("Cannot publish divergent independent/parser counts")
    with get_session() as session:
        run = session.execute(select(IngestionRun).where(IngestionRun.id == run_id).with_for_update()).scalar_one()
        if run.status != 'running':
            raise ValueError("Публиковать можно только running ingestion run")
        dataset = session.execute(select(DataSet).where(DataSet.id == run.dataset_id).with_for_update()).scalar_one()
        persisted = session.scalar(select(func.count()).select_from(FnsSmeSupportEntry).where(FnsSmeSupportEntry.ingestion_run_id == run_id))
        if persisted != stats['inserted_records'] or persisted != stats['eligible_records']:
            raise ValueError("Повторное чтение PostgreSQL не совпало со счётчиком импорта")
        if stats['source_records'] != stats['eligible_records'] + stats['excluded_npd_records']:
            raise ValueError("Нельзя публиковать snapshot с нарушенным балансом записей")
        if not run.source_url or run.file_checksum != integrity['archive_sha256']:
            raise ValueError("Нельзя публиковать snapshot без provenance source_url/checksum")
        snapshot_date = date.fromisoformat(stats['snapshot_data_date'])
        if dataset.last_data_date and snapshot_date < dataset.last_data_date:
            raise ValueError("Нельзя заменить опубликованный snapshot более старой датой")
        now = datetime.now(timezone.utc)
        run.data_date = snapshot_date
        run.status = 'success'
        run.finished_at = now
        run.rows_read = stats['source_records']
        run.rows_inserted = persisted
        run.rows_updated = 0
        run.rows_skipped = stats['excluded_npd_records']
        run.errors_count = 0
        run.error_message = None
        run.details = {**(run.details or {}), **stats, 'complete_snapshot': True,
                       'person_rows_persisted': False, 'matching_method': 'inn_exact'}
        dataset.last_data_date = snapshot_date
        dataset.last_success_at = now
        session.commit()
        return {'run_id': run.id, 'dataset_id': dataset.id, 'data_date': run.data_date,
                'persisted_records': persisted, **stats}


def cleanup_old_snapshots(*, dataset_id, keep_run_id):
    with get_session() as session:
        old_runs = select(IngestionRun.id).where(IngestionRun.dataset_id == dataset_id,
            IngestionRun.id < keep_run_id, IngestionRun.status.in_(['success', 'failed']))
        result = session.execute(delete(FnsSmeSupportEntry).where(
            FnsSmeSupportEntry.dataset_id == dataset_id,
            FnsSmeSupportEntry.ingestion_run_id.in_(old_runs)))
        session.commit()
        return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Worker Foundation adapter
# ---------------------------------------------------------------------------

SOURCE_ID = DATASET_CODE
SOURCE_PAGE_URL = "https://www.nalog.gov.ru/opendata/7707329152-rsmppp/"
HANDLER_VERSION = "sme-support-official-v1"


def _worker_spec():
    from app.ingestion.fns_bulk_worker import FnsBulkSourceSpec

    return FnsBulkSourceSpec(
        source_id=SOURCE_ID,
        dataset_code=DATASET_CODE,
        source_page_url=SOURCE_PAGE_URL,
        source_path="7707329152-rsmppp",
        handler_version=HANDLER_VERSION,
        kind="sme_support",
        api_projection="fns_sme_support_check",
        card_projection="company_card.fns_sme_support",
        check_frequency="daily",
    )


def iter_worker_xml_records(xml_file):
    """Yield every support fact while marking NPD-only rows non-projectable.

    The immutable normalized snapshot remains complete.  Publication stores
    only facts whose recipient INN exactly matches a legal entity or IP in the
    Master registry; no person-name matching exists in this path.
    """

    provider_inn = None
    root = None
    try:
        for event, element in ET.iterparse(xml_file, events=("start", "end")):
            tag = local_name(element.tag)
            if event == "start" and root is None:
                if tag != "Файл":
                    raise ValueError("Ожидался корневой элемент Файл")
                root = element
            if event != "end":
                continue
            if tag == "ИдОтпр":
                provider_inn = _provider_inn_from_sender(element)
                element.clear()
                continue
            if tag != "Документ":
                continue
            snapshot_date = parse_date(element.get("ДатаСост"))
            if snapshot_date is None:
                # Compatibility fixtures use the fact-level information date.
                snapshot_date = parse_date(element.get("ДатаСвед"))
            if snapshot_date is None:
                raise ValueError("У документа ФНС отсутствует дата состояния")
            for record in parse_support_records(
                element,
                data_date=snapshot_date,
                provider_inn=provider_inn,
                legacy_parser=parse_support_document,
            ):
                yield {
                    **record,
                    "eligible_for_company_projection": (
                        record["recipient_kind"] != "npd_individual"
                    ),
                }
            if root is not None:
                root.remove(element)
            element.clear()
    except ET.ParseError as error:
        raise ValueError(f"Некорректный XML ФНС: {error}") from error


def _discover_worker_release(*, now=None, provider=None):
    from app.ingestion.fns_bulk_worker import FnsRelease, _utc, utc_now
    from app.providers.fns_sme_support_provider import FnsSmeSupportProvider

    observed_at = _utc(now or utc_now())
    discovered = (provider or FnsSmeSupportProvider()).discover_release()
    if discovered.modified_date is None or not discovered.structure_url:
        from app.worker.errors import SchemaMismatchError

        raise SchemaMismatchError(
            "official SME-support passport has no release date or structure"
        )
    return FnsRelease(
        source_page_url=SOURCE_PAGE_URL,
        artifact_url=discovered.data_url,
        xsd_url=discovered.structure_url,
        source_data_date=discovered.modified_date,
        actual_until=discovered.data_date,
        discovered_at=observed_at,
        provenance=(
            "Дата последнего внесения изменений "
            f"{discovered.modified_date.isoformat()}"
        ),
    )


def fns_sme_support_worker_handler(context):
    from app.ingestion.fns_bulk_worker import run_bulk_handler

    return run_bulk_handler(
        context,
        spec=_worker_spec(),
        iterator=iter_worker_xml_records,
    )


def _support_signature(value):
    return (
        value["provider_inn"],
        value["recipient_inn"],
        value["recipient_kind"],
        value.get("recipient_ogrn"),
        value["information_date"],
        value["support_until"],
        value["decision_date"],
        value.get("termination_date"),
        value.get("violation_code"),
        value.get("support_form_code"),
        value.get("support_form_name"),
        value.get("support_type_code"),
        value.get("support_type_name"),
        tuple(sorted((item.get("value"), item.get("unit_code")) for item in value.get("amounts") or ())),
    )


def _support_row_values(row, *, dataset_id, ingestion_run_id):
    def parsed(name):
        return date.fromisoformat(row[name]) if row.get(name) else None

    return {
        "dataset_id": dataset_id,
        "ingestion_run_id": ingestion_run_id,
        "data_date": parsed("data_date"),
        "source_record_key": row["source_record_key"],
        "source_document_id": row["source_document_id"],
        "provider_inn": row.get("provider_inn"),
        "recipient_inn": row["recipient_inn"],
        "recipient_kind": row["recipient_kind"],
        "recipient_ogrn": row.get("recipient_ogrn"),
        "information_date": parsed("information_date"),
        "support_until": parsed("support_until"),
        "decision_date": parsed("decision_date"),
        "termination_date": parsed("termination_date"),
        "violation_code": row.get("violation_code"),
        "support_form_code": row.get("support_form_code"),
        "support_form_name": row.get("support_form_name"),
        "support_type_code": row.get("support_type_code"),
        "support_type_name": row.get("support_type_name"),
        "amounts": list(row.get("amounts") or ()),
        "violations": list(row.get("violations") or ()),
        "regulatory_document_ids": list(row.get("regulatory_document_ids") or ()),
    }


def _project_worker_support(
    session,
    *,
    dataset,
    ingestion_run,
    staging_path,
    replay,
):
    from app.ingestion.fns_bulk_worker import _iter_jsonl

    matched = unmatched = excluded_npd = inserted = 0
    matched_company_ids = set()
    signatures = {}
    for batch in _iter_jsonl(staging_path):
        eligible = [
            row for row in batch if row.get("eligible_for_company_projection")
        ]
        excluded_npd += len(batch) - len(eligible)
        inns = {str(row["recipient_inn"]) for row in eligible}
        companies = {
            inn: (company_id, entity_type)
            for inn, company_id, entity_type in session.execute(
                select(Company.inn, Company.id, Company.entity_type).where(
                    Company.inn.in_(inns)
                )
            )
        }
        values = []
        for row in eligible:
            company = companies.get(str(row["recipient_inn"]))
            expected_type = (
                "legal"
                if row["recipient_kind"] == "legal"
                else "individual_entrepreneur"
            )
            if company is None or str(company[1]) != expected_type:
                unmatched += 1
                continue
            matched += 1
            matched_company_ids.add(int(company[0]))
            value = _support_row_values(
                row,
                dataset_id=dataset.id,
                ingestion_run_id=ingestion_run.id,
            )
            signatures[value["source_record_key"]] = _support_signature(value)
            values.append(value)
        if not values:
            continue
        statement = insert(FnsSmeSupportEntry).values(values)
        if replay:
            statement = statement.on_conflict_do_nothing(
                constraint="uq_fns_sme_support_run_record_key"
            )
        inserted += len(
            session.scalars(statement.returning(FnsSmeSupportEntry.id)).all()
        )
    return {
        "matched": matched,
        "unmatched": unmatched,
        "excluded_npd": excluded_npd,
        "source_records": matched + unmatched + excluded_npd,
        "inserted": inserted,
        "matched_company_ids": matched_company_ids,
        "signatures": signatures,
    }


def publish_fns_sme_support_worker_result(session, claim, result):
    from dataclasses import replace
    from datetime import time

    from app.contracts.data_readiness import AutoUpdateStatus
    from app.ingestion.fns_bulk_worker import (
        _accepted_replay_path,
        _apply_successful_check,
        _claim_actual_until,
        _file_path,
        utc_now,
    )
    from app.models.worker import WorkerPublicationState
    from app.worker.contracts import ExecutionCounters, SourceChangeSummary
    from app.worker.errors import InvalidDataError

    spec = _worker_spec()
    dataset = session.scalar(
        select(DataSet).where(DataSet.code == DATASET_CODE).with_for_update()
    )
    if dataset is None:
        raise InvalidDataError(f"dataset is not registered: {DATASET_CODE}")
    now = utc_now()
    source_date = date.fromisoformat(str(claim.schedule_metadata["source_data_date"]))
    previous_source_date = dataset.last_data_date
    actual_until = _claim_actual_until(claim)

    if claim.schedule_metadata.get("check_only"):
        state = session.get(WorkerPublicationState, SOURCE_ID)
        validation = dict(state.validation_metadata or {}) if state else {}
        legacy_run_id = (validation.get("validation") or {}).get(
            "legacy_ingestion_run_id"
        )
        ingestion_run = session.get(IngestionRun, legacy_run_id)
        if ingestion_run is None or ingestion_run.status != "success":
            raise InvalidDataError("accepted SME-support publication run is unavailable")
        replay_path = _accepted_replay_path(session, claim=claim, spec=spec)
        projected = _project_worker_support(
            session,
            dataset=dataset,
            ingestion_run=ingestion_run,
            staging_path=replay_path,
            replay=True,
        )
        published = int(
            session.scalar(
                select(func.count()).select_from(FnsSmeSupportEntry).where(
                    FnsSmeSupportEntry.ingestion_run_id == ingestion_run.id
                )
            )
            or 0
        )
        ingestion_run.rows_inserted = published
        ingestion_run.records_written = published
        ingestion_run.details = {
            **(ingestion_run.details or {}),
            "eligible_records": published,
            "source_eligible_records": projected["matched"] + projected["unmatched"],
            "excluded_npd_records": projected["excluded_npd"],
        }
        dataset.record_count = published
        status = _apply_successful_check(
            dataset,
            actual_until=actual_until,
            now=now,
            check_interval=spec.check_interval,
        )
        summary = SourceChangeSummary(
            matched_companies=len(projected["matched_company_ids"]),
            new_facts=projected["inserted"],
            changed_facts=0,
            removed_or_expired_facts=0,
            unchanged_facts=published - projected["inserted"],
            replayed_facts=projected["inserted"],
            quarantined_records=0,
            source_records=projected["source_records"],
            source_data_date=source_date,
            previous_source_data_date=previous_source_date,
        )
        coverage = dict(dataset.coverage or {})
        coverage["last_replay"] = {
            "checked_at": now.isoformat(),
            "matched": projected["matched"],
            "unmatched": projected["unmatched"],
            "new_facts": projected["inserted"],
        }
        coverage["published_facts"] = published
        coverage["change_summary"] = summary.as_dict()
        dataset.coverage = coverage
        return replace(
            result,
            staging_result=None,
            checksum_metadata={
                **result.checksum_metadata,
                "freshness": status.value,
                "official_actual_until": actual_until.isoformat()
                if actual_until
                else None,
            },
            counters=ExecutionCounters(
                records_seen=projected["source_records"],
                records_written=projected["inserted"],
                records_published=projected["inserted"],
            ),
            change_summary=summary,
        )

    if result.staging_result is None:
        raise InvalidDataError("SME-support publisher requires normalized staging")
    staging_path = _file_path(result.staging_result.staging_pointer)
    previous_run = session.scalar(
        select(IngestionRun)
        .where(IngestionRun.dataset_id == dataset.id, IngestionRun.status == "success")
        .order_by(IngestionRun.finished_at.desc().nullslast(), IngestionRun.id.desc())
        .limit(1)
    )
    previous = {}
    if previous_run is not None:
        for row in session.scalars(
            select(FnsSmeSupportEntry).where(
                FnsSmeSupportEntry.ingestion_run_id == previous_run.id
            )
        ):
            previous[row.source_record_key] = _support_signature(row.__dict__)

    ingestion_run = IngestionRun(
        dataset_id=dataset.id,
        status="running",
        data_date=source_date,
        source_file_name=Path(staging_path).name,
        source_url=str(claim.schedule_metadata["artifact_url"]),
        file_checksum=str(result.checksum_metadata.get("artifact_sha256") or ""),
        run_uuid=str(claim.run_id),
        trigger="worker",
        source_as_of=datetime.combine(source_date, time.min, tzinfo=timezone.utc),
        retrieved_at=now,
        version=str(claim.schedule_metadata.get("release_identity") or ""),
    )
    session.add(ingestion_run)
    session.flush()
    projected = _project_worker_support(
        session,
        dataset=dataset,
        ingestion_run=ingestion_run,
        staging_path=staging_path,
        replay=False,
    )
    published = projected["inserted"]
    current = projected["signatures"]
    shared = set(previous) & set(current)
    changed = sum(previous[key] != current[key] for key in shared)
    unchanged = len(shared) - changed
    new = len(set(current) - set(previous))
    removed = len(set(previous) - set(current))
    ingestion_run.status = "success"
    ingestion_run.finished_at = now
    ingestion_run.rows_read = projected["source_records"]
    ingestion_run.rows_inserted = published
    ingestion_run.rows_skipped = projected["unmatched"] + projected["excluded_npd"]
    ingestion_run.records_seen = projected["source_records"]
    ingestion_run.records_written = published
    ingestion_run.details = {
        "complete_snapshot": True,
        "person_rows_persisted": False,
        "matching_method": "inn_exact",
        "source_records": projected["source_records"],
        "eligible_records": published,
        "source_eligible_records": projected["matched"] + projected["unmatched"],
        "excluded_npd_records": projected["excluded_npd"],
    }
    dataset.enabled = True
    dataset.auto_update_status = AutoUpdateStatus.CONFIGURED
    dataset.last_success_at = now
    dataset.last_data_date = source_date
    dataset.source_as_of = datetime.combine(source_date, time.min, tzinfo=timezone.utc)
    dataset.retrieved_at = now
    dataset.published_at = now
    dataset.record_count = published
    status = _apply_successful_check(
        dataset,
        actual_until=actual_until,
        now=now,
        check_interval=spec.check_interval,
    )
    summary = SourceChangeSummary(
        matched_companies=len(projected["matched_company_ids"]),
        new_facts=new,
        changed_facts=changed,
        removed_or_expired_facts=removed,
        unchanged_facts=unchanged,
        replayed_facts=0,
        quarantined_records=0,
        source_records=projected["source_records"],
        source_data_date=source_date,
        previous_source_data_date=previous_source_date,
        unavailable_reasons=(
            {"previous_source_data_date": "first accepted publication"}
            if previous_source_date is None
            else {}
        ),
    )
    dataset.coverage = {
        "source_records": projected["source_records"],
        "source_eligible_records": projected["matched"] + projected["unmatched"],
        "excluded_npd_records": projected["excluded_npd"],
        "matched": projected["matched"],
        "unmatched": projected["unmatched"],
        "published_facts": published,
        "risk_summary_candidate_companies": len(projected["matched_company_ids"]),
        "api_projection": spec.api_projection,
        "card_projection": spec.card_projection,
        "release_identity": claim.schedule_metadata.get("release_identity"),
        "change_summary": summary.as_dict(),
    }
    validation = replace(
        result.staging_result.validation,
        metadata={
            **result.staging_result.validation.metadata,
            "legacy_ingestion_run_id": ingestion_run.id,
            "matched": projected["matched"],
            "unmatched": projected["unmatched"],
            "published_facts": published,
            "api_projection": spec.api_projection,
            "card_projection": spec.card_projection,
            "freshness": status.value,
            "official_actual_until": actual_until.isoformat()
            if actual_until
            else None,
        },
    )
    return replace(
        result,
        staging_result=replace(result.staging_result, validation=validation),
        counters=ExecutionCounters(
            records_seen=projected["source_records"],
            records_written=result.counters.records_written if result.counters else 0,
            records_published=published,
        ),
        change_summary=summary,
    )


def register_fns_sme_support_worker(session, registry):
    from app.ingestion.fns_bulk_worker import register_bulk_handler

    return register_bulk_handler(
        session,
        registry,
        spec=_worker_spec(),
        handler=fns_sme_support_worker_handler,
        publisher=publish_fns_sme_support_worker_result,
    )


def schedule_fns_sme_support_check(
    session,
    *,
    raw_root,
    now=None,
    provider=None,
):
    from app.ingestion.fns_bulk_worker import enqueue_bulk_release
    from app.models.worker import WorkerPublicationState

    release = _discover_worker_release(now=now, provider=provider)
    state = session.get(WorkerPublicationState, SOURCE_ID)
    validation = dict(state.validation_metadata or {}) if state else {}
    same_release = (
        (validation.get("validation") or {}).get("release_identity")
        == release.identity
    )
    return enqueue_bulk_release(
        session,
        spec=_worker_spec(),
        release=release,
        raw_root=Path(raw_root),
        check_only=same_release,
        replay_pointer=state.active_pointer if same_release and state else None,
        replay_checksum=str(validation.get("checksum") or "")
        if same_release
        else None,
        scheduled_for=release.discovered_at.date(),
    )
