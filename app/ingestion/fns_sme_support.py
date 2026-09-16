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
