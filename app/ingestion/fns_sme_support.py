from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.fns_sme_support import FnsSmeSupportEntry
from app.models.source import DataSet, IngestionRun


DATASET_CODE = "fns_sme_support"
DEFAULT_BATCH_SIZE = 2000
MAX_BATCH_SIZE = 2500
UNIT_NAMES = {
    "1": "рубль",
    "2": "квадратный метр",
    "3": "час",
    "4": "процент",
    "5": "единица",
}


def local_name(tag) -> str:
    text = str(tag or "")
    return text.rsplit("}", 1)[-1] if "}" in text else text


def parse_date(value) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            pass
    return None


def _text(value) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def normalize_inn(value) -> str | None:
    text = _text(value)
    if text and text.isdigit() and len(text) in {10, 12}:
        return text
    return None


def normalize_ogrn(value) -> str | None:
    text = _text(value)
    if text and text.isdigit() and len(text) in {13, 15}:
        return text
    return None


def _direct_child(element, *names):
    wanted = set(names)
    for child in list(element):
        if local_name(child.tag) in wanted:
            return child
    return None


def _value(element, *names):
    for name in names:
        value = element.attrib.get(name)
        if _text(value):
            return _text(value)
    child = _direct_child(element, *names)
    if child is None:
        return None
    if _text(child.text):
        return _text(child.text)
    for name in names:
        if _text(child.attrib.get(name)):
            return _text(child.attrib.get(name))
    if len(child.attrib) == 1:
        return _text(next(iter(child.attrib.values())))
    return None


def _complex_values(document, tag_name: str) -> list[ET.Element]:
    return [child for child in list(document) if local_name(child.tag) == tag_name]


def _attribute_or_child(element: ET.Element, name: str):
    value = element.attrib.get(name)
    if _text(value):
        return _text(value)
    child = _direct_child(element, name)
    return _text(child.text) if child is not None else None


def parse_support_document(
    document: ET.Element,
    *,
    provider_inn: str | None,
    data_date: date,
) -> dict:
    if local_name(document.tag) != "Документ":
        raise ValueError("Ожидался элемент Документ")

    source_document_id = _value(
        document,
        "ИдДок",
        "ИдПоддержки",
        "ИдПоддерж",
        "ИдПод",
        "НомерПоддержки",
        "НомПод",
    )
    if not source_document_id:
        raise ValueError("У записи поддержки нет идентификатора документа")

    legal_inn = normalize_inn(_value(document, "ИННЮЛ"))
    person_inn = normalize_inn(_value(document, "ИННФЛ"))
    recipient_inn = legal_inn or person_inn
    if recipient_inn is None:
        raise ValueError("У записи поддержки нет валидного ИНН получателя")

    recipient_ogrn = normalize_ogrn(_value(document, "ОГРН", "ОГРНИП"))
    if len(recipient_inn) == 10:
        recipient_kind = "legal"
        if recipient_ogrn is not None and len(recipient_ogrn) != 13:
            raise ValueError("ОГРН юридического лица должен содержать 13 цифр")
    else:
        if recipient_ogrn is not None and len(recipient_ogrn) != 15:
            raise ValueError("ОГРНИП должен содержать 15 цифр")
        recipient_kind = (
            "individual_entrepreneur_or_kfh"
            if recipient_ogrn is not None
            else "npd_individual"
        )

    information_date = parse_date(_value(document, "ДатаСвед"))
    support_until = parse_date(_value(document, "СрокПод"))
    decision_date = parse_date(_value(document, "ДатаОказ"))
    termination_date = parse_date(_value(document, "ДатаПрекр"))
    if information_date is None or support_until is None or decision_date is None:
        raise ValueError("У записи поддержки отсутствуют обязательные даты")

    violation_code = _value(document, "ИнфНаруш")
    if violation_code not in {None, "1", "2"}:
        raise ValueError("Некорректный код ИнфНаруш")

    form = _direct_child(document, "ФормПод")
    support_type = _direct_child(document, "ВидПод")
    support_form_code = _attribute_or_child(form, "КодФорм") if form is not None else None
    support_form_name = _attribute_or_child(form, "НаимФорм") if form is not None else None
    support_type_code = _attribute_or_child(support_type, "КодВид") if support_type is not None else None
    support_type_name = _attribute_or_child(support_type, "НаимВид") if support_type is not None else None

    amounts = []
    for item in _complex_values(document, "РазмПод"):
        amount = _attribute_or_child(item, "РазмПод")
        unit_code = _attribute_or_child(item, "ЕдПод")
        if amount is None or unit_code is None:
            raise ValueError("Неполный элемент РазмПод")
        amounts.append(
            {
                "value": amount,
                "unit_code": unit_code,
                "unit_name": UNIT_NAMES.get(unit_code, f"код {unit_code}"),
            }
        )
    if not amounts:
        raise ValueError("У записи поддержки отсутствует размер поддержки")

    violations = []
    for item in _complex_values(document, "Нарушения"):
        violations.append(
            {
                "kind_code": _attribute_or_child(item, "ВидНаруш"),
                "violation_date": _attribute_or_child(item, "ДатаНаруш"),
                "remedy_deadline": _attribute_or_child(item, "СрокНаруш"),
                "remedied_date": _attribute_or_child(item, "ДатаУстрНаруш"),
            }
        )
    if violation_code == "1" and not violations:
        raise ValueError("ИнфНаруш=1, но детали нарушения отсутствуют")

    regulatory_document_ids = []
    for item in _complex_values(document, "РегДок"):
        identifier = _attribute_or_child(item, "ИдРД")
        if identifier and identifier not in regulatory_document_ids:
            regulatory_document_ids.append(identifier)

    clean_provider_inn = normalize_inn(provider_inn)
    if clean_provider_inn is not None and len(clean_provider_inn) != 10:
        clean_provider_inn = None

    source_record_key = (
        f"{clean_provider_inn or '-'}:{source_document_id}:{recipient_inn}"
    )

    return {
        "data_date": data_date,
        "source_record_key": source_record_key,
        "source_document_id": source_document_id,
        "provider_inn": clean_provider_inn,
        "recipient_inn": recipient_inn,
        "recipient_ogrn": recipient_ogrn,
        "recipient_kind": recipient_kind,
        "information_date": information_date,
        "support_until": support_until,
        "decision_date": decision_date,
        "termination_date": termination_date,
        "violation_code": violation_code,
        "support_form_code": support_form_code,
        "support_form_name": support_form_name,
        "support_type_code": support_type_code,
        "support_type_name": support_type_name,
        "amounts": amounts,
        "violations": violations,
        "regulatory_document_ids": regulatory_document_ids,
    }


def _provider_inn_from_sender(sender: ET.Element) -> str | None:
    value = sender.attrib.get("ИННЮЛ") or _value(sender, "ИННЮЛ")
    normalized = normalize_inn(value)
    return normalized if normalized and len(normalized) == 10 else None


def stream_xml_file(stream, *, data_date: date, consume) -> dict:
    provider_inn = None
    expected_documents = None
    source_records = 0
    eligible_records = 0
    excluded_npd_records = 0

    try:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            tag = local_name(element.tag)
            if event == "start" and tag == "Файл":
                raw_expected = element.attrib.get("КолДок")
                if raw_expected is None:
                    raise ValueError("В XML ФНС отсутствует обязательный КолДок")
                try:
                    expected_documents = int(raw_expected)
                except ValueError as error:
                    raise ValueError("Некорректный КолДок в XML ФНС") from error
                if expected_documents < 1:
                    raise ValueError("КолДок должен быть больше нуля")
                continue

            if event != "end":
                continue

            if tag == "ИдОтпр":
                provider_inn = _provider_inn_from_sender(element)
                element.clear()
                continue

            if tag != "Документ":
                continue

            record = parse_support_document(
                element,
                provider_inn=provider_inn,
                data_date=data_date,
            )
            source_records += 1
            if record["recipient_kind"] == "npd_individual":
                # Person/NPD is outside the current product boundary. We scan it
                # for completeness but deliberately do not persist personal rows.
                excluded_npd_records += 1
            else:
                consume(record)
                eligible_records += 1
            element.clear()
    except ET.ParseError as error:
        raise ValueError(f"Некорректный XML ФНС: {error}") from error

    if expected_documents is None:
        raise ValueError("Не найден корневой элемент Файл с КолДок")
    if source_records != expected_documents:
        raise ValueError(
            f"КолДок={expected_documents}, но распознано Документ={source_records}"
        )

    return {
        "expected_documents": expected_documents,
        "source_records": source_records,
        "eligible_records": eligible_records,
        "excluded_npd_records": excluded_npd_records,
    }


def get_dataset_id(dataset_code: str = DATASET_CODE) -> int:
    session = get_session()
    try:
        dataset_id = session.execute(
            select(DataSet.id).where(DataSet.code == dataset_code)
        ).scalar_one_or_none()
        if dataset_id is None:
            raise RuntimeError(
                f"Dataset {dataset_code} не зарегистрирован. Запусти scripts.init_sources."
            )
        return dataset_id
    finally:
        session.close()


def create_ingestion_run(
    *, dataset_id: int, data_date: date, source_url: str | None, source_file_name: str | None,
    checksum: str | None,
) -> int:
    session = get_session()
    try:
        run = IngestionRun(
            dataset_id=dataset_id,
            status="running",
            data_date=data_date,
            source_url=source_url,
            source_file_name=source_file_name,
            file_checksum=checksum,
            details={"complete_snapshot": False, "person_rows_persisted": False},
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id
    finally:
        session.close()


def mark_run_failed(run_id: int, error: Exception, *, details: dict | None = None) -> None:
    session = get_session()
    try:
        run = session.get(IngestionRun, run_id)
        if run is not None:
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.errors_count = 1
            run.error_message = str(error)[:4000]
            run.details = {**(run.details or {}), **(details or {}), "complete_snapshot": False}
            session.execute(
                delete(FnsSmeSupportEntry).where(
                    FnsSmeSupportEntry.ingestion_run_id == run_id
                )
            )
            session.commit()
    finally:
        session.close()


def _insert_batch(*, run_id: int, dataset_id: int, records: list[dict]) -> int:
    if not records:
        return 0
    session = get_session()
    try:
        values = [
            {"ingestion_run_id": run_id, "dataset_id": dataset_id, **record}
            for record in records
        ]
        # Do not use ON CONFLICT DO NOTHING here. A duplicate inside one official
        # snapshot is a data-integrity failure, not a row to silently skip. The
        # database unique constraint makes this deterministic across batches.
        session.execute(insert(FnsSmeSupportEntry).values(values))
        session.commit()
        return len(records)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def import_fns_sme_support_archive(
    archive_path: Path,
    *,
    dataset_id: int,
    run_id: int,
    data_date: date,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict:
    if batch_size < 1:
        raise ValueError("batch_size должен быть > 0")
    if batch_size > MAX_BATCH_SIZE:
        raise ValueError(
            f"batch_size должен быть <= {MAX_BATCH_SIZE}, чтобы не превышать "
            "лимит bind-параметров PostgreSQL"
        )

    archive_path = Path(archive_path)
    batch: list[dict] = []
    inserted = 0
    source_records = 0
    eligible_records = 0
    excluded_npd_records = 0
    expected_documents = 0
    xml_files = 0

    def consume(record):
        nonlocal inserted
        batch.append(record)
        if len(batch) >= batch_size:
            written = _insert_batch(
                run_id=run_id, dataset_id=dataset_id, records=list(batch)
            )
            if written != len(batch):
                raise ValueError("Обнаружен дубликат source record key внутри snapshot ФНС")
            inserted += written
            batch.clear()

    try:
        with ZipFile(archive_path) as archive:
            members = [
                item for item in archive.infolist()
                if not item.is_dir() and item.filename.lower().endswith(".xml")
            ]
            if not members:
                raise ValueError("В ZIP ФНС нет XML-файлов")

            for member in members:
                xml_files += 1
                with archive.open(member) as stream:
                    stats = stream_xml_file(
                        stream, data_date=data_date, consume=consume
                    )
                source_records += stats["source_records"]
                eligible_records += stats["eligible_records"]
                excluded_npd_records += stats["excluded_npd_records"]
                expected_documents += stats["expected_documents"]

        if batch:
            written = _insert_batch(
                run_id=run_id, dataset_id=dataset_id, records=list(batch)
            )
            if written != len(batch):
                raise ValueError("Обнаружен дубликат source record key внутри snapshot ФНС")
            inserted += written
            batch.clear()
    except BadZipFile as error:
        raise ValueError("Файл ФНС не является корректным ZIP") from error

    if source_records < 1:
        raise ValueError("Пустой snapshot ФНС не публикуется")
    if source_records != expected_documents:
        raise ValueError("Контроль общего КолДок по ZIP ФНС не пройден")
    if source_records != eligible_records + excluded_npd_records:
        raise ValueError("Контроль company/IP + excluded NPD records не пройден")
    if inserted != eligible_records:
        raise ValueError(
            f"Ожидалось сохранить {eligible_records} company/IP записей, сохранено {inserted}"
        )

    return {
        "xml_files": xml_files,
        "source_records": source_records,
        "expected_documents": expected_documents,
        "eligible_records": eligible_records,
        "excluded_npd_records": excluded_npd_records,
        "inserted_records": inserted,
    }


def publish_ingestion_run(run_id: int, stats: dict) -> dict:
    session = get_session()
    try:
        run = session.execute(
            select(IngestionRun).where(IngestionRun.id == run_id).with_for_update()
        ).scalar_one()
        if run.status != "running":
            raise ValueError("Публиковать можно только running ingestion run")
        dataset = session.execute(
            select(DataSet).where(DataSet.id == run.dataset_id).with_for_update()
        ).scalar_one()

        persisted = session.execute(
            select(func.count()).select_from(FnsSmeSupportEntry).where(
                FnsSmeSupportEntry.ingestion_run_id == run_id
            )
        ).scalar_one()
        if persisted != stats["inserted_records"]:
            raise ValueError("Повторное чтение PostgreSQL не совпало со счётчиком импорта")
        if stats["source_records"] != stats["expected_documents"]:
            raise ValueError("Нельзя публиковать snapshot без полного контроля КолДок")
        if stats["source_records"] != stats["eligible_records"] + stats["excluded_npd_records"]:
            raise ValueError("Нельзя публиковать snapshot с нарушенным балансом записей")
        if not run.file_checksum or not run.source_url:
            raise ValueError("Нельзя публиковать snapshot без provenance source_url/checksum")

        now = datetime.now(timezone.utc)
        run.status = "success"
        run.finished_at = now
        run.rows_read = stats["source_records"]
        run.rows_inserted = persisted
        run.rows_updated = 0
        run.rows_skipped = stats["excluded_npd_records"]
        run.errors_count = 0
        run.error_message = None
        run.details = {
            **(run.details or {}),
            **stats,
            "complete_snapshot": True,
            "person_rows_persisted": False,
            "matching_method": "inn_exact",
        }
        dataset.last_data_date = run.data_date
        dataset.last_success_at = now
        session.commit()
        return {
            "run_id": run.id,
            "dataset_id": dataset.id,
            "data_date": run.data_date,
            "persisted_records": persisted,
            **stats,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def cleanup_old_snapshots(*, dataset_id: int, keep_run_id: int) -> int:
    session = get_session()
    try:
        result = session.execute(
            delete(FnsSmeSupportEntry).where(
                FnsSmeSupportEntry.dataset_id == dataset_id,
                FnsSmeSupportEntry.ingestion_run_id != keep_run_id,
            )
        )
        session.commit()
        return int(result.rowcount or 0)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
